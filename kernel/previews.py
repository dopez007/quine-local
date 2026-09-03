"""Preview environments: durable, addressable running copies of ANY version (ring 0).

Generalizes the ephemeral blue-green candidate into a first-class object: a *preview* is an
extra app process (beyond the A/B slots) running some version's tree in its own slot dir
(`slots/p_<name>`), reachable through the gateway by name (cookie routing — see
`kernel/gateway.py`). Use it to click around a pending candidate before approving it, to
keep a named experiment line runnable (see the line machinery in `kernel/core.py`), or to
inspect a variant in another browser tab.

Same safety posture as everything else in ring 0:
  • a preview only registers after passing the SAME watchdog health gate as a promotion
    candidate — a broken version never yields a "running" preview;
  • preview children are spawned by the bootloader, so they are keyless (secrets stripped)
    and their crash output is captured per slot;
  • previews are bounded (`previews.max`) and reaped when idle (`previews.idle_minutes`),
    so forgotten experiments can't accumulate into resource exhaustion;
  • previews do NOT survive a kernel restart (boot reaps stale `slots/p_*` dirs) — a line's
    preview is respawned on demand; the line itself lives in git and always survives.

Caveat (documented in INSTRUCTIONS): previews share the real `data/` partition with the
live app — isolated per-preview data is future work (workspace snapshots).
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from kernel import bootloader, state_store, watchdog
from kernel.bootloader import AppHandle

# Optional post-health gate for create(): given the candidate's port, return
# (ok, reason, report). Line promotion injects the Verification Gate through this seam,
# so previews.py itself stays free of verifier imports (and trivially unit-testable).
Verify = Callable[[int], Awaitable[tuple[bool, str, dict | None]]]

# Preview names become slot ids (p_<name>), cookie values, and URL path segments — keep
# them small and unambiguous. "exit" is the reserved leave-preview route.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
_RESERVED = frozenset({"exit"})
_SLOT_PREFIX = "p_"


@dataclass
class Preview:
    name: str
    handle: AppHandle
    version: str
    line: str | None       # set when this preview is the running tip of a named line
    slot: str              # which of the preview's two sub-slots is live ("a" | "b")
    created_at: float
    last_hit: float        # updated by the gateway on every routed request (idle reaping)


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or "")) and name not in _RESERVED


def slot_id(name: str, sub: str = "a") -> str:
    """Each preview owns TWO slot dirs (p_<name>-a / p_<name>-b) and flips between them on
    replace — exactly the A/B pattern of the main slots, so a replacement never deploys
    over the directory its predecessor is still running from."""
    return f"{_SLOT_PREFIX}{name}-{sub}"


def reap_stale_slot_dirs() -> int:
    """Delete leftover `slots/p_*` dirs from a previous kernel run (their processes died
    with that kernel; the dirs are plain deployed trees — no .git, nothing precious)."""
    removed = 0
    if not state_store.SLOTS_DIR.exists():
        return 0
    for path in state_store.SLOTS_DIR.glob(f"{_SLOT_PREFIX}*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


class PreviewManager:
    """The kernel's registry of running previews. All methods are called from the kernel's
    single event loop (creation is additionally serialized per-name by simple presence
    checks — the gateway only reads)."""

    def __init__(self, get_config: Callable[[], dict]) -> None:
        self._get_config = get_config
        self._previews: dict[str, Preview] = {}

    # ── config shortcuts ────────────────────────────────────────────────────────────
    def _cfg(self) -> dict:
        return self._get_config().get("previews", {}) or {}

    def max_previews(self) -> int:
        return int(self._cfg().get("max", 3))

    def idle_seconds(self) -> float:
        return float(self._cfg().get("idle_minutes", 120)) * 60

    # ── queries ─────────────────────────────────────────────────────────────────────
    def get(self, name: str) -> Preview | None:
        """The named preview, if it exists AND its process is alive (a dead one is
        forgotten on sight, so the gateway never routes into a corpse)."""
        p = self._previews.get(name)
        if p is None:
            return None
        if not p.handle.alive():
            p.handle.close_log()
            del self._previews[name]
            return None
        return p

    def for_line(self, line: str) -> Preview | None:
        return next((self.get(p.name) for p in list(self._previews.values())
                     if p.line == line), None)

    def touch(self, name: str) -> None:
        p = self._previews.get(name)
        if p is not None:
            p.last_hit = time.time()

    def list(self) -> list[dict[str, Any]]:
        out = []
        for name in sorted(self._previews):
            p = self.get(name)
            if p is None:
                continue
            out.append({
                "name": p.name, "version": p.version, "short": p.version[:8],
                "line": p.line, "port": p.handle.port, "pid": p.handle.pid,
                "created_at": p.created_at, "last_hit": p.last_hit,
                "url": f"/preview/{p.name}",
            })
        return out

    # ── lifecycle ───────────────────────────────────────────────────────────────────
    async def create(self, name: str, sha: str, *, line: str | None = None,
                     replace: bool = False, verify: Verify | None = None) -> dict[str, Any]:
        """Deploy + boot `sha` as preview `name`, health-gated. `replace=True` swaps an
        existing preview of the same name blue-green style: the old process keeps serving
        until the new one proves healthy (used by line promotion). `verify` runs after the
        health gate and before the swap — a failing candidate is stopped and the old
        preview keeps serving, exactly like a failed prod promotion."""
        if not valid_name(name):
            return {"ok": False, "reason": "preview name must be 1-24 chars of [a-z0-9-] "
                                           "(and not a reserved word)"}
        existing = self.get(name)
        if existing is not None and not replace:
            return {"ok": False, "reason": f"preview '{name}' already exists — stop it first"}
        alive = sum(1 for n in list(self._previews) if self.get(n) is not None)
        if existing is None and alive >= self.max_previews():
            return {"ok": False, "reason": f"preview limit reached ({self.max_previews()}) — "
                                           "stop one first (previews.max raises the cap)"}

        # Blue-green within the preview: boot the replacement in the OTHER sub-slot; the
        # old process keeps serving until the new one proves healthy.
        sub = "a" if existing is None else ("b" if existing.slot == "a" else "a")
        handle = await bootloader.start(sha, slot_id(name, sub),
                                        extra_env={"QUINE_PREVIEW_NAME": name})
        cfg = self._get_config()
        ok, why = await watchdog.wait_healthy(
            handle,
            cfg["app"]["health_path"],
            float(cfg["watchdog"]["health_timeout_seconds"]),
            float(cfg["watchdog"]["health_poll_interval"]),
        )
        if not ok:
            await bootloader.stop(handle)
            state_store.audit("preview_failed", name=name, version=sha[:12], reason=why,
                              log_tail=state_store.read_log_tail(slot_id(name, sub))[-2000:])
            return {"ok": False, "stage": "health",
                    "reason": f"preview failed health check: {why}"}

        verification: dict | None = None
        if verify is not None:
            try:
                v_ok, v_why, verification = await verify(handle.port)
            except Exception as exc:  # a crashed gate rejects the candidate, never the preview
                v_ok, v_why, verification = False, f"verification crashed: {exc}", None
            if not v_ok:
                await bootloader.stop(handle)
                state_store.audit("preview_verify_failed", name=name, version=sha[:12],
                                  reason=v_why[:300])
                return {"ok": False, "stage": "verify", "reason": v_why,
                        "verification": verification}

        if existing is not None:
            await bootloader.stop(existing.handle)
        now = time.time()
        self._previews[name] = Preview(name=name, handle=handle, version=sha, line=line,
                                       slot=sub, created_at=now, last_hit=now)
        state_store.audit("preview_started", name=name, version=sha[:12],
                          line=line or "", port=handle.port)
        return {"ok": True, "name": name, "version": sha, "short": sha[:8],
                "line": line, "port": handle.port, "url": f"/preview/{name}",
                "verification": verification}

    async def stop(self, name: str) -> dict[str, Any]:
        p = self._previews.pop(name, None)
        if p is None:
            return {"ok": False, "reason": f"no preview '{name}'"}
        await bootloader.stop(p.handle)
        for sub in ("a", "b"):
            shutil.rmtree(bootloader.slot_dir(slot_id(name, sub)), ignore_errors=True)
        state_store.audit("preview_stopped", name=name, version=p.version[:12])
        return {"ok": True, "name": name}

    async def stop_all(self) -> None:
        for name in list(self._previews):
            try:
                await self.stop(name)
            except Exception:
                pass

    async def reap_idle(self, now: float | None = None) -> list[str]:
        """Stop previews idle longer than previews.idle_minutes. Line previews are reaped
        too — the line survives in git and its preview respawns on the next line change
        (or an explicit preview request)."""
        now = time.time() if now is None else now
        idle_for = self.idle_seconds()
        reaped = []
        for name in list(self._previews):
            p = self.get(name)
            if p is not None and now - p.last_hit > idle_for:
                await self.stop(name)
                state_store.audit("preview_reaped", name=name, idle_minutes=round((now - p.last_hit) / 60))
                reaped.append(name)
        return reaped
