"""Agent runtime supervisor (immutable).

The agent's actual brain — its loop, tools, prompt, engines — is NOT here anymore. It
lives in the versioned, agent-editable `runtime/` package of the system image. This
module only:
  • spawns that runtime as a KEYLESS subprocess worker (no provider secrets in its env;
    it does inference via the /llm_call syscall),
  • relays the worker's stdout events to the live log,
  • falls back to the immutable `recovery_runtime` if the editable runtime won't start,
  • runs the authoritative validation backstop and returns the proposal.

Keeping the worker keyless + isolated is what lets the agent rewrite *how it operates*
freely while the recovery substrate (this file, versioning, watchdog) stays immutable.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any

from kernel import policy, state_store, versioning
from kernel.util import CHILD_CREATIONFLAGS

_MAX_OUTPUT = 8000

# A self-mod is a careful, near-deterministic edit task: pin a low temperature for it,
# regardless of the user-facing `agent.temperature` (which the in-app Run agent uses).
# Injected into the worker config below, so it covers both the editable runtime and the
# immutable recovery runtime without either needing to know about it.
SELFMOD_TEMPERATURE = 0.1


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n…[truncated {len(text) - _MAX_OUTPUT} chars]"


# ── validation gate (authoritative; the worker also calls this via /validate) ────────
def _build_if_needed(staging: pathlib.Path) -> tuple[bool, str]:
    """If the app declares a build (app_manifest.json) and this change touched the
    build dir, run it so a broken frontend fails BEFORE commit. Fails closed if the
    build tool is missing for a frontend change; skips quietly for non-frontend edits."""
    try:
        manifest = json.loads((staging / "app_manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return True, ""  # missing/invalid manifest is caught by policy.check_staging
    build = manifest.get("build")
    build_dir = manifest.get("build_dir")
    if not build or not build_dir:
        return True, ""

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(staging), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=versioning._env(),
            creationflags=CHILD_CREATIONFLAGS,
        ).stdout
    except Exception:
        status = ""
    prefix = build_dir.rstrip("/") + "/"
    touched = any(
        ln[3:].strip().strip('"').startswith(prefix)
        for ln in status.splitlines() if len(ln) > 3
    )
    if not touched:
        return True, ""

    tool = (shlex.split(build) or [""])[0]
    if shutil.which(tool) is None:
        return False, (
            f"frontend changed but build tool '{tool}' is not installed; cannot "
            "validate the UI build (install Node/npm, or revert the frontend change)."
        )
    cache = state_store.STATE_DIR / ".npm-cache"
    cache.mkdir(parents=True, exist_ok=True)
    # The build command + scripts come from the agent-controlled staging tree (manifest
    # `build`, package.json/vite scripts), so run them with secrets stripped — a malicious
    # build must not be able to read provider keys out of the environment.
    env = {**state_store.stripped_env(), "npm_config_cache": str(cache)}
    try:
        res = subprocess.run(
            build, shell=True, cwd=str(staging / build_dir),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, env=env, creationflags=CHILD_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired:
        return False, "frontend build timed out"
    except Exception as exc:  # never let a build hiccup escape into the pipeline as a 500
        return False, f"frontend build could not run: {exc}"
    if res.returncode != 0:
        return False, "Frontend build failed:\n" + _truncate(res.stdout + "\n" + res.stderr)
    serve_root = manifest.get("serve_root")
    if serve_root and not (staging / serve_root).exists():
        return False, f"build did not produce expected output: {serve_root}"
    return True, "build ok"


def _run_app_tests(staging: pathlib.Path, env: dict) -> tuple[bool, str]:
    """Run the candidate's OWN test suite (`tests/` inside the app tree — seeded with
    app/tests/test_smoke.py, extendable by the agent) as part of the pre-commit gate.
    `-o addopts=` is load-bearing: staging lives under the repo root, so pytest's rootdir
    discovery would otherwise inherit the repository's low-memory suite `testpaths` and run the
    WRONG tests instead of the candidate's own smoke tests. Quiet skip when the tree has no
    tests/ dir (a version may legitimately drop it; behavior is still health/verify-gated)."""
    if not (staging / "tests").is_dir():
        return True, ""
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-x", "-q",
             "-o", "addopts=", "-p", "no:cacheprovider"],
            cwd=str(staging), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=120,
            creationflags=CHILD_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired:
        return False, "App tests timed out after 120s"
    except Exception as exc:  # pytest missing/unlaunchable is an infra gap, not a bad version
        return True, f"app tests skipped ({exc})"
    if res.returncode != 0:
        return False, "App tests failed:\n" + _truncate(res.stdout + "\n" + res.stderr)
    return True, "app tests ok"


def _validate(staging: pathlib.Path) -> tuple[bool, str]:
    """Pre-commit gate: syntax-compile all .py, import main, app tests, policy, build."""
    py_files = [str(p) for p in staging.rglob("*.py") if "__pycache__" not in p.parts]
    # Secrets are stripped from every step here: py_compile / `import main` / pytest
    # execute agent-authored staging code, and must not run with provider keys in the
    # environment.
    stripped = state_store.stripped_env()
    if py_files:
        res = subprocess.run(
            [sys.executable, "-m", "py_compile", *py_files],
            cwd=str(staging), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=stripped,
            creationflags=CHILD_CREATIONFLAGS,
        )
        if res.returncode != 0:
            return False, "Syntax errors:\n" + _truncate(res.stderr)

    env = {**stripped, "PYTHONPATH": str(staging)}
    res = subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=str(staging), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
        creationflags=CHILD_CREATIONFLAGS,
    )
    if res.returncode != 0:
        return False, "Importing main failed:\n" + _truncate(res.stderr)

    ok, report = _run_app_tests(staging, env)
    if not ok:
        return False, report

    ok, errors = policy.check_staging(staging)
    if not ok:
        return False, "Policy errors:\n" + "\n".join(errors)

    built, report = _build_if_needed(staging)
    if not built:
        return False, report
    return True, "validation passed"


# Public name for callers outside the worker pipeline (the /validate syscall and the
# kernel's revert/re-apply ops run the exact same authoritative gate).
validate_staging = _validate


# ── worker supervision ───────────────────────────────────────────────────────────────
def _active_runtime_dir() -> pathlib.Path | None:
    """The editable runtime/ of the currently active (last-good) version, if present."""
    slot = state_store.read_slots().get("active_slot")
    if not slot:
        return None
    rt = state_store.SLOTS_DIR / slot / "runtime"
    return rt if (rt / "__main__.py").exists() else None


def _runtime_specs(active_runtime: pathlib.Path | None) -> list[dict]:
    """Worker launch specs to try in order: the editable runtime, then recovery."""
    specs: list[dict] = []
    if active_runtime is not None:
        slot_dir = active_runtime.parent
        specs.append({"label": "runtime", "cwd": str(slot_dir),
                      "argv": [sys.executable, "-u", "-m", "runtime"]})
    specs.append({"label": "recovery", "cwd": str(state_store.ROOT),
                  "argv": [sys.executable, "-u", str(state_store.ROOT / "kernel" / "recovery_runtime.py")]})
    return specs


def _worker_env(staging: pathlib.Path, task_id: str, prompt: str, config: dict, cwd: str,
                resume_task: str | None = None) -> dict:
    env = state_store.stripped_env()  # keyless: the worker never sees provider keys
    kcfg = config.get("kernel", {})
    # Force the self-mod temperature on a copy (never mutate the kernel's live config).
    worker_cfg = json.loads(json.dumps(config))
    worker_cfg.setdefault("agent", {})["temperature"] = SELFMOD_TEMPERATURE
    env["QUINE_STAGING_DIR"] = str(staging)
    env["QUINE_SYSCALL_URL"] = f"http://{kcfg.get('host', '127.0.0.1')}:{kcfg.get('port', 8000)}/api/syscall"
    env["QUINE_DATA_DIR"] = str(state_store.DATA_DIR)
    env["QUINE_TASK_ID"] = task_id
    env["QUINE_TASK_PROMPT"] = prompt
    env["QUINE_CONFIG"] = json.dumps(worker_cfg)
    env["PYTHONPATH"] = cwd
    # Continue-from-a-commit: point the worker at the saved transcript (in app-owned data/) of
    # the version being continued, so it resumes that conversation as full context. Only set
    # when a snapshot actually exists — a missing one just means a fresh conversation.
    if resume_task:
        snap = state_store.DATA_DIR / "selfmod_convos" / f"{resume_task}.jsonl"
        if snap.exists():
            env["QUINE_RESUME_CONVO"] = str(snap)
    return env


async def _run_worker(spec: dict, env: dict, relay, set_worker=None) -> dict:
    """Spawn one worker and pump its stdout. Returns {ready, proposed, message}."""
    proc = subprocess.Popen(
        spec["argv"], cwd=spec["cwd"], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        encoding="utf-8", errors="replace", creationflags=CHILD_CREATIONFLAGS,
    )
    if set_worker is not None:
        try:
            set_worker(proc)  # let the kernel terminate this worker on cancel
        except Exception:
            pass
    ready = proposed = False
    message = "agent change"
    assert proc.stdout is not None  # created with stdout=PIPE
    while True:
        line = await asyncio.to_thread(proc.stdout.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            relay({"kind": "stdout", "summary": line[:160]})  # surface stray prints/tracebacks
            continue
        etype = ev.get("event")
        if etype == "ready":
            ready = True
        elif etype == "propose":
            proposed = True
            message = ev.get("message", "agent change")
            relay({"kind": "propose", "summary": message})
        elif etype == "step":
            # Forward the whole step (kind/summary/name/args/thought/…) so the live log
            # can show tool calls + reasoning, not just a terse one-liner.
            relay({k: v for k, v in ev.items() if k != "event"})
    await asyncio.to_thread(proc.wait)
    return {"ready": ready, "proposed": proposed, "message": message}


async def run_task(
    task_id: str, prompt: str, staging: pathlib.Path, config: dict, emit=None,
    set_worker=None, is_cancelled=None, resume_task: str | None = None
) -> dict[str, Any]:
    steps: list[dict] = []

    def relay(ev: dict) -> None:
        ev = {"kind": "step", **ev}
        steps.append({"t": round(time.time(), 3), **ev})
        if emit is not None:
            try:
                emit(ev)  # forward the full event (kind/summary/name/args/thought/…)
            except Exception:
                pass

    proposed = False
    message = "agent change"
    specs = _runtime_specs(_active_runtime_dir())
    for attempt, spec in enumerate(specs):
        env = _worker_env(staging, task_id, prompt, config, spec["cwd"], resume_task=resume_task)
        result = await _run_worker(spec, env, relay, set_worker=set_worker)
        if result["ready"]:
            proposed = result["proposed"]
            message = result["message"]
            break
        if is_cancelled is not None and is_cancelled():
            break  # a cancel killed the worker before it readied — don't fall back
        relay({"kind": "runtime_fallback",
               "summary": f"{spec['label']} runtime failed to start"
               + ("; using recovery" if attempt + 1 < len(specs) else "")})

    # Authoritative backstop: re-validate before we let core commit. Never let a
    # validation crash (e.g. a build subprocess error) escape as an unhandled 500 —
    # a failed validation is a normal "not proposed" outcome.
    if proposed:
        try:
            ok, report = _validate(staging)
        except Exception as exc:
            ok, report = False, f"validation crashed: {exc}"
        steps.append({"t": round(time.time(), 3), "kind": "propose_commit",
                      "ok": ok, "message": message, "report": report})
        if emit is not None:
            try:
                emit({"kind": "propose_commit", "summary": ("OK" if ok else "FAILED") + f": {message}"})
            except Exception:
                pass
        proposed = ok

    task_dir = state_store.TASKS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "transcript.json").write_text(
        json.dumps({"prompt": prompt, "proposed": proposed, "steps": steps}, indent=2),
        encoding="utf-8",
    )
    return {"proposed": proposed, "message": message, "steps": steps}
