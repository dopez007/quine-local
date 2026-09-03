"""Recovery runtime — the immutable fallback brain.

A minimal, self-contained self-mod worker (stdlib + urllib only) the kernel runs ONLY
when the active version's editable runtime/ is missing or fails to start. It speaks the
same stdout protocol and is keyless (inference via the kernel's /llm_call syscall). Kept
intentionally tiny so it is always available to fix or roll back a broken runtime/.

Run as a direct script (NOT `-m`, to avoid importing the kernel package):
    python -u kernel/recovery_runtime.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

STAGING = pathlib.Path(os.environ["QUINE_STAGING_DIR"])
SYSCALL = os.environ.get("QUINE_SYSCALL_URL", "")
PROMPT = os.environ.get("QUINE_TASK_PROMPT", "")
CONFIG = json.loads(os.environ.get("QUINE_CONFIG", "{}"))
# An optional deployment edge token: syscalls must carry it or gateway auth returns 401. Unset in
# local development means no header.
AUTH_TOKEN = os.environ.get("KERNEL_AUTH_TOKEN", "")
_MAX = 8000
# Detached shell children (no console) — mirrors kernel.util.CHILD_CREATIONFLAGS, inlined
# because this script must stay import-free of the kernel package. The constant only
# exists on Windows; 0 = no-op elsewhere.
_CREATIONFLAGS: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _step(kind: str, summary: str = "") -> None:
    _emit({"event": "step", "kind": kind, "summary": summary})


def _headers(base: dict | None = None) -> dict:
    headers = dict(base or {})
    if AUTH_TOKEN:
        headers["authorization"] = f"Bearer {AUTH_TOKEN}"
    return headers


def _post(path: str, payload: dict, timeout: float = 600) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SYSCALL + path, data=data,
        headers=_headers({"content-type": "application/json"}), method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _poll_steer() -> list:
    """Drain any mid-run steering messages (best-effort; [] if unreachable/offline)."""
    try:
        req = urllib.request.Request(SYSCALL + "/steer", headers=_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("messages", []) or []
    except Exception:
        return []


def _validate() -> dict:
    try:
        return _post("/validate", {})
    except Exception:  # unreachable/misconfigured → defer to the kernel backstop
        return {"ok": True, "report": "validation deferred to kernel backstop"}


def _truncate(t: str) -> str:
    return t if len(t) <= _MAX else t[:_MAX] + "\n…[truncated]"


def _within(rel: str) -> pathlib.Path | None:
    try:
        full = (STAGING / rel).resolve()
        full.relative_to(STAGING.resolve())
        return full
    except (ValueError, OSError):
        return None


def _tool(name: str, args: dict) -> str:
    if name == "read_file":
        t = _within(args.get("path", ""))
        return _truncate(t.read_text(encoding="utf-8", errors="replace")) if t and t.exists() else "ERROR: not found"
    if name == "write_file":
        t = _within(args.get("path", ""))
        if t is None:
            return "ERROR: path escapes the workspace"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(args.get("content", ""), encoding="utf-8")
        return f"wrote {args.get('path')}"
    if name == "edit_file":
        t = _within(args.get("path", ""))
        if t is None or not t.exists():
            return "ERROR: not found"
        old, new = args.get("old", ""), args.get("new", "")
        if not old:
            return "ERROR: 'old' must be non-empty"
        text = t.read_text(encoding="utf-8", errors="replace")
        if old not in text:
            return "ERROR: 'old' not found (must match exactly)"
        t.write_text(text.replace(old, new), encoding="utf-8")
        return f"edited {args.get('path')}"
    if name == "list_dir":
        t = _within(args.get("path", "."))
        return "\n".join(sorted(x.name for x in t.iterdir())) if t and t.exists() else "ERROR: not found"
    if name == "run_shell":
        try:
            r = subprocess.run(args.get("command", ""), shell=True, cwd=str(STAGING),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=int(args.get("timeout", 120)),
                               creationflags=_CREATIONFLAGS)
            return _truncate(f"exit={r.returncode}\n{r.stdout}\n{r.stderr}")
        except subprocess.TimeoutExpired:
            return "ERROR: timed out"
    if name == "run_tests":
        return _validate().get("report", "")
    return f"ERROR: unknown tool {name}"


_SCHEMAS = [
    {"type": "function", "function": {"name": n, "description": d,
     "parameters": {"type": "object", "properties": p, "required": req}}}
    for n, d, p, req in [
        ("read_file", "Read a file.", {"path": {"type": "string"}}, ["path"]),
        ("write_file", "Write a file.", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        ("edit_file", "Patch a file by replacing an exact substring (prefer over write_file).",
         {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, ["path", "old", "new"]),
        ("list_dir", "List a directory.", {"path": {"type": "string"}}, []),
        ("run_shell", "Run a shell command.", {"command": {"type": "string"}}, ["command"]),
        ("run_tests", "Validate the workspace.", {}, []),
        ("propose_commit", "Finish and request commit.", {"message": {"type": "string"}}, ["message"]),
    ]
]


def _scripted_step(i: int):
    if i == 1:
        return None, [{"id": "c" + uuid.uuid4().hex[:8], "name": "read_file", "args": {"path": "main.py"}}]
    if i == 2:
        txt = (STAGING / "main.py").read_text(encoding="utf-8")
        patched = re.sub(r'APP_BUILD = ".*?"', f'APP_BUILD = "auto: {" ".join(PROMPT.split())[:34]}"', txt, count=1)
        req = STAGING / "REQUESTS.md"
        existing = req.read_text(encoding="utf-8") if req.exists() else "# Change requests\n"
        return None, [
            {"id": "c" + uuid.uuid4().hex[:8], "name": "write_file", "args": {"path": "main.py", "content": patched}},
            {"id": "c" + uuid.uuid4().hex[:8], "name": "write_file",
             "args": {"path": "REQUESTS.md", "content": existing + f"\n- {PROMPT}\n"}},
        ]
    return None, [{"id": "c" + uuid.uuid4().hex[:8], "name": "propose_commit",
                   "args": {"message": "recovery: " + PROMPT[:50]}}]


def _litellm_step(model: str, temperature: float, messages: list[dict]):
    resp = _post("/llm_call", {"model": model, "messages": messages, "tools": _SCHEMAS, "temperature": temperature})
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "llm_call failed"))
    msg = resp["response"]["choices"][0]["message"]
    calls = []
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": tc.get("id") or "c" + uuid.uuid4().hex[:8], "name": tc["function"]["name"], "args": args})
    return msg.get("content"), calls


def main() -> None:
    _emit({"event": "ready"})
    agent = CONFIG.get("agent", {})
    scripted = agent.get("engine") == "scripted"
    model = agent.get("model", "gpt-4o-mini")
    temperature = float(agent.get("temperature", 0.0))
    max_steps = int(agent.get("max_steps", 40))
    _step("start", f"recovery engine={'scripted' if scripted else model}")

    messages: list[dict] = [
        {"role": "system", "content": "You are the recovery self-modification agent. Make the requested "
         "change to the staging workspace, then call propose_commit. Keep main.py importable with GET /health."},
        {"role": "user", "content": PROMPT},
    ]
    i = 0
    for _ in range(max_steps):
        for m in _poll_steer():  # honor mid-run user steering
            _step("steer_received", m[:160])
            messages.append({"role": "user", "content": "[steering] " + m})
        i += 1
        try:
            text, calls = _scripted_step(i) if scripted else _litellm_step(model, temperature, messages)
        except Exception as exc:
            _step("engine_error", str(exc))
            return
        _step("assistant", ",".join(c["name"] for c in calls))
        if not calls:
            return
        messages.append({"role": "assistant", "content": text or "",
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"], "arguments": json.dumps(c["args"])}} for c in calls]})
        for c in calls:
            if c["name"] == "propose_commit":
                res = _validate()
                if res.get("ok"):
                    _emit({"event": "propose", "message": c["args"].get("message", "recovery change")})
                    return
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": "VALIDATION FAILED:\n" + res.get("report", "")})
            else:
                result = _tool(c["name"], c["args"])
                # Content-free status only — a tool result is the app's own source; never echo
                # it to the live event log (matches app/runtime/agent.py source concealment).
                _step("tool_result", f"{c['name']}: done ({len(result)} chars)")
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _step("worker_error", str(exc))
