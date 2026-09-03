"""Agent evals (opt-in): held-out benchmark tasks that gate the agent's own brain.

The last piece of the self-improvement loop. Health checks prove a candidate BOOTS and
the Verification Gate proves the APP behaves — but neither proves the candidate's
self-mod AGENT still works after a change to `runtime/` (its loop, tools, prompt,
engines). This module does: when a candidate's diff touches `evals.paths` (default
`runtime/`), the kernel runs every enabled benchmark task by spawning the CANDIDATE's
own runtime — no recovery fallback, that would mask a broken brain — against a throwaway
staging of the candidate tree, and requires each run to propose a change that passes the
authoritative validation gate (`agent_runtime.validate_staging`). "Promote only if the
harness still performs on held-out tasks."

Benchmark tasks live in protected `state/evals.json` (via kernel/state_store.py), so the
mutable app can never water down the very benchmark that gates its runtime. Failure
policy mirrors the verifier: a task the candidate FAILS always blocks (registry status
`eval_failed`); an infrastructure failure (staging couldn't be created, …) fails open
unless `evals.strict` — an environment hiccup must not block every runtime change.

Offline: with the scripted engine each eval worker runs the deterministic scripted
driver, so the whole gate is exercised keylessly (a broken candidate runtime fails to
emit `ready` → the task fails → the change is rejected while production stays healthy).

Eval workers are hermetic: keyless (stripped env, inference via /llm_call like any
worker), pointed at a throwaway data dir (a benchmark run must not pollute the live
self-mod conversation), and killed on a per-task deadline.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from typing import Any

from kernel import agent_runtime, state_store, versioning

_NAME_MAX = 80
_PROMPT_MAX = 4000


# ── config ──────────────────────────────────────────────────────────────────────────────
def _cfg(config: dict) -> dict:
    return config.get("evals", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", False))


def strict(config: dict) -> bool:
    return bool(_cfg(config).get("strict", False))


def scope_paths(config: dict) -> list[str]:
    paths = _cfg(config).get("paths")
    if not isinstance(paths, list):
        return ["runtime/"]
    return [str(p) for p in paths if str(p).strip()]


def task_timeout(config: dict) -> float:
    return float(_cfg(config).get("timeout_seconds", 600))


def touches_scope(sha: str, paths: list[str]) -> bool:
    """Whether a version's commit touched the eval scope. Empty scope = every change."""
    if not paths:
        return True
    changed = versioning.changed_paths(sha)
    return any(f.startswith(p) for f in changed for p in paths)


# ── the protected benchmark store (state/evals.json) ────────────────────────────────────
def list_tasks() -> list[dict[str, Any]]:
    return state_store.read_eval_store()


def enabled_tasks() -> list[dict[str, Any]]:
    return [t for t in state_store.read_eval_store() if t.get("enabled", True)]


def upsert_task(spec: dict) -> tuple[bool, str, dict | None]:
    """Create (no id) or update (existing id) a benchmark task. Operator-only surface —
    reached via the syscall boundary, never handed to the agent's tools."""
    if not isinstance(spec, dict):
        return False, "task must be an object", None
    name = str(spec.get("name") or "").strip()
    prompt = str(spec.get("prompt") or "").strip()
    if not name or len(name) > _NAME_MAX:
        return False, f"name must be 1..{_NAME_MAX} chars", None
    if not prompt or len(prompt) > _PROMPT_MAX:
        return False, f"prompt must be 1..{_PROMPT_MAX} chars", None
    tasks = state_store.read_eval_store()
    tid = str(spec.get("id") or "").strip()
    fields = {"name": name, "prompt": prompt, "enabled": bool(spec.get("enabled", True))}
    if tid:
        entry = next((t for t in tasks if t.get("id") == tid), None)
        if entry is None:
            return False, f"unknown eval task {tid}", None
        entry.update(fields)
    else:
        entry = {"id": "ev" + uuid.uuid4().hex[:10], **fields,
                 "created_at": round(time.time(), 3), "last_result": None}
        tasks.append(entry)
    state_store.write_eval_store(tasks)
    state_store.audit("eval_task_saved", eval=entry["id"], name=name,
                      enabled=entry["enabled"])
    return True, "", entry


def delete_task(task_id: str) -> bool:
    tasks = state_store.read_eval_store()
    kept = [t for t in tasks if t.get("id") != task_id]
    if len(kept) == len(tasks):
        return False
    state_store.write_eval_store(kept)
    state_store.audit("eval_task_deleted", eval=task_id)
    return True


def set_task_enabled(task_id: str, on: bool) -> bool:
    tasks = state_store.read_eval_store()
    entry = next((t for t in tasks if t.get("id") == task_id), None)
    if entry is None:
        return False
    entry["enabled"] = bool(on)
    state_store.write_eval_store(tasks)
    state_store.audit("eval_task_toggled", eval=task_id, enabled=bool(on))
    return True


def _stamp_result(task_id: str, ok: bool, detail: str, sha: str) -> None:
    tasks = state_store.read_eval_store()
    entry = next((t for t in tasks if t.get("id") == task_id), None)
    if entry is not None:
        entry["last_result"] = {"ok": ok, "detail": detail[:300],
                                "version": sha[:12], "ts": round(time.time(), 3)}
        state_store.write_eval_store(tasks)


# ── the runner ──────────────────────────────────────────────────────────────────────────
async def _run_one(task: dict, sha: str, candidate_dir, config: dict) -> tuple[bool, str]:
    """One benchmark task against candidate `sha`: spawn the candidate's runtime on a
    fresh staging of the candidate tree; it must emit ready, propose, and validate."""
    eval_id = "eval_" + uuid.uuid4().hex[:8]
    staging = versioning.create_staging(eval_id, base=sha)
    tmp_data = staging.parent / "data"
    tmp_data.mkdir(parents=True, exist_ok=True)
    try:
        spec = {"label": "candidate", "cwd": str(candidate_dir),
                "argv": [sys.executable, "-u", "-m", "runtime"]}
        env = agent_runtime._worker_env(staging, eval_id, task["prompt"], config,
                                        cwd=str(candidate_dir))
        env["QUINE_DATA_DIR"] = str(tmp_data)  # hermetic: never touch the live convo/notes

        holder: dict[str, Any] = {}
        try:
            result = await asyncio.wait_for(
                agent_runtime._run_worker(spec, env, relay=lambda ev: None,
                                          set_worker=lambda p: holder.update(proc=p)),
                timeout=task_timeout(config))
        except asyncio.TimeoutError:
            proc = holder.get("proc")
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return False, f"timed out after {int(task_timeout(config))}s"

        if not result["ready"]:
            # Deliberately NO recovery fallback here: a candidate whose runtime cannot
            # even start has a broken brain — that is exactly what this gate catches.
            return False, "candidate runtime failed to start"
        if not result["proposed"]:
            return False, "candidate runtime did not propose a change"
        try:
            # to_thread like every other validate_staging call site (core, gateway): the
            # candidate's pytest run takes tens of seconds and must not block the kernel's
            # event loop — the gateway (same process) still has to proxy /health meanwhile.
            ok, report = await asyncio.to_thread(agent_runtime.validate_staging, staging)
        except Exception as exc:
            return False, f"validation crashed: {exc}"
        if not ok:
            return False, f"proposal failed validation: {report[:200]}"
        return True, "passed"
    finally:
        versioning._force_rmtree(staging.parent)


async def run_evals(sha: str, config: dict, emit=None) -> dict[str, Any]:
    """Run every enabled benchmark task against candidate `sha`. Returns
    {ok, total, passed, results: [{id, name, ok, detail}]} — ok gates promotion."""
    tasks = enabled_tasks()
    results: list[dict[str, Any]] = []
    eval_root = state_store.TASKS_DIR / ("eval_cand_" + uuid.uuid4().hex[:8])
    candidate_dir = eval_root / "candidate"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    versioning.deploy(sha, candidate_dir)  # the runtime under test, materialized once
    try:
        for task in tasks:
            if emit is not None:
                try:
                    emit({"kind": "eval", "summary": f"eval: {task.get('name')}"})
                except Exception:
                    pass
            ok, detail = await _run_one(task, sha, candidate_dir, config)
            _stamp_result(str(task.get("id")), ok, detail, sha)
            results.append({"id": task.get("id"), "name": task.get("name"),
                            "ok": ok, "detail": detail[:300]})
    finally:
        versioning._force_rmtree(eval_root)
    passed = sum(1 for r in results if r["ok"])
    return {"ok": passed == len(results), "total": len(results), "passed": passed,
            "results": results}


async def maybe_run(sha: str, config: dict, emit=None) -> dict[str, Any]:
    """The promotion-gate entry point: scope-check the candidate's diff, then run the
    benchmark. Returns {ok, skipped, reason, report} — ok=False must block the change."""
    paths = scope_paths(config)
    if not touches_scope(sha, paths):
        return {"ok": True, "skipped": True,
                "reason": f"diff outside eval scope ({', '.join(paths)})", "report": None}
    tasks = enabled_tasks()
    if not tasks:
        return {"ok": True, "skipped": True, "reason": "no enabled eval tasks", "report": None}
    try:
        report = await run_evals(sha, config, emit=emit)
    except Exception as exc:  # infra failure — fail open unless strict
        reason = f"eval run could not execute: {type(exc).__name__}: {exc}"
        state_store.audit("evals_error", version=sha[:12], reason=reason[:300],
                          strict=strict(config))
        if strict(config):
            return {"ok": False, "skipped": False, "reason": reason, "report": None}
        return {"ok": True, "skipped": False, "reason": reason, "report": None}
    if report["ok"]:
        state_store.audit("evals_passed", version=sha[:12],
                          passed=report["passed"], total=report["total"])
        return {"ok": True, "skipped": False,
                "reason": f"passed {report['passed']}/{report['total']}", "report": report}
    failed = [r for r in report["results"] if not r["ok"]]
    first = failed[0] if failed else {}
    reason = (f"{len(failed)}/{report['total']} benchmark task(s) failed — "
              f"{first.get('name')}: {first.get('detail')}")
    return {"ok": False, "skipped": False, "reason": reason, "report": report}
