"""Worker SDK: the contract between this (keyless, isolated) runtime worker and the
kernel that supervises it.

- Inference + validation go through the kernel's syscalls (no provider keys here).
- Lifecycle/progress go to the kernel over stdout as JSON lines.
- Config + task come in via environment (so the worker also runs in offline unit tests
  where no syscall server is up).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

STAGING = pathlib.Path(os.environ["QUINE_STAGING_DIR"])
SYSCALL = os.environ.get("QUINE_SYSCALL_URL", "")
DATA_DIR = pathlib.Path(os.environ.get("QUINE_DATA_DIR", ""))
TASK_ID = os.environ.get("QUINE_TASK_ID", "")
PROMPT = os.environ.get("QUINE_TASK_PROMPT", "")
CONFIG = json.loads(os.environ.get("QUINE_CONFIG", "{}"))
# Set by the kernel for a "continue from a commit" run: the path to a saved conversation
# snapshot (data/selfmod_convos/<task_id>.jsonl) whose transcript this run should resume as
# full context. Empty for a normal fresh request. See agent._maybe_seed_resume.
RESUME_CONVO = os.environ.get("QUINE_RESUME_CONVO", "")
# The kernel's optional deployment edge token. Syscalls must carry it or gateway auth rejects them
# with 401. Unset in local development means no auth header is sent.
AUTH_TOKEN = os.environ.get("KERNEL_AUTH_TOKEN", "")


def _auth_headers(base: dict | None = None) -> dict:
    headers = dict(base or {})
    if AUTH_TOKEN:
        headers["authorization"] = f"Bearer {AUTH_TOKEN}"
    return headers


# ── stdout protocol (worker → kernel) ───────────────────────────────────────────────
def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def ready() -> None:
    """Signal successful startup. If the kernel never sees this, it falls back to the
    recovery runtime — so emit it before doing real work."""
    _emit({"event": "ready"})


def step(kind: str, summary: str = "", **extra) -> None:
    _emit({"event": "step", "kind": kind, "summary": summary, **extra})


def propose(message: str) -> None:
    _emit({"event": "propose", "message": message})


# ── kernel syscalls (worker → kernel; keys stay in the kernel) ──────────────────────
def _syscall_url(path: str) -> str:
    """Build the full syscall URL; returns empty string if SYSCALL base is unset."""
    if not SYSCALL:
        return ""
    return SYSCALL.rstrip("/") + "/" + path.lstrip("/")


def _http_error_payload(exc: urllib.error.HTTPError, path: str) -> dict:
    """Recover the JSON error body the kernel sent with a non-2xx status.

    urllib raises HTTPError *instead of returning*, discarding the response body — so a
    /llm_call provider failure (missing key, unknown model, rate limit) would otherwise
    reach the agent as a useless "HTTP Error 502: Bad Gateway", hiding the real cause.
    The kernel already serialises the reason as {"ok": False, "error": ...}; read it back."""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            parsed.setdefault("ok", False)
            parsed.setdefault("status", exc.code)  # mark the HTTP status (non-2xx ⇒ not a verdict)
            return parsed
    except json.JSONDecodeError:
        pass
    return {"ok": False, "status": exc.code,
            "error": f"kernel syscall {path} failed (HTTP {exc.code}): "
                     f"{body[:500].strip() or exc.reason}"}


def _post(path: str, payload: dict, timeout: float = 600) -> dict:
    url = _syscall_url(path)
    if not url:
        return {"ok": False, "error": "QUINE_SYSCALL_URL not set"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=_auth_headers({"content-type": "application/json"}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return _http_error_payload(exc, path)
    # urllib.error.URLError (kernel unreachable) is left to propagate — callers such as
    # validate()/poll_steer() already treat that as "defer to the kernel backstop".


def _get(path: str, timeout: float = 10) -> dict:
    url = _syscall_url(path)
    if not url:
        return {"error": "QUINE_SYSCALL_URL not set", "messages": []}
    req = urllib.request.Request(url, headers=_auth_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def poll_steer() -> list[str]:
    """Drain any mid-run steering messages the user queued for this task. Best-effort:
    returns [] if the syscall server isn't reachable (e.g. an offline unit test)."""
    try:
        return _get("/steer").get("messages", []) or []
    except Exception:
        return []


def llm_call(model: str, messages: list[dict], tools: list[dict] | None = None, **kw) -> dict:
    """Inference via the kernel. Returns {"ok":bool, "response"|"error":...}."""
    return _post("/llm_call", {"model": model, "messages": messages, "tools": tools, **kw})


def validate() -> dict:
    """Validate the staging via the kernel — ADVISORY only, so the model's fix-retry loop can
    see validation errors mid-run. It is never authoritative: the kernel re-validates the
    staging (agent_runtime._validate) before it commits. So we only honour a genuine 200
    verdict from OUR kernel; anything that is NOT such a verdict — the syscall server is
    unreachable/misconfigured, or a DIFFERENT kernel happens to answer on the port and reports
    'no active self-mod task' (HTTP 409), or auth rejects it (401) — is deferred to that
    backstop rather than being mistaken for a real validation failure (which would wedge the
    agent in an unfixable retry loop)."""
    deferred = {"ok": True, "report": "validation deferred to kernel backstop"}
    try:
        res = _post("/validate", {}, timeout=600)
    except Exception:  # unreachable → defer to the kernel backstop
        return deferred
    if not isinstance(res, dict) or res.get("status") or res.get("error"):
        return deferred  # non-2xx / transport / misconfig ⇒ not a verdict on our staging
    return res
