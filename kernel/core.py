"""The Kernel: orchestrates boot, blue-green reboot, and rollback.

This is the brain that ties together versioning (history), the bootloader (slots +
processes), and the watchdog (health). The gateway is just an HTTP skin over it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
import uuid
from typing import Any, Callable

import httpx

from bootstrap import integrity
from kernel import (agent_runtime, bootloader, checks, evals, events, kernelmod,
                    opauth, preflight, previews, registry, state_store, triggers,
                    verifier, versioning, watchdog)
from kernel.bootloader import AppHandle


def _verify_reason(report: dict[str, Any]) -> str:
    """One readable sentence for a verification failure: the first failing check (named,
    with its origin version for a regression) plus how many more failed."""
    failed = report.get("failed") or []
    if not failed:
        return "verification failed"
    first = failed[0]
    what = f"check {first.get('name')!r}"
    if first.get("kind") == "regression" and first.get("origin"):
        what += f" (regression from {str(first['origin'])[:8]})"
    more = f" (+{len(failed) - 1} more failed)" if len(failed) > 1 else ""
    return f"verification failed: {what}: {first.get('detail')}{more}"


def _verification_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    """The compact verification record stored in the registry (and shown by the UI)."""
    if not (report.get("total") or report.get("failed")):
        return None
    return {
        "ok": report["ok"], "total": report["total"], "passed": report["passed"],
        "failed": [{"name": f.get("name"), "detail": f.get("detail"),
                    "kind": f.get("kind"), "origin": f.get("origin")}
                   for f in (report.get("failed") or [])[:5]],
    }


class Kernel:
    def __init__(self) -> None:
        self.config = state_store.load_config()
        self.current: AppHandle | None = None
        self.lock = asyncio.Lock()        # serialize reboots
        self.task_lock = asyncio.Lock()   # serialize self-mod tasks (v1: one at a time)
        self.booting = True
        self.current_staging = None       # staging of the in-flight task (for /validate)
        self.current_task_id: str | None = None  # the in-flight self-mod task, if any
        self.current_task_kind: str | None = None  # "change" | "revert" | "reapply"
        self.current_worker = None        # the live worker Popen (so cancel can kill it)
        self.cancel_requested = False     # set by cancel(); the run loop honors it
        self.current_steer: list[str] = []  # user steering messages queued for the worker
        self._task_seq = 0                # monotonic per-task event sequence (UI de-dupe)
        self._pending: dict[str, asyncio.Future] = {}  # task_id → result future (awaited by caller)
        self._drainer: asyncio.Task | None = None      # single consumer of the persisted queue
        self._monitor: asyncio.Task | None = None      # continuous liveness monitor (opt-in)
        self._monitor_stuck = False                    # de-dupes "nowhere to roll back" audits
        self.previews = previews.PreviewManager(lambda: self.config)
        self._preview_reaper: asyncio.Task | None = None  # stops idle previews
        self.triggers = triggers.TriggerManager(
            lambda: self.config, self._enqueue_autonomous, self._fetch_active_errors,
            fetch_proposals=self._fetch_advisor_queue)
        self._trigger_loop: asyncio.Task | None = None  # autonomous trigger scheduler
        self.restart_requested = False    # set by a kernel-update approval → __main__ exits 42
        self._server: Any = None          # the uvicorn Server (set by the gateway lifespan)

    # ── config shortcuts ──────────────────────────────────────────────────────────
    @property
    def _health_path(self) -> str:
        return self.config["app"]["health_path"]

    @property
    def _timeout(self) -> float:
        return float(self.config["watchdog"]["health_timeout_seconds"])

    @property
    def _interval(self) -> float:
        return float(self.config["watchdog"]["health_poll_interval"])

    async def _await_health(self, handle: AppHandle) -> tuple[bool, str]:
        return await watchdog.wait_healthy(handle, self._health_path, self._timeout, self._interval)

    # ── lifecycle ─────────────────────────────────────────────────────────────────
    async def boot(self) -> None:
        state_store.ensure_dirs()
        state_store.load_secrets()
        await self._validate_provider_key()
        self._mark_interrupted_task()
        versioning.ensure_repo()
        seed = versioning.seed_initial()
        # Migration + self-heal: (re)build the version-metadata index from git facts, so
        # pre-registry repos get seq numbers/statuses and a lost index is never fatal.
        versioning.reconcile_registry()

        slots = state_store.read_slots()
        target = slots.get("last_known_good") or seed

        handle = await bootloader.start(target, "a")
        ok, reason = await self._await_health(handle)

        if not ok and target != seed:
            # Recorded good version won't boot — fall back to the seed.
            state_store.audit("boot_health_failed", version=target[:12], reason=reason)
            await bootloader.stop(handle)
            registry.set_status(target, "health_failed", health={
                "ok": False, "reason": reason, "log_tail": state_store.read_log_tail("a")})
            target = seed
            handle = await bootloader.start(target, "a")
            ok, reason = await self._await_health(handle)

        if not ok:
            await bootloader.stop(handle)
            state_store.audit("boot_failed", version=target[:12], reason=reason)
            raise RuntimeError(f"app failed to boot: {reason}")

        self.current = handle
        history = [v for v in slots.get("promotion_history") or [] if v]
        # Legacy mirror: never point the one-step undo at the version we just booted —
        # a fresh boot used to leave previous_version == active_version, making
        # "roll back one step" reboot into itself.
        prev = slots.get("active_version")
        state_store.write_slots(
            {
                "active_slot": "a",
                "active_version": target,
                "previous_version": history[-1] if history else (prev if prev != target else None),
                "last_known_good": target,
                "promotion_history": history,  # the undo stack survives restarts
            }
        )
        self.booting = False
        state_store.audit("boot_ok", version=target[:12], port=handle.port)

        # Resume any self-mod backlog left by a previous run (durable queue).
        if state_store.read_queue():
            state_store.audit("queue_resume", pending=len(state_store.read_queue()))
            self._ensure_drainer()

        # Previews don't survive a kernel restart — their processes died with it. Clear
        # the leftover slot dirs; lines (git refs) survive and respawn previews on demand.
        previews.reap_stale_slot_dirs()
        self._preview_reaper = asyncio.create_task(self._reap_previews_loop())
        self._trigger_loop = asyncio.create_task(self._trigger_tick_loop())

        self._reconcile_kernel_boot_result()  # turn the firmware's swap outcome into audit + status

        self.apply_monitor_config()  # start the live watchdog if enabled

    async def _validate_provider_key(self) -> None:
        """Preflight the configured provider key so a dead credential fails the boot instead of
        surfacing as a 502 on the first real request. Blocks ONLY on a definitive auth rejection;
        a transient/undeterminable result warns and continues. No-op for the scripted engine.
        Runs once per kernel process (blue-green app reboots don't re-enter boot())."""
        status, detail = await preflight.check_provider_key(self.config)
        if status == "skip":
            return
        model = (self.config.get("agent", {}) or {}).get("model", "")
        if status == "invalid":
            state_store.audit("key_validation_failed", model=model, reason=(detail or "")[:300])
            raise RuntimeError(
                f"provider rejected the configured key for {model!r} — refusing to serve. Fix the "
                f"key in state/secrets.env (or set KERNEL_VALIDATE_KEYS=0 to skip the check): {detail}")
        if status == "unknown":
            state_store.audit("key_validation_unknown", model=model, reason=(detail or "")[:300])
            print(f"[kernel] WARN: could not verify the provider key for {model!r} — booting "
                  f"anyway (transient error, not a definitive rejection): {detail}", flush=True)
            return
        state_store.audit("key_validation_ok", model=model)

    # ── continuous watchdog (opt-in): auto-rollback when the LIVE app goes unhealthy ──
    def apply_monitor_config(self) -> None:
        """(Re)start or stop the live monitor to match config watchdog.monitor_* —
        called at boot and after every config change."""
        if self._monitor is not None and not self._monitor.done():
            self._monitor.cancel()
            self._monitor = None
        wd = self.config.get("watchdog", {})
        if not wd.get("monitor_enabled"):
            return
        self._monitor = asyncio.create_task(watchdog.monitor(
            lambda: self.current,
            self._health_path,
            float(wd.get("monitor_interval", 10)),
            int(wd.get("monitor_failures", 3)),
            self._on_live_unhealthy,
        ))

    async def _on_live_unhealthy(self, reason: str) -> None:
        """The LIVE app went unhealthy after promotion: auto-roll-back to the previous
        promoted version (fallback: last_known_good) through the normal health-gated
        reboot. reboot_to_version's registry bookkeeping marks the bad version
        rolled_back; a failed recovery leaves the monitor retrying next interval."""
        if self.lock.locked():
            return  # a reboot is already in flight; its own health gate decides
        bad = self.current.version if self.current else None
        slots = state_store.read_slots()
        history = [v for v in slots.get("promotion_history") or [] if v]
        lkg = slots.get("last_known_good")
        target = (next((v for v in reversed(history) if v != bad), None)
                  or (lkg if lkg != bad else None))
        if not target:
            if not self._monitor_stuck:  # audit once, not every poll interval
                self._monitor_stuck = True
                state_store.audit("monitor_unhealthy", version=(bad or "")[:12],
                                  reason=reason[:200], action="none: no version to roll back to")
            return
        self._monitor_stuck = False
        state_store.audit("monitor_unhealthy", version=(bad or "")[:12], reason=reason[:200],
                          action=f"auto-rollback to {target[:12]}")
        await self.reboot_to_version(
            target, reason=f"watchdog auto-rollback: {reason[:120]}", history_op="pop",
            run_regressions=False)

    async def reboot_to_version(self, sha: str, reason: str = "self-mod",
                                history_op: str = "push", *,
                                acceptance: list[dict[str, Any]] | None = None,
                                run_regressions: bool = True,
                                exclude_origins: set[str] | frozenset[str] = frozenset(),
                                freeze_meta: dict[str, Any] | None = None,
                                verification_note: str | None = None,
                                emit=None) -> dict[str, Any]:
        """Blue-green: start `sha` in the inactive slot; promote on health, else roll back.

        The active app is never touched until the candidate proves healthy, so a bad
        version can never take the system down.

        `history_op` maintains the undo stack (slots.promotion_history): "push" appends
        the version being departed (normal forward promotion / explicit jump); "pop" drops
        the stack tip instead (a rollback() consuming its own undo target).

        Verification Gate (opt-in, `verifier.enabled`): after the health gate and before
        promotion, the candidate must also pass `acceptance` (this change's derived
        checks) plus the frozen regression suite. Rollback callers pass
        `run_regressions=False` so the escape hatch is never blocked by the gate; a
        revert excludes the reverted version's own checks via `exclude_origins`.
        `freeze_meta` ({task, prompt}) lets passing acceptance checks be frozen into the
        suite on promotion; `verification_note` stamps an "unverified" promotion (skipped
        or fail-open derivation); `emit` surfaces verify progress into a task's event log.
        """
        async with self.lock:
            old = self.current
            new_slot = bootloader.other_slot(old.slot if old else "a")
            state_store.audit("reboot_begin", version=sha[:12], slot=new_slot, reason=reason)

            candidate = await bootloader.start(sha, new_slot)
            ok, why = await self._await_health(candidate)

            if not ok:
                await bootloader.stop(candidate)
                state_store.audit("rolled_back", version=sha[:12], reason=why)
                # Attach the crashed candidate's captured output so "why did this version
                # fail?" is answerable later (surfaces via /versions → the error tracker).
                registry.set_status(sha, "health_failed", health={
                    "ok": False, "reason": why,
                    "log_tail": state_store.read_log_tail(new_slot)})
                return {"ok": False, "version": sha, "reason": why}

            # Healthy is not enough (opt-in): the candidate must also BEHAVE — pass this
            # change's acceptance checks and the frozen regression suite — or it never
            # becomes the active version. A gate crash fails the PROMOTION, never the
            # live app (candidate promotions are always safe to reject).
            verification: dict[str, Any] | None = None
            if verifier.enabled(self.config) and (acceptance or run_regressions):
                if emit is not None:
                    emit({"kind": "verify", "summary": "running verification checks"})
                try:
                    report = await verifier.verify_candidate(
                        candidate.port, acceptance or [], self.config,
                        include_regressions=run_regressions,
                        exclude_origins=exclude_origins)
                except Exception as exc:
                    report = {"ok": False, "total": 0, "passed": 0, "results": [],
                              "failed": [{"name": "verification", "kind": "gate",
                                          "detail": f"verification crashed: {exc}"}]}
                checks.record_results(report["results"], sha)
                verification = _verification_summary(report)
                if not report["ok"]:
                    await bootloader.stop(candidate)
                    why = _verify_reason(report)
                    state_store.audit("verify_failed", version=sha[:12], reason=why[:300])
                    registry.set_status(sha, "verify_failed", verification=verification)
                    if emit is not None:
                        emit({"kind": "verify_failed", "summary": why[:200]})
                    return {"ok": False, "version": sha, "reason": why}
                if report["total"]:
                    state_store.audit("verify_passed", version=sha[:12], checks=report["total"])
                    if emit is not None:
                        emit({"kind": "verify",
                              "summary": f"verification passed ({report['passed']}/{report['total']} checks)"})
            if verification is None and verification_note:
                verification = {"unverified": True, "reason": verification_note}

            old_main = versioning.head()  # where the active line pointed BEFORE this promote
            self.current = candidate
            versioning.promote(sha)  # advance `main` to this healthy version
            slots = state_store.read_slots()
            history = [v for v in slots.get("promotion_history") or [] if v]
            if history_op == "pop":
                if history:
                    history.pop()
            elif old and old.version and old.version != sha:
                if not history or history[-1] != old.version:
                    history.append(old.version)
            history = history[-state_store.PROMOTION_HISTORY_MAX:]
            state_store.write_slots(
                {
                    "active_slot": new_slot,
                    "active_version": sha,
                    # kept as a mirror of the stack tip for backward compat (old UI/tools)
                    "previous_version": history[-1] if history else None,
                    "last_known_good": sha,
                    "promotion_history": history,
                }
            )
            # Registry bookkeeping: moving `main` re-draws the active line. Versions only
            # reachable from the OLD tip fall off it (tip → rolled_back, rest → abandoned);
            # previously-abandoned ancestors of the NEW tip are back on it (→ restored).
            if old_main != sha:
                off_line = versioning.commits_only_in(tip=old_main, not_in=sha)
                for x in off_line:
                    registry.set_status(x, "rolled_back" if x == old_main else "abandoned", by=sha)
                back_on = [x for x in versioning.commits_only_in(tip=sha, not_in=old_main)
                           if x != sha]
                for x in back_on:
                    meta = registry.get(x)
                    if meta and meta.get("status") in ("abandoned", "rolled_back"):
                        registry.set_status(x, "restored", via=sha)
                    elif meta and meta.get("status") == "line_promoted":
                        # A promoted line's commits just became part of the active line
                        # for real — upgrade them from "live on a line" to promoted.
                        registry.set_status(x, "promoted", via=sha)
                # The regression suite follows the active line: checks whose origin fell
                # off it are lifecycle-disabled; a restored origin gets its checks back
                # (sticky operator-disables stay put — see checks.sync_lifecycle). The
                # promoted sha itself is included: a roll-FORWARD to a version re-arms
                # the checks its earlier roll-back disabled.
                lifecycle = checks.sync_lifecycle(disable_origins=set(off_line),
                                                  enable_origins=set(back_on) | {sha})
                if lifecycle["disabled"] or lifecycle["enabled"]:
                    state_store.audit("checks_lifecycle", **lifecycle)
            registry.set_status(sha, "promoted", health={"ok": True, "reason": "healthy"},
                                verification=verification)
            # Passing acceptance checks become permanent regression checks: every future
            # candidate must keep this change working (the compounding safety net).
            if acceptance and freeze_meta is not None:
                frozen = checks.freeze_checks(acceptance, origin=sha,
                                              task_id=freeze_meta.get("task"),
                                              prompt=freeze_meta.get("prompt"))
                if frozen:
                    state_store.audit("check_frozen", version=sha[:12], count=len(frozen))
            await bootloader.stop(old)
            state_store.audit("promoted", version=sha[:12], slot=new_slot, port=candidate.port)
            return {"ok": True, "version": sha, "slot": new_slot, "port": candidate.port}

    async def rollback(self) -> dict[str, Any]:
        """One-step undo: walk one entry back through the promotion history, so repeated
        rollbacks step v5 → v4 → v3 (instead of ping-ponging between the last two)."""
        slots = state_store.read_slots()
        history = [v for v in slots.get("promotion_history") or [] if v]
        # Legacy slots.json (pre-undo-stack): fall back to the old single-field target.
        target = history[-1] if history else slots.get("previous_version")
        if not target:
            return {"ok": False, "reason": "no previous version to roll back to"}
        # run_regressions=False on every rollback path: recovery must never be blocked
        # by the Verification Gate (the target already passed it when it was promoted).
        return await self.reboot_to_version(target, reason="manual rollback", history_op="pop",
                                            run_regressions=False)

    async def rollback_to(self, sha: str) -> dict[str, Any]:
        """Revert to any specific version from history (sha, short sha, v<seq>, or label)."""
        full = versioning.resolve_version(sha)
        if not full:
            return {"ok": False, "reason": f"unknown version {sha}"}
        return await self.reboot_to_version(full, reason=f"rollback to {full[:8]}",
                                            run_regressions=False)

    def label_version(self, ref: str, label: str) -> dict[str, Any]:
        """Set (or clear, with an empty label) a human-friendly name on a version. Labels
        are unique and resolvable everywhere a version reference is accepted."""
        full = versioning.resolve_version(ref)
        if not full:
            return {"ok": False, "reason": f"unknown version {ref}"}
        ok, detail = registry.set_label(full, label or None)
        if not ok:
            return {"ok": False, "reason": detail}
        state_store.audit("label_set", version=full[:12], label=label or "")
        return {"ok": True, "version": full, "short": full[:8], "label": label or None}

    # ── preview environments + named lines ─────────────────────────────────────────────
    def _gate_for_preview(self, acceptance: list[dict[str, Any]] | None) -> Any:
        """The Verification Gate packaged as a previews.create verify-callback (used by
        line promotion). None when the gate is off. Deliberately does NOT stamp the
        stored checks' last_result — the prod store records prod truth, not experiments."""
        if not verifier.enabled(self.config):
            return None

        async def _verify(port: int) -> tuple[bool, str, dict | None]:
            report = await verifier.verify_candidate(port, acceptance or [], self.config)
            summary = _verification_summary(report)
            if not report["ok"]:
                return False, _verify_reason(report), summary
            return True, "", summary

        return _verify

    async def preview_version(self, ref: str, name: str | None = None) -> dict[str, Any]:
        """Boot a preview of ANY version (incl. a pending candidate — click around before
        approving). Auto-names from the version's seq number when no name is given."""
        full = versioning.resolve_version(ref)
        if not full:
            return {"ok": False, "reason": f"unknown version {ref}"}
        if not name:
            meta = registry.get(full) or {}
            name = f"v{meta['seq']}" if meta.get("seq") else full[:8]
        return await self.previews.create(name, full)

    async def preview_promote(self, name: str) -> dict[str, Any]:
        """Make what you're looking at the production version: a line preview promotes
        its line; a pending candidate goes through approval; anything else is a plain
        health+regression-gated reboot to that version."""
        p = self.previews.get(name)
        if p is None:
            return {"ok": False, "reason": f"no preview '{name}'"}
        if p.line:
            return await self.promote_line(p.line)
        if self._find_pending(p.version):
            result = await self.approve_version(p.version)
        else:
            result = await self.reboot_to_version(p.version, reason=f"promote preview {name}")
        if result.get("ok"):
            await self.previews.stop(name)  # it IS production now — the copy is redundant
        return {**result, "preview": name}

    # ── named lines: parallel version lines for experiments / staging / A-B ───────────
    async def create_line(self, name: str, from_ref: str | None = None,
                          description: str = "") -> dict[str, Any]:
        """Create line `name` starting at `from_ref` (default: the current head) and spin
        up its preview. The line is a git ref — it survives anything versions.git does."""
        if not previews.valid_name(name):
            return {"ok": False, "reason": "line name must be 1-24 chars of [a-z0-9-]"}
        if versioning.line_tip(name) is not None:
            return {"ok": False, "reason": f"line '{name}' already exists"}
        base = versioning.resolve_version(from_ref) if from_ref else versioning.head()
        if not base:
            return {"ok": False, "reason": f"unknown version {from_ref}"}
        versioning.set_line(name, base)
        lines = [ln for ln in state_store.read_lines() if ln.get("name") != name]
        lines.append({"name": name, "created_from": base, "created_at": time.time(),
                      "description": (description or "")[:200]})
        state_store.write_lines(lines)
        state_store.audit("line_created", line=name, base=base[:12])
        # Best-effort preview spawn: the line exists even if the preview cap is hit.
        preview = await self.previews.create(name, base, line=name)
        return {"ok": True, "line": name, "tip": base, "short": base[:8],
                "preview": preview, "url": f"/preview/{name}"}

    def list_lines(self) -> list[dict[str, Any]]:
        """Every line ref, enriched with metadata, tip identity, ahead/behind main, and
        its preview (if running). Refs are the truth; stale metadata rows are dropped."""
        refs = versioning.list_line_refs()
        meta_by_name = {ln.get("name"): ln for ln in state_store.read_lines()}
        head = versioning.head() if versioning.has_history() else None
        out = []
        for name in sorted(refs):
            tip = refs[name]
            reg = registry.get(tip) or {}
            preview = self.previews.for_line(name)
            out.append({
                "name": name, "tip": tip, "short": tip[:8], "seq": reg.get("seq"),
                "message": reg.get("message"),
                "ahead": len(versioning.commits_only_in(tip=tip, not_in=head)) if head else 0,
                "behind": len(versioning.commits_only_in(tip=head, not_in=tip)) if head else 0,
                "created_at": (meta_by_name.get(name) or {}).get("created_at"),
                "description": (meta_by_name.get(name) or {}).get("description") or "",
                "preview": ({"name": preview.name, "url": f"/preview/{preview.name}",
                             "version": preview.version[:8]} if preview else None),
                "url": f"/preview/{name}",
            })
        return out

    async def promote_line(self, name: str) -> dict[str, Any]:
        """Ship a line to production: health + verification-gated blue-green reboot to its
        tip. The existing bookkeeping re-draws the active line (the line's commits become
        promoted; whatever main abandons becomes recoverable, as with any promotion)."""
        tip = versioning.line_tip(name)
        if tip is None:
            return {"ok": False, "reason": f"unknown line '{name}'"}
        active = self.current.version if self.current else None
        if tip == active:
            return {"ok": True, "line": name, "version": tip, "short": tip[:8],
                    "promoted": True, "reason": "line tip is already the active version"}
        result = await self.reboot_to_version(tip, reason=f"promote line {name}")
        if result.get("ok"):
            state_store.audit("line_promoted", line=name, version=tip[:12])
            preview = self.previews.for_line(name)
            if preview is not None:  # prod now serves this tree — the copy is redundant
                await self.previews.stop(preview.name)
        return {**result, "line": name}

    async def delete_line(self, name: str) -> dict[str, Any]:
        """Discard a line: drop its ref, metadata, and preview. Its version commits stay
        in history (pinned by their v_* branches) and can be re-applied later."""
        if versioning.line_tip(name) is None:
            return {"ok": False, "reason": f"unknown line '{name}'"}
        preview = self.previews.for_line(name)
        if preview is not None:
            await self.previews.stop(preview.name)
        versioning.delete_line(name)
        state_store.write_lines([ln for ln in state_store.read_lines()
                                 if ln.get("name") != name])
        state_store.audit("line_deleted", line=name)
        return {"ok": True, "line": name, "deleted": True}

    # ── governance: approval-gated promotion (opt-in via agent.require_approval) ───────
    def _require_approval(self) -> bool:
        return bool(self.config.get("agent", {}).get("require_approval", False))

    def _should_hold(self, trigger: str | None, operator: bool | None = None) -> bool:
        """Whether a committed version holds for approval instead of promoting.

        Human changes follow `agent.require_approval` (off by default). With
        `operator_auth.enabled`, a change submitted WITHOUT operator credentials (e.g.
        app-process code enqueuing programmatically) also always holds — the enforcement
        half of "unattended enqueues can't self-promote". AUTONOMOUS (trigger-fired)
        changes ALWAYS hold — you wake up to a verified, previewable fix awaiting one
        click — UNLESS full-auto is explicitly enabled (`triggers.auto_promote`) AND the
        Verification Gate is on. auto_promote with the verifier OFF still holds and is
        audited: we never let a timer promote unverified code to production."""
        if not trigger:
            if self._require_approval():
                return True
            if opauth.enabled(self.config) and operator is not True:
                state_store.audit("hold_unattended", reason="change_request submitted "
                                  "without operator credentials while operator_auth is on")
                return True
            return False
        tcfg = self.config.get("triggers", {}) or {}
        if tcfg.get("auto_promote") and verifier.enabled(self.config):
            return False  # full-auto, and only because changes are behavior-verified
        if tcfg.get("auto_promote"):  # opted into full-auto but the gate is off → fail closed
            state_store.audit("trigger_hold_unverified", reason="auto_promote set but "
                              "verifier.enabled is off — holding for approval")
        return True

    def list_pending(self) -> list[dict[str, Any]]:
        """Versions committed but awaiting approval (review each via the /diff syscall)."""
        return state_store.read_pending()

    def _find_pending(self, sha: str) -> dict[str, Any] | None:
        return next((p for p in state_store.read_pending()
                     if sha in (p.get("sha"), p.get("short"))), None)

    async def approve_version(self, sha: str) -> dict[str, Any]:
        """Promote a pending version (blue-green, with the same health/rollback safety as any
        reboot). Clears it from the pending list only once it's live."""
        entry = self._find_pending(sha)
        if not entry:
            return {"ok": False, "reason": f"no pending version {sha}"}
        full = entry["sha"]
        # Verification runs now, at promotion time — with the acceptance checks that were
        # derived when the version was committed (carried in the pending entry).
        reboot = await self.reboot_to_version(
            full, reason=f"approved {full[:8]}",
            acceptance=entry.get("checks") or [],
            verification_note=entry.get("verification_note"),
            freeze_meta={"task": entry.get("task"), "prompt": entry.get("prompt")})
        if reboot["ok"]:
            state_store.remove_pending(full)
            state_store.audit("promotion_approved", version=full[:12])
        return {"ok": reboot["ok"], "version": full, "short": full[:8],
                "promoted": reboot["ok"], "reason": reboot.get("reason")}

    def reject_version(self, sha: str) -> dict[str, Any]:
        """Discard a pending version (it stays in history but is never promoted)."""
        entry = self._find_pending(sha)
        if not entry:
            return {"ok": False, "reason": f"no pending version {sha}"}
        state_store.remove_pending(entry["sha"])
        registry.set_status(entry["sha"], "rejected")
        state_store.audit("promotion_rejected", version=entry["sha"][:12])
        return {"ok": True, "version": entry["sha"], "short": entry["sha"][:8], "rejected": True}

    # ── Gated Kernel Self-Update (feature #4) ──────────────────────────────────────────
    # The agent rewrites the KERNEL itself, heavily gated: a candidate is validated stricter
    # than an app change, ALWAYS held for operator approval (no auto-promote path exists),
    # and applied by the immutable firmware which verifies + health-gates + auto-rolls-back.
    def _kernel_update_enabled(self) -> bool:
        return bool(self.config.get("kernel_update", {}).get("enabled", False))

    async def kernel_change_request(self, prompt: str) -> dict[str, Any]:
        """Enqueue a change to the KERNEL. Gated by the kernel_update master switch (OFF by
        default). Runs through the same durable FIFO (kind="kernel"), one at a time."""
        if not self._kernel_update_enabled():
            return {"ok": False, "reason": "kernel self-update is disabled — enable "
                    "kernel_update.enabled in Settings (this is a high-blast-radius feature)"}
        prompt = (prompt or "").strip()
        if not prompt:
            return {"ok": False, "reason": "prompt required"}
        task_id = "t" + uuid.uuid4().hex[:10]
        self._write_status(task_id, prompt, "queued", time.time())
        state_store.queue_enqueue(task_id, prompt, kind="kernel")
        state_store.audit("kernel_change_request_queued", task=task_id, prompt=prompt[:300])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[task_id] = fut
        self._ensure_drainer()
        return await fut

    async def _run_kernel_change(self, task_id: str, prompt: str) -> dict[str, Any]:
        """One kernel change: stage kernel.git → kernel-resident agent edits → strict
        validate → commit → ALWAYS hold as pending_kernel.json (never auto-promotes)."""
        async with self.task_lock:
            started_at = time.time()
            self.current_task_id = task_id
            self.current_task_kind = "kernel"
            self._task_seq = 0
            state_store.set_current_task(task_id)
            self._write_status(task_id, prompt, "running", started_at)
            emit = lambda ev: self._emit_event(task_id, ev)  # noqa: E731

            def finish(state: str, result: dict, summary: str) -> dict[str, Any]:
                self._write_status(task_id, prompt, state, started_at, result)
                emit({"kind": "done", "summary": summary, "result": result})
                return result

            state_store.audit("kernel_change_request", task=task_id, prompt=prompt[:300])
            emit({"kind": "request", "summary": f"[kernel] {prompt[:180]}"})
            staging = kernelmod.create_kernel_staging(task_id)
            try:
                try:
                    result = await kernelmod.run_kernel_agent(
                        task_id, prompt, staging, self.config, emit)
                except Exception as exc:
                    return finish("failed", {"ok": False, "task": task_id,
                                  "reason": f"kernel agent error: {exc}"}, "kernel agent error")
                if not result.get("proposed"):
                    return finish("failed", {"ok": False, "task": task_id,
                                  "reason": "agent did not propose a kernel change"}, "no proposal")

                emit({"kind": "propose_commit", "summary": "validating candidate kernel"})
                ok, report = await asyncio.to_thread(kernelmod.validate_kernel_staging, staging)
                if not ok:
                    state_store.audit("kernel_validate_failed", task=task_id, report=report[:300])
                    return finish("failed", {"ok": False, "task": task_id,
                                  "reason": report}, "kernel validation failed")

                committed = kernelmod.commit_kernel_staging(
                    staging, result["message"], task_id=task_id, prompt=prompt)
                if not committed:
                    return finish("failed", {"ok": False, "task": task_id,
                                  "reason": "no kernel changes to commit"}, "no changes")
                sha, digest = committed
                state_store.audit("kernel_version_committed", task=task_id,
                                  version=sha[:12], digest=digest[:16])
                emit({"kind": "committed", "summary": f"kernel {sha[:8]} — {result['message']}"})

                # ALWAYS hold: kernel changes are never auto-promoted. The firmware applies +
                # health-gates only after an explicit operator approval.
                state_store.write_pending_kernel({
                    "sha": sha, "short": sha[:8], "digest": digest, "task": task_id,
                    "message": result["message"], "prompt": prompt, "created_at": time.time(),
                })
                kernelmod.set_kernel_status(sha, "pending")
                state_store.audit("kernel_promotion_pending", task=task_id, version=sha[:12])
                emit({"kind": "pending", "summary": f"kernel {sha[:8]} awaiting approval"})
                return finish("pending", {
                    "ok": True, "task": task_id, "version": sha, "short": sha[:8],
                    "digest": digest, "promoted": False, "pending": True,
                    "message": result["message"], "kernel": True,
                }, "kernel change pending approval")
            finally:
                with contextlib.suppress(Exception):
                    shutil.rmtree(staging, ignore_errors=True)
                self.current_task_id = None
                self.current_task_kind = None

    def _signed_mode(self) -> bool:
        return bool((os.environ.get("KERNEL_INTEGRITY_PUBKEY") or "").strip())

    def approve_kernel_version(self, sha: str, signature: str | None = None) -> dict[str, Any]:
        """Operator approval of a pending kernel change. In signed mode the operator must
        supply a valid ed25519 signature over the candidate digest. On approval this only
        RECORDS the target (active_kernel.json) and requests a restart — the firmware does
        the verified swap + health-gate + rollback."""
        pending = state_store.read_pending_kernel()
        if not pending or sha not in (pending.get("sha"), pending.get("short")):
            return {"ok": False, "reason": f"no pending kernel version {sha}"}
        digest = pending["digest"]
        if self._signed_mode():
            pubkey = os.environ["KERNEL_INTEGRITY_PUBKEY"].strip()
            if not signature or not integrity._verify_signature(digest, pubkey, signature.strip()):
                state_store.audit("kernel_approve_denied", version=pending["sha"][:12],
                                  reason="missing/invalid signature (signed mode)")
                return {"ok": False, "reason": "signed mode: a valid operator signature over "
                        f"the candidate digest ({digest[:16]}…) is required"}
        active = {"version": pending["sha"], "digest": digest,
                  "boot_health_seconds": int(self.config.get("kernel_update", {})
                                              .get("boot_health_seconds", 60))}
        if signature:
            active["signature"] = signature.strip()
        state_store.write_active_kernel(active)   # stashes the prior active as the rollback target
        state_store.write_pending_kernel(None)
        kernelmod.set_kernel_status(pending["sha"], "approved")
        state_store.audit("kernel_promotion_approved", version=pending["sha"][:12],
                          signed=self._signed_mode())
        self.request_restart()
        return {"ok": True, "version": pending["sha"], "short": pending["sha"][:8],
                "restarting": True, "message": "approved — restarting so the firmware can "
                "apply, verify, and health-gate the new kernel"}

    def reject_kernel_version(self, sha: str) -> dict[str, Any]:
        pending = state_store.read_pending_kernel()
        if not pending or sha not in (pending.get("sha"), pending.get("short")):
            return {"ok": False, "reason": f"no pending kernel version {sha}"}
        state_store.write_pending_kernel(None)
        kernelmod.set_kernel_status(pending["sha"], "rejected")
        state_store.audit("kernel_promotion_rejected", version=pending["sha"][:12])
        return {"ok": True, "version": pending["sha"], "short": pending["sha"][:8], "rejected": True}

    def rollback_kernel(self) -> dict[str, Any]:
        """Operator rollback: point the active kernel back at the previous good version and
        restart so the firmware reconcile swaps it back on disk. With no recorded previous
        version, roll back to the SEED (kv1 = the shipped kernel) EXPLICITLY — the firmware
        only reconciles on-disk when a pointer exists, so we must name the target rather than
        clear it (else the promoted files would keep running)."""
        cur = state_store.read_active_kernel()
        if not cur:
            return {"ok": False, "reason": "the shipped kernel is already active — nothing to roll back"}
        prev = state_store.read_prev_active_kernel()
        if not prev:
            # No recorded predecessor (the first promote had no prior active) → roll back to
            # the SEED (kv1 = shipped). Use seed_version(), NOT seed_kernel()/head — after a
            # promote the line tip IS the promoted version, so head would be a no-op target.
            seed = kernelmod.seed_version()
            if not seed:
                return {"ok": False, "reason": "no seed kernel version recorded"}
            prev = {"version": seed["sha"], "digest": seed["digest"],
                    "boot_health_seconds": int(self.config.get("kernel_update", {})
                                               .get("boot_health_seconds", 60)), "shipped": True}
        # Don't restash the version we're leaving as the new rollback target (avoid ping-pong).
        if prev.get("version") == cur.get("version"):
            return {"ok": False, "reason": "already at the rollback target"}
        state_store.write_active_kernel(prev)
        to = prev.get("version", "")
        state_store.audit("kernel_rollback_requested", to=to[:12], shipped=bool(prev.get("shipped")))
        self.request_restart()
        return {"ok": True, "restarting": True,
                "to": "shipped" if prev.get("shipped") else to, "version": to}

    def request_restart(self) -> None:
        """Ask the process to restart so the firmware reconciles active_kernel.json. The
        gateway lifespan/entry checks `restart_requested` after uvicorn stops and exits 42."""
        self.restart_requested = True
        server = getattr(self, "_server", None)
        if server is not None:
            server.should_exit = True  # stop uvicorn → clean lifespan shutdown → exit 42

    def kernel_status(self) -> dict[str, Any]:
        active = state_store.read_active_kernel()
        pending = state_store.read_pending_kernel()
        on_disk = kernelmod.digest_of(state_store.ROOT)
        return {
            "enabled": self._kernel_update_enabled(),
            "signed_mode": self._signed_mode(),
            "shipped_digest": on_disk,
            "active": active,
            "in_sync": (active is None) or active.get("digest") == on_disk,
            "pending": pending,
            "has_rollback": state_store.read_prev_active_kernel() is not None,
            "seed_pubkey_set": bool((os.environ.get("KERNEL_INTEGRITY_PUBKEY") or "").strip()),
        }

    def _reconcile_kernel_boot_result(self) -> None:
        """At boot, resolve the outcome of any kernel swap into a hash-chained audit record
        + a kernel_versions.json status (the firmware can touch neither).

        Failure: the firmware left a breadcrumb before relaunching the previous kernel — this
        (now-restored) kernel consumes it and marks the bad version failed + audits the
        rollback. Success: NO breadcrumb — a healthy promote is self-evident, so if
        active_kernel.json names a freshly-approved version whose digest is what we're now
        running, we mark it promoted ourselves."""
        result = state_store.take_kernel_boot_result()
        if result and not result.get("ok"):
            sha, reason = result.get("version") or "", result.get("reason", "")
            kernelmod.set_kernel_status(sha, "kernel_health_failed", reason=reason[:200])
            state_store.audit("kernel_health_failed", version=sha[:12], reason=reason[:200])
            state_store.audit("kernel_rolled_back", version=sha[:12])

        active = state_store.read_active_kernel()
        if not active:
            return
        entry = kernelmod.get_kernel_version(active.get("version") or "")
        if (entry and entry.get("status") == "approved"
                and active.get("digest") == kernelmod.digest_of(state_store.ROOT)):
            kernelmod.set_kernel_status(active["version"], "promoted")
            state_store.audit("kernel_promoted", version=active["version"][:12])

    # ── operator git ops: selective revert / re-apply (queued like self-mods) ─────────
    async def revert_version(self, ref: str) -> dict[str, Any]:
        """Undo ONE version's changes while keeping everything after it: builds a new
        version with `git revert` and ships it through the normal validate → commit →
        blue-green pipeline. The surgical counterpart to rollback (which rewinds the
        whole active line and abandons everything after the target)."""
        full = versioning.resolve_version(ref)
        if not full:
            return {"ok": False, "reason": f"unknown version {ref}"}
        ancestors = versioning.main_ancestors()
        if full not in ancestors:
            return {"ok": False, "reason": "only versions on the active line can be reverted — "
                                           "an abandoned version is already inactive (re-apply is its counterpart)"}
        if versioning.parent_of(full) is None:
            return {"ok": False, "reason": "the seed version cannot be reverted"}
        meta = registry.get(full) or {}
        by = meta.get("reverted_by")
        if by and by in ancestors:  # its revert is still live on the active line
            by_meta = registry.get(by) or {}
            name = f"v{by_meta['seq']}" if by_meta.get("seq") else by[:8]
            return {"ok": False, "reason": f"already reverted by {name} — revert that version to restore it"}
        return await self._enqueue_git_op("revert", full)

    async def reapply_version(self, ref: str) -> dict[str, Any]:
        """Re-apply an ABANDONED version's changes onto the current line (cherry-pick),
        recovering work a rollback left behind. Ships through the same validate →
        commit → blue-green pipeline as any change."""
        full = versioning.resolve_version(ref)
        if not full:
            return {"ok": False, "reason": f"unknown version {ref}"}
        ancestors = versioning.main_ancestors()
        if full in ancestors:
            return {"ok": False, "reason": "version is already on the active line"}
        if any(p.get("sha") == full for p in state_store.read_pending()):
            return {"ok": False, "reason": "version is pending approval — approve or reject it instead"}
        meta = registry.get(full) or {}
        by = meta.get("reapplied_by")
        if by and by in ancestors:  # its re-apply is still live on the active line
            by_meta = registry.get(by) or {}
            name = f"v{by_meta['seq']}" if by_meta.get("seq") else by[:8]
            return {"ok": False, "reason": f"already re-applied by {name}"}
        return await self._enqueue_git_op("reapply", full)

    def _git_op_display(self, kind: str, target: str) -> tuple[str, str, str]:
        """(verb, human name, original commit subject) for prompts/messages/errors."""
        meta = registry.get(target) or {}
        name = f"v{meta['seq']} ({target[:8]})" if meta.get("seq") else target[:8]
        verb = "revert" if kind == "revert" else "re-apply"
        return verb, name, versioning.version_message(target)

    async def _enqueue_git_op(self, kind: str, target: str) -> dict[str, Any]:
        """Queue a git op through the same durable FIFO as self-mods, so it serializes
        with them, shows up in /task + SSE, and survives a kernel restart."""
        verb, name, subject = self._git_op_display(kind, target)
        prompt = f"{verb} {name}: {subject}"
        task_id = "t" + uuid.uuid4().hex[:10]
        self._write_status(task_id, prompt, "queued", time.time())
        state_store.queue_enqueue(task_id, prompt, kind=kind, payload={"target": target})
        state_store.audit(f"{kind}_request", task=task_id, version=target[:12])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[task_id] = fut
        self._ensure_drainer()
        return await fut

    async def _run_git_op(self, task_id: str, kind: str, target: str) -> dict[str, Any]:
        """One operator git op: stage → git revert/cherry-pick → validate → commit →
        blue-green reboot. Same durable status/event scaffolding and the same safety
        gates as a self-mod (full validation + health-gated promotion) — but no agent,
        and no approval hold: the operator clicking the button IS the approver."""
        async with self.task_lock:
            started_at = time.time()
            self.current_task_id = task_id
            self.current_task_kind = kind
            self._task_seq = 0
            state_store.set_current_task(task_id)
            verb, name, subject = self._git_op_display(kind, target)
            prompt = f"{verb} {name}: {subject}"
            self._write_status(task_id, prompt, "running", started_at)

            emit = lambda ev: self._emit_event(task_id, ev)  # noqa: E731

            def finish(state: str, result: dict, done_summary: str) -> dict[str, Any]:
                self._write_status(task_id, prompt, state, started_at, result)
                emit({"kind": "done", "summary": done_summary, "result": result})
                return result

            emit({"kind": "request", "summary": prompt[:200]})
            try:
                staging = versioning.create_staging(task_id)
                apply_op = (versioning.revert_onto_staging if kind == "revert"
                            else versioning.cherry_pick_onto_staging)
                ok, detail = await asyncio.to_thread(apply_op, staging, target)
                if not ok:
                    shutil.rmtree(staging, ignore_errors=True)
                    state_store.audit(f"{kind}_conflict", task=task_id,
                                      version=target[:12], detail=detail[:300])
                    return finish("failed", {
                        "ok": False, "task": task_id,
                        "reason": f"{name} does not {verb} cleanly onto the current version: {detail}",
                    }, f"{verb} conflict")

                ok, report = await asyncio.to_thread(agent_runtime.validate_staging, staging)
                if not ok:
                    shutil.rmtree(staging, ignore_errors=True)
                    return finish("failed", {
                        "ok": False, "task": task_id,
                        "reason": f"{verb} result failed validation: {report}",
                    }, f"{verb} failed validation")

                message = (f'revert: undo {target[:8]} "{subject}"' if kind == "revert"
                           else f'reapply: {target[:8]} "{subject}"')
                sha = versioning.commit_staging(
                    staging, message, task_id=task_id, origin=kind,
                    reverts=target if kind == "revert" else None,
                    reapplies=target if kind == "reapply" else None,
                )
                if not sha:
                    return finish("failed", {
                        "ok": False, "task": task_id,
                        "reason": f"no resulting changes — {name} is already "
                                  + ("reverted" if kind == "revert" else "applied"),
                    }, "no changes")
                state_store.audit("version_committed", task=task_id, version=sha[:12],
                                  message=message, op=kind, target=target[:12])
                emit({"kind": "committed", "summary": f"{sha[:8]} {message}"})

                # A revert must not be blocked by the checks guarding the very version it
                # removes — exclude them for the run, then lifecycle-disable them below.
                reboot = await self.reboot_to_version(
                    sha, reason=f"{kind} {target[:8]}", emit=emit,
                    exclude_origins={target} if kind == "revert" else frozenset())
                if reboot["ok"]:
                    registry.set_status(target, "reverted" if kind == "revert" else "reapplied",
                                        by=sha)
                    lifecycle = (checks.sync_lifecycle(disable_origins={target})
                                 if kind == "revert"
                                 else checks.sync_lifecycle(enable_origins={target}))
                    if lifecycle["disabled"] or lifecycle["enabled"]:
                        state_store.audit("checks_lifecycle", op=kind, version=target[:12],
                                          **lifecycle)
                emit({"kind": "reboot",
                      "summary": (f"promoted {sha[:8]}" if reboot["ok"]
                                  else f"rolled back: {reboot.get('reason')}")})
                return finish("done" if reboot["ok"] else "failed", {
                    "ok": reboot["ok"], "task": task_id, "version": sha, "short": sha[:8],
                    "promoted": reboot["ok"], "reason": reboot.get("reason"), "message": message,
                    ("reverts" if kind == "revert" else "reapplies"): target,
                }, "ok" if reboot["ok"] else "failed")
            finally:
                self.current_task_id = None
                self.current_task_kind = None

    # ── self-mod task progress: persisted to disk so the UI can recover it ─────────
    def _emit_event(self, task_id: str, ev: dict) -> None:
        """Stamp an event with a monotonic seq, persist it to the task's events.jsonl,
        and publish it to the live bus. Persistence is what lets the UI replay a run
        after a tab switch / reload; the seq lets it de-dupe replay against the live
        stream."""
        self._task_seq += 1
        record = {"task": task_id, "seq": self._task_seq, "t": round(time.time(), 3), **ev}
        record.setdefault("kind", "step")
        try:
            state_store.append_task_event(task_id, record)
        except Exception:
            pass
        events.bus.publish(record)

    def _write_status(self, task_id: str, prompt: str, state: str,
                      started_at: float, result: dict | None = None) -> None:
        state_store.write_task_status(task_id, {
            "task_id": task_id, "prompt": prompt, "state": state,
            "started_at": started_at,
            "ended_at": None if state == "running" else time.time(),
            "result": result,
        })

    def _mark_interrupted_task(self) -> None:
        """A worker never survives a kernel restart, so any task still marked `running`
        at boot was interrupted. Surface that (its partial log is preserved) instead of
        leaving a zombie `running` task that the UI would wait on forever."""
        tid = state_store.current_task_id()
        if not tid:
            return
        status = state_store.read_task_status(tid)
        if status and status.get("state") == "running":
            status["state"] = "interrupted"
            status["ended_at"] = time.time()
            state_store.write_task_status(tid, status)
            # It was mid-run when the kernel died; drop it from the queue so the drainer
            # resumes only the never-started backlog, not a half-applied task.
            state_store.queue_remove(tid)

    def enqueue_steer(self, message: str) -> dict[str, Any]:
        """Queue a mid-run steering message for the worker (drained via /steer GET)."""
        message = (message or "").strip()
        if not self.current_task_id:
            return {"ok": False, "reason": "no task running"}
        if not message:
            return {"ok": False, "reason": "message required"}
        self.current_steer.append(message)
        self._emit_event(self.current_task_id, {"kind": "steer", "summary": message[:200]})
        state_store.audit("steer", task=self.current_task_id, message=message[:200])
        return {"ok": True}

    def drain_steer(self) -> list[str]:
        msgs = self.current_steer
        self.current_steer = []
        return msgs

    async def cancel(self) -> dict[str, Any]:
        """Abort the in-flight self-mod: kill the worker so the run unwinds without
        committing, freeing the lock for a new task."""
        task_id = self.current_task_id
        if not task_id:
            return {"ok": False, "reason": "no task running"}
        if self.current_task_kind in ("revert", "reapply"):
            return {"ok": False, "reason": "revert/re-apply runs are atomic and health-gated — "
                                           "let it finish, then roll back if needed"}
        if self.current_task_kind == "kernel":
            # No worker process to kill and the kernel driver never polls cancel_requested —
            # reporting "cancelled" here would be a lie while the run completes anyway.
            return {"ok": False, "reason": "a kernel change runs to commit and always holds for "
                                           "approval — reject the pending version instead"}
        self.cancel_requested = True
        proc = self.current_worker
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.terminate()
            for _ in range(20):  # up to ~2s for a graceful exit, then hard-kill
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
        self._emit_event(task_id, {"kind": "cancelled", "summary": "cancel requested by user"})
        state_store.audit("change_cancelled", task=task_id)
        return {"ok": True, "task": task_id}

    async def change_request(self, prompt: str, base_version: str | None = None,
                             resume_task: str | None = None,
                             line: str | None = None,
                             operator: bool | None = None) -> dict[str, Any]:
        """Enqueue a self-modification and return its final result.

        Requests are appended to a FIFO queue persisted under state/tasks/ and run one at
        a time by a single drainer, so a backlog is never dropped — and survives a kernel
        restart (the drainer resumes it on boot). The caller still receives the result of
        *its own* task via a per-task future, preserving the original synchronous contract.

        "Continue from a commit": `base_version` (any version ref) re-bases the agent's edits
        onto that exact version's tree; `resume_task` (the task_id that produced it) seeds the
        agent's conversation with that version's saved transcript. Both travel in the durable
        queue payload, so a continue is as restart-safe as any queued change.

        `line` targets a NAMED LINE instead of production: the agent edits from the line's
        tip, and on success the line ref + its preview env advance — the live app is never
        touched (promote the line explicitly when it's ready).
        """
        base = None
        if line:
            if base_version:
                return {"ok": False, "reason": "line and base_version are mutually exclusive "
                                               "(a line change always edits from the line tip)"}
            if versioning.line_tip(line) is None:
                return {"ok": False, "reason": f"unknown line '{line}'"}
        if base_version:
            base = versioning.resolve_version(base_version)
            if not base:
                return {"ok": False, "reason": f"unknown version {base_version}"}
        task_id = "t" + uuid.uuid4().hex[:10]
        self._write_status(task_id, prompt, "queued", time.time())
        payload: dict[str, Any] = {}
        if base:
            payload["base"] = base
        if resume_task:
            payload["resume_task"] = resume_task
        if line:
            payload["line"] = line
        if operator is not None:
            payload["operator"] = bool(operator)
        state_store.queue_enqueue(task_id, prompt, payload=payload or None)
        state_store.audit("change_request_queued", task=task_id, prompt=prompt[:300],
                          base=(base[:12] if base else None), resume_task=resume_task or None,
                          line=line or None)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[task_id] = fut
        self._ensure_drainer()
        return await fut

    def _ensure_drainer(self) -> None:
        """Start the queue drainer if it isn't already running. Safe to call repeatedly."""
        if self._drainer is None or self._drainer.done():
            self._drainer = asyncio.create_task(self._drain_queue())

    async def _drain_queue(self) -> None:
        """Run queued self-mod tasks FIFO, one at a time, until the queue is empty.

        There is no `await` between observing an empty queue and returning, so under
        asyncio's cooperative scheduling a concurrent enqueue + _ensure_drainer() can never
        slip past a drainer that is exiting — the backlog is always picked up."""
        while True:
            item = state_store.queue_peek()
            if item is None:
                return
            task_id = str(item.get("task_id") or "")
            prompt = item.get("prompt", "")
            kind = item.get("kind") or "change"  # older queues have no kind field
            try:
                if kind in ("revert", "reapply"):
                    target = str((item.get("payload") or {}).get("target") or "")
                    result = await self._run_git_op(task_id, kind, target)
                elif kind == "kernel":
                    result = await self._run_kernel_change(task_id, prompt)
                else:
                    payload = item.get("payload") or {}
                    result = await self._run_change_request(
                        task_id, prompt,
                        base=payload.get("base") or None,
                        resume_task=payload.get("resume_task") or None,
                        line=payload.get("line") or None,
                        trigger=payload.get("trigger") or None,
                        operator=payload.get("operator"),
                    )
            except Exception as exc:  # one bad task must never wedge the queue
                result = {"ok": False, "task": task_id, "reason": f"error: {exc}"}
                with contextlib.suppress(Exception):
                    self._write_status(task_id, prompt, "failed", time.time(), result)
                    state_store.audit("change_request_error", task=task_id, error=str(exc)[:300])
            state_store.queue_remove(task_id)
            fut = self._pending.pop(task_id, None)
            if fut is not None and not fut.done():
                fut.set_result(result)

    async def _run_change_request(self, task_id: str, prompt: str, base: str | None = None,
                                  resume_task: str | None = None,
                                  line: str | None = None,
                                  trigger: str | None = None,
                                  operator: bool | None = None) -> dict[str, Any]:
        """One self-modification: stage → agent edits → validate → commit → reboot.

        Serialized via task_lock (one at a time). The reboot uses its own lock, which is a
        *different* lock, so there is no re-entrancy/deadlock. Progress is persisted
        per-task (status.json + events.jsonl) so the UI can recover a run that outlived the
        tab/page that started it.

        `base` re-bases the staging clone on a specific version (continue-from-a-commit);
        `resume_task` seeds the agent's conversation from that version's saved transcript.
        `line` targets a named line: the change stages from the line's tip and, on success,
        advances the line ref + its preview env instead of rebooting production.
        `trigger` marks an AUTONOMOUS change (fired by kernel.triggers): its version holds
        for approval by default (see _should_hold) and is stamped origin="trigger".
        """
        async with self.task_lock:
            started_at = time.time()
            self.current_task_id = task_id
            self.current_task_kind = "change"
            self.current_worker = None
            self.cancel_requested = False
            self.current_steer = []
            self._task_seq = 0
            state_store.set_current_task(task_id)
            self._write_status(task_id, prompt, "running", started_at)

            emit = lambda ev: self._emit_event(task_id, ev)  # noqa: E731

            def finish(state: str, result: dict, done_summary: str) -> dict[str, Any]:
                self._write_status(task_id, prompt, state, started_at, result)
                emit({"kind": "done", "summary": done_summary, "result": result})
                return result

            state_store.audit("change_request", task=task_id, prompt=prompt[:300])
            emit({"kind": "request", "summary": prompt[:200]})

            if line is not None:
                tip = versioning.line_tip(line)
                if tip is None:  # deleted while this task sat in the queue
                    return finish("failed", {
                        "ok": False, "task": task_id,
                        "reason": f"line '{line}' no longer exists", "steps": [],
                    }, "line deleted")
                base = tip  # a line change always edits from the line's tip

            staging = versioning.create_staging(task_id, base=base)
            self.current_staging = staging  # the /validate syscall validates this
            try:
                result = await agent_runtime.run_task(
                    task_id, prompt, staging, self.config, emit=emit,
                    set_worker=lambda p: setattr(self, "current_worker", p),
                    is_cancelled=lambda: self.cancel_requested,
                    resume_task=resume_task,
                )
            finally:
                self.current_staging = None
                self.current_worker = None

            try:
                if self.cancel_requested:
                    state_store.audit("agent_cancelled", task=task_id)
                    with contextlib.suppress(Exception):
                        shutil.rmtree(staging, ignore_errors=True)
                    return finish("cancelled", {
                        "ok": False, "task": task_id, "cancelled": True,
                        "reason": "cancelled by user", "steps": result["steps"],
                    }, "cancelled")

                if not result["proposed"]:
                    state_store.audit("agent_no_proposal", task=task_id)
                    return finish("failed", {
                        "ok": False, "task": task_id,
                        "reason": "agent did not propose a valid commit", "steps": result["steps"],
                    }, "no valid commit proposed")

                sha = versioning.commit_staging(
                    staging, result["message"], task_id=task_id, prompt=prompt,
                    origin="trigger" if trigger else "self-mod")
                if not sha:
                    state_store.audit("agent_no_changes", task=task_id)
                    return finish("failed", {
                        "ok": False, "task": task_id,
                        "reason": "no file changes to commit", "steps": result["steps"],
                    }, "no file changes to commit")
                state_store.audit("version_committed", task=task_id, version=sha[:12], message=result["message"])
                emit({"kind": "committed", "summary": f"{sha[:8]} {result['message']}"})

                # Verification Gate (opt-in): derive executable acceptance checks for THIS
                # request, ring-0, from the original prompt + the committed diff. Derivation
                # is fail-open unless verifier.strict (an outage must not block every
                # self-mod); the checks themselves are enforced inside reboot_to_version.
                acceptance: list[dict[str, Any]] = []
                verification_note: str | None = None
                if verifier.enabled(self.config):
                    derived = await verifier.derive_checks(
                        prompt, versioning.diff(sha), self.config)
                    if derived["ok"] and derived["checks"]:
                        acceptance = derived["checks"]
                        names = ", ".join(str(c.get("name")) for c in acceptance)
                        state_store.audit("verify_derived", task=task_id, version=sha[:12],
                                          count=len(acceptance))
                        emit({"kind": "verify_derive",
                              "summary": f"derived {len(acceptance)} acceptance check(s): {names}"[:300]})
                    elif derived["ok"]:  # skipped: nothing verifiable over HTTP
                        verification_note = derived["reason"]
                        state_store.audit("verify_skipped", task=task_id, version=sha[:12],
                                          reason=derived["reason"][:200])
                        emit({"kind": "verify_derive",
                              "summary": f"no acceptance checks: {derived['reason']}"[:200]})
                    elif verifier.strict(self.config):
                        state_store.audit("verify_derive_failed", task=task_id, version=sha[:12],
                                          reason=derived["reason"][:300], strict=True)
                        registry.set_status(sha, "verify_failed", verification={
                            "unverified": True, "reason": derived["reason"]})
                        return finish("failed", {
                            "ok": False, "task": task_id, "version": sha, "short": sha[:8],
                            "promoted": False, "steps": result["steps"],
                            "reason": f"check derivation failed (verifier.strict): {derived['reason']}",
                        }, "verification derivation failed")
                    else:
                        verification_note = f"derivation failed: {derived['reason']}"
                        state_store.audit("verify_derive_failed", task=task_id, version=sha[:12],
                                          reason=derived["reason"][:300], strict=False)
                        emit({"kind": "verify_derive",
                              "summary": f"promoting unverified — {verification_note}"[:200]})

                # Agent evals (opt-in): when the diff touches the agent's own brain
                # (evals.paths, default runtime/), the CANDIDATE's runtime must still pass
                # the held-out benchmark before this change ships anywhere — prod, a hold,
                # or a line. The paper's "no degradation on held-out tasks": health proves
                # it boots, verification proves the app behaves, evals prove the AGENT
                # still works. A failed task always blocks; infra failures follow
                # evals.strict (see kernel/evals.py).
                if evals.enabled(self.config):
                    ev = await evals.maybe_run(sha, self.config, emit=emit)
                    if ev.get("skipped"):
                        emit({"kind": "eval", "summary": f"evals skipped: {ev['reason']}"[:200]})
                    elif ev["ok"]:
                        emit({"kind": "eval", "summary": f"agent evals: {ev['reason']}"[:200]})
                    else:
                        registry.set_status(sha, "eval_failed", evals=ev.get("report"))
                        state_store.audit("evals_failed", task=task_id, version=sha[:12],
                                          reason=ev["reason"][:300])
                        emit({"kind": "eval", "summary": f"agent evals FAILED: {ev['reason']}"[:250]})
                        return finish("failed", {
                            "ok": False, "task": task_id, "version": sha, "short": sha[:8],
                            "promoted": False, "steps": result["steps"],
                            "reason": f"agent evals failed: {ev['reason']}",
                        }, "agent evals failed")

                # Line target: advance the line ref + its preview env instead of touching
                # production. The approval hold deliberately does NOT apply — a line IS the
                # review environment (promoting the line to prod is the approval moment).
                if line is not None:
                    return await self._advance_line(task_id, line, sha, acceptance,
                                                    verification_note, result, emit, finish)

                # Governance gate: hold the committed version for approval instead of
                # promoting it. It lives in history as a v_* branch — diffable via the /diff
                # syscall and listed in Versions — so it can be reviewed BEFORE it ever goes
                # live. The active app is untouched until approve_version() runs. Off by
                # default for human changes (agent.require_approval); ON by default for
                # AUTONOMOUS (trigger-fired) ones (see _should_hold).
                if self._should_hold(trigger, operator):
                    state_store.add_pending({
                        "sha": sha, "short": sha[:8], "task": task_id,
                        "message": result["message"], "created_at": time.time(),
                        # Approve-time verification uses the checks derived NOW (see
                        # approve_version); prompt rides along for check provenance.
                        "checks": acceptance, "verification_note": verification_note,
                        "prompt": prompt, "trigger": trigger,
                    })
                    registry.set_status(sha, "pending")
                    state_store.audit("promotion_pending", task=task_id, version=sha[:12],
                                      trigger=trigger or None)
                    summary = (f"autonomous change awaiting approval: {sha[:8]}" if trigger
                               else f"awaiting approval: {sha[:8]}")
                    emit({"kind": "pending", "summary": summary})
                    return finish("pending", {
                        "ok": True, "task": task_id, "version": sha, "short": sha[:8],
                        "promoted": False, "pending": True, "message": result["message"],
                        "steps": result["steps"], "trigger": trigger,
                    }, "pending approval")

                reboot = await self.reboot_to_version(
                    sha, reason=f"self-mod {task_id}", emit=emit,
                    acceptance=acceptance, verification_note=verification_note,
                    freeze_meta={"task": task_id, "prompt": prompt})
                emit({"kind": "reboot",
                      "summary": f"promoted {sha[:8]}" if reboot["ok"] else f"rolled back: {reboot.get('reason')}"})
                return finish("done" if reboot["ok"] else "failed", {
                    "ok": reboot["ok"], "task": task_id, "version": sha, "short": sha[:8],
                    "promoted": reboot["ok"], "reason": reboot.get("reason"),
                    "message": result["message"], "steps": result["steps"],
                }, "ok" if reboot["ok"] else "failed")
            finally:
                self.current_task_id = None
                self.current_task_kind = None

    async def _advance_line(self, task_id: str, line: str, sha: str,
                            acceptance: list[dict[str, Any]],
                            verification_note: str | None,
                            result: dict[str, Any],
                            emit: Callable[[dict], None],
                            finish: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        """Promote a committed change onto its LINE: boot the candidate into the line's
        preview slot (blue-green — the old preview serves until the new one is healthy),
        run the Verification Gate against it, and only then advance the line ref. The
        production app is never touched; a failing candidate leaves both the line ref and
        the running preview exactly as they were. Acceptance checks are NOT frozen into
        the regression suite — freezing is a prod-promotion event."""
        emit({"kind": "reboot", "summary": f"booting candidate into line '{line}' preview"})
        if verifier.enabled(self.config):
            emit({"kind": "verify", "summary": "running verification checks"})
        res = await self.previews.create(line, sha, line=line, replace=True,
                                         verify=self._gate_for_preview(acceptance))
        if not res.get("ok"):
            if res.get("stage") == "verify":
                registry.set_status(sha, "verify_failed",
                                    verification=res.get("verification"))
                emit({"kind": "verify_failed", "summary": str(res.get("reason"))[:200]})
            else:
                registry.set_status(sha, "health_failed", health={
                    "ok": False, "reason": res.get("reason")})
            return finish("failed", {
                "ok": False, "task": task_id, "version": sha, "short": sha[:8],
                "promoted": False, "line": line, "reason": res.get("reason"),
                "steps": result["steps"],
            }, f"line '{line}' candidate rejected")

        versioning.set_line(line, sha)
        verification = res.get("verification") or (
            {"unverified": True, "reason": verification_note} if verification_note else None)
        registry.set_status(sha, "line_promoted", line=line, verification=verification)
        state_store.audit("line_advanced", line=line, version=sha[:12], task=task_id)
        emit({"kind": "line", "summary": f"line '{line}' → {sha[:8]} (preview updated)"})
        return finish("done", {
            "ok": True, "task": task_id, "version": sha, "short": sha[:8],
            "promoted": False, "line": line, "line_promoted": True,
            "preview_url": f"/preview/{line}", "message": result["message"],
            "steps": result["steps"],
        }, f"line '{line}' updated")

    async def _reap_previews_loop(self) -> None:
        """Periodically stop previews nobody has touched (previews.idle_minutes)."""
        while True:
            await asyncio.sleep(60)
            with contextlib.suppress(Exception):
                await self.previews.reap_idle()

    # ── autonomous triggers ───────────────────────────────────────────────────────────
    async def _trigger_tick_loop(self) -> None:
        """Evaluate autonomous triggers on a fixed cadence. The manager itself no-ops while
        the master switch is off, so this loop is cheap when the feature is unused."""
        while True:
            await asyncio.sleep(30)
            with contextlib.suppress(Exception):
                await self.triggers.tick()

    async def _fetch_active_errors(self) -> list[dict] | None:
        """The active app's unresolved error groups (for error_spike triggers). Polls the
        live slot directly, like the watchdog — defensive: any failure or an app whose
        /api/errors a self-mod reshaped just yields None (the trigger stays quiet)."""
        handle = self.current
        if handle is None or not handle.alive():
            return None
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"http://127.0.0.1:{handle.port}/api/errors",
                                params={"include_resolved": "false"})
            if r.status_code != 200:
                return None
            groups = r.json().get("groups")
            return groups if isinstance(groups, list) else None
        except Exception:
            return None

    async def _fetch_advisor_queue(self) -> list[dict] | None:
        """The active app's Advisor auto-queue (for advisor triggers): open improvement
        proposals as ready-to-fire prompts. Ring 3 proposes; ring 0 decides when filing is
        allowed. Defensive like the error poll — any failure (or an app without the
        Advisor plugin) just yields None and the trigger stays quiet."""
        handle = self.current
        if handle is None or not handle.alive():
            return None
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    f"http://127.0.0.1:{handle.port}/api/plugins/advisor/auto_queue")
            if r.status_code != 200:
                return None
            rows = r.json().get("proposals")
            return rows if isinstance(rows, list) else None
        except Exception:
            return None

    def _enqueue_autonomous(self, prompt: str, *, trigger_id: str | None,
                            trigger_name: str | None) -> str:
        """Fire-and-forget enqueue of a trigger-fired self-mod onto the durable FIFO (no
        caller awaits it). The `trigger` payload marks it autonomous so _run_change_request
        applies the hold-for-approval posture; provenance rides to the registry."""
        task_id = "t" + uuid.uuid4().hex[:10]
        self._write_status(task_id, prompt, "queued", time.time())
        state_store.queue_enqueue(task_id, prompt, payload={
            "trigger": trigger_id, "trigger_name": trigger_name})
        state_store.audit("change_request_queued", task=task_id, prompt=prompt[:300],
                          trigger=trigger_id)
        self._ensure_drainer()
        return task_id

    async def shutdown(self) -> None:
        if self._monitor is not None and not self._monitor.done():
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._monitor
        if self._drainer is not None and not self._drainer.done():
            self._drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._drainer
        if self._preview_reaper is not None and not self._preview_reaper.done():
            self._preview_reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._preview_reaper
        if self._trigger_loop is not None and not self._trigger_loop.done():
            self._trigger_loop.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._trigger_loop
        await self.previews.stop_all()
        await bootloader.stop(self.current)

    def dequeue(self, task_id: str) -> dict[str, Any]:
        """Remove a still-QUEUED task from the backlog (the running task can't be
        dequeued — cancel it instead). Its waiting caller resolves immediately."""
        task_id = (task_id or "").strip()
        if not task_id:
            return {"ok": False, "reason": "task_id required"}
        if task_id == self.current_task_id:
            return {"ok": False, "reason": "task is already running — cancel it instead"}
        if not any(it.get("task_id") == task_id for it in state_store.read_queue()):
            return {"ok": False, "reason": f"no queued task {task_id}"}
        state_store.queue_remove(task_id)
        status = state_store.read_task_status(task_id) or {}
        self._write_status(task_id, status.get("prompt", ""), "cancelled",
                           status.get("started_at") or time.time(),
                           {"ok": False, "task": task_id, "dequeued": True,
                            "reason": "removed from queue before it started"})
        state_store.audit("dequeued", task=task_id)
        fut = self._pending.pop(task_id, None)
        if fut is not None and not fut.done():
            fut.set_result({"ok": False, "task": task_id, "dequeued": True,
                            "reason": "removed from queue before it started"})
        return {"ok": True, "task": task_id, "dequeued": True}

    # ── introspection ─────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        cur = self.current
        queue = state_store.read_queue()
        return {
            "booting": self.booting,
            "active": None
            if cur is None
            else {
                "version": cur.version,
                "short": cur.version[:8],
                "slot": cur.slot,
                "port": cur.port,
                "pid": cur.pid,
                "alive": cur.alive(),
            },
            "slots": state_store.read_slots(),
            "versions_count": versioning.count_versions(),
            "previews": self.previews.list(),
            "lines": [ln["name"] for ln in self.list_lines()],
            "current_task": self.current_task_id,
            "current_task_kind": self.current_task_kind,
            "queue_depth": len(queue),
            "queue": [{"task_id": it.get("task_id"), "prompt": (it.get("prompt") or "")[:200],
                       "kind": it.get("kind") or "change", "enqueued_at": it.get("enqueued_at")}
                      for it in queue[:20]],
        }
