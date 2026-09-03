"""Background process tools for the development sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import time
import uuid
from dataclasses import dataclass

from ._processes import popen_kwargs, terminate_tree


def _schema(name: str, description: str, properties: dict, required=None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


@dataclass
class BackgroundTask:
    id: str
    name: str
    command: str
    cwd: pathlib.Path
    log_path: pathlib.Path
    started_at: float
    proc: subprocess.Popen

    def view(self, dev_dir: pathlib.Path, include_tail: bool = False) -> dict:
        code = self.proc.poll()
        rel_cwd = self.cwd.relative_to(dev_dir).as_posix() if self.cwd != dev_dir else "."
        rel_log = self.log_path.relative_to(dev_dir).as_posix()
        out = {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "cwd": rel_cwd,
            "pid": self.proc.pid,
            "running": code is None,
            "returncode": code,
            "started_at": self.started_at,
            "log": rel_log,
        }
        if include_tail:
            out["log_tail"] = _tail(self.log_path)
        return out


_TASKS: dict[str, BackgroundTask] = {}
BG_WAIT_DEFAULT_SECONDS = 20.0
BG_WAIT_MAX_SECONDS = 25.0  # the Run loop gives every tool at most 30 seconds
BG_WAIT_POLL_SECONDS = 0.25


def _resolve_dir(rel: str, dev_dir: pathlib.Path) -> pathlib.Path | None:
    try:
        full = (dev_dir / (rel or ".")).resolve()
        full.relative_to(dev_dir.resolve())
        return full
    except (ValueError, OSError):
        return None


def _task_dir(dev_dir: pathlib.Path) -> pathlib.Path:
    d = dev_dir / ".quine" / "background"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tail(path: pathlib.Path, max_chars: int = 8000) -> str:
    if not path.exists():
        return ""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f"error reading log: {exc}"
    chunk = raw[-max_chars:]
    text = chunk.decode("utf-8", errors="replace")
    if len(raw) > max_chars:
        text = f"…[last {max_chars} bytes]\n{text}"
    return text


async def bg_start(args: dict, ctx) -> str:
    command = (args.get("command") or "").strip()
    if not command:
        return "error: command is required"
    name = (args.get("name") or "background-task").strip()[:80] or "background-task"
    cwd = _resolve_dir((args.get("cwd") or ".").strip(), ctx.dev_dir)
    if cwd is None:
        return "error: cwd escapes the development/ workspace"
    cwd.mkdir(parents=True, exist_ok=True)

    task_id = "bg_" + uuid.uuid4().hex[:10]
    log_path = _task_dir(ctx.dev_dir) / f"{task_id}.log"
    env = os.environ.copy()
    env.update({"NO_COLOR": "1", "FORCE_COLOR": "0", "PY_COLORS": "0"})
    log = log_path.open("a", encoding="utf-8", errors="replace")
    log.write(f"$ {command}\n[started {time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
    log.flush()
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **popen_kwargs(),
        )
    finally:
        # The child now owns the file handle. Closing our duplicate prevents handle leaks in the app.
        log.close()
    task = BackgroundTask(task_id, name, command, cwd, log_path, time.time(), proc)
    _TASKS[task_id] = task
    return json.dumps({"ok": True, "task": task.view(ctx.dev_dir, include_tail=True)}, indent=2)


async def bg_list(args: dict, ctx) -> str:
    include_tail = bool(args.get("tail", False))
    tasks = [t.view(ctx.dev_dir, include_tail=include_tail)
             for t in sorted(_TASKS.values(), key=lambda x: x.started_at, reverse=True)]
    return json.dumps({"tasks": tasks}, indent=2)


async def bg_read_log(args: dict, ctx) -> str:
    task_id = (args.get("id") or "").strip()
    if not task_id:
        return "error: id is required"
    task = _TASKS.get(task_id)
    if task is None:
        return f"error: unknown background task {task_id}"
    max_chars = int(args.get("max_chars", 8000) or 8000)
    log = _tail(task.log_path, max_chars=max(1000, min(max_chars, 50000))) or "(log is empty)"
    code = task.proc.poll()
    if code is None:
        status = "still running; use bg_wait to wait before checking again"
    else:
        status = f"finished with return code {code}"
    return f"{log}\n\n[background task {status}]"


async def bg_wait(args: dict, ctx) -> str:
    """Wait without busy-polling, while remaining immediately cancellable by Run's Stop button."""
    task_id = (args.get("id") or "").strip()
    if not task_id:
        return "error: id is required"
    task = _TASKS.get(task_id)
    if task is None:
        return f"error: unknown background task {task_id}"
    try:
        timeout_seconds = float(args.get("timeout_seconds", BG_WAIT_DEFAULT_SECONDS))
    except (TypeError, ValueError):
        return "error: timeout_seconds must be a number"
    timeout_seconds = max(0.0, min(timeout_seconds, BG_WAIT_MAX_SECONDS))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while task.proc.poll() is None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(BG_WAIT_POLL_SECONDS, remaining))

    view = task.view(ctx.dev_dir, include_tail=True)
    return json.dumps({
        "ok": True,
        "completed": not view["running"],
        "timed_out": bool(view["running"]),
        "task": view,
    }, indent=2)


async def bg_stop(args: dict, ctx) -> str:
    task_id = (args.get("id") or "").strip()
    if not task_id:
        return "error: id is required"
    task = _TASKS.get(task_id)
    if task is None:
        return f"error: unknown background task {task_id}"
    if task.proc.poll() is None:
        await terminate_tree(task.proc.pid)
        try:
            await asyncio.to_thread(task.proc.wait, timeout=2)
        except subprocess.TimeoutExpired:
            pass
    return json.dumps({"ok": True, "task": task.view(ctx.dev_dir, include_tail=True)}, indent=2)


TOOLS = {
    "bg_start": {
        "schema": _schema(
            "bg_start",
            "Start a long-running background command inside the development/ sandbox and return "
            "immediately with a task id and log path. Use this for dev servers, watchers, or "
            "programs the user wants to test while you continue working.",
            {"command": {"type": "string", "description": "shell command to start"},
             "name": {"type": "string", "description": "short label for the task"},
             "cwd": {"type": "string", "description": "working directory relative to development/"}},
            ["command"],
        ),
        "handler": bg_start,
    },
    "bg_list": {
        "schema": _schema(
            "bg_list",
            "List background tasks started by bg_start, including pid, running state, return code, "
            "and log path. Pass tail=true to include recent log output.",
            {"tail": {"type": "boolean", "description": "include recent log output"}},
        ),
        "handler": bg_list,
        "allow_repeats": True,
    },
    "bg_read_log": {
        "schema": _schema(
            "bg_read_log",
            "Read one snapshot of recent log output for a background task. If it is still running, "
            "use bg_wait instead of repeatedly polling this tool.",
            {"id": {"type": "string", "description": "background task id"},
             "max_chars": {"type": "integer", "description": "maximum characters to return"}},
            ["id"],
        ),
        "handler": bg_read_log,
        "allow_repeats": True,
    },
    "bg_wait": {
        "schema": _schema(
            "bg_wait",
            "Wait for a background task to finish without busy-polling. Returns after completion or "
            "a bounded timeout with the current status and recent log. It is safe to call again if "
            "the task is still running.",
            {"id": {"type": "string", "description": "background task id"},
             "timeout_seconds": {
                 "type": "number",
                 "description": "seconds to wait (default 20, maximum 25)",
             }},
            ["id"],
        ),
        "handler": bg_wait,
        "allow_repeats": True,
    },
    "bg_stop": {
        "schema": _schema(
            "bg_stop",
            "Stop a background task started by bg_start. Only owned task ids can be stopped.",
            {"id": {"type": "string", "description": "background task id"}},
            ["id"],
        ),
        "handler": bg_stop,
    },
}
