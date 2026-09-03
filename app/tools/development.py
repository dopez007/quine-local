"""Development sandbox tools — build arbitrary software in the development/ workspace.

Each tool is `async def handler(args: dict, ctx) -> str`. The workspace lives at
`ctx.dev_dir` — a directory on the persistent DATA partition (never inside the app slot,
which the kernel rebuilds from git on every version switch), so what the agent builds here
survives reboots, self-mods and rollbacks.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib

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


async def dev_list(args: dict, ctx) -> str:
    """List the development workspace contents as a tree."""
    dev = ctx.dev_dir
    dev.mkdir(parents=True, exist_ok=True)

    def _walk(rel: pathlib.Path) -> list[dict]:
        full = dev / rel
        if not full.is_dir():
            return []
        entries = []
        for child in sorted(full.iterdir()):
            if child.name.startswith("."):
                continue
            r = child.relative_to(dev)
            if child.is_dir():
                entries.append({
                    "name": str(r),
                    "kind": "dir",
                    "children": _walk(r),
                })
            else:
                try:
                    sz = child.stat().st_size
                except OSError:
                    sz = 0
                entries.append({"name": str(r), "kind": "file", "size": sz})
        return entries

    root = {"name": "", "kind": "dir", "children": _walk(pathlib.Path(""))}
    return json.dumps(root, indent=2)


async def dev_read_file(args: dict, ctx) -> str:
    """Read a UTF-8 text file from development/ (or a byte-offset slice)."""
    rel = (args.get("path") or "").strip()
    if not rel:
        return "error: path is required"
    target = _resolve(rel, ctx.dev_dir)
    if target is None:
        return "error: path escapes the development/ workspace"
    if not target.exists() or not target.is_file():
        return f"error: not found — development/{rel}"
    try:
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is not None or limit is not None:
            raw = target.read_bytes()
            off = int(offset) if offset is not None else 0
            lim = int(limit) if limit is not None else len(raw) - off
            if off < 0:
                off = max(0, len(raw) + off)
            chunk = raw[off:off + lim]
            text = chunk.decode("utf-8", errors="replace")
            sz = len(chunk)
            total = len(raw)
            footer = ""
            if off > 0:
                footer += f" [bytes {off}–{off + sz}]"
            else:
                footer += f" [first {sz} bytes]"
            if off + sz < total:
                footer += f" (total file: {total} bytes)"
            if len(text) > 12000:
                text = text[:12000] + f"\n…[truncated {len(text) - 12000} chars]"
            return text + footer
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"error: {e}"
    MAX = 12000
    if len(text) > MAX:
        text = text[:MAX] + f"\n…[truncated {len(text) - MAX} chars]"
    return text


async def dev_write_file(args: dict, ctx) -> str:
    """Create or overwrite a file in development/."""
    rel = (args.get("path") or "").strip()
    if not rel:
        return "error: path is required"
    target = _resolve(rel, ctx.dev_dir)
    if target is None:
        return "error: path escapes the development/ workspace"
    content = args.get("content", "")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote development/{rel} ({len(content)} chars)"


async def dev_edit_file(args: dict, ctx) -> str:
    """Edit a file in development/ by replacing an exact substring."""
    rel = (args.get("path") or "").strip()
    if not rel:
        return "error: path is required"
    old = args.get("old", "")
    new = args.get("new", "")
    if not old:
        return "error: 'old' must be a non-empty string"
    target = _resolve(rel, ctx.dev_dir)
    if target is None:
        return "error: path escapes the development/ workspace"
    if not target.exists():
        return f"error: not found — development/{rel}"
    text = target.read_text(encoding="utf-8", errors="replace")
    occ = text.count(old)
    if occ == 0:
        return "error: 'old' not found in file (it must match exactly, including whitespace)"
    count = int(args.get("count", 1) or 0)
    if count == 1 and occ > 1:
        return (f"error: 'old' is ambiguous ({occ} matches); add more surrounding "
                "context to make it unique, or set count to replace several.")
    replaced = text.replace(old, new) if count == 0 else text.replace(old, new, count)
    target.write_text(replaced, encoding="utf-8")
    n = occ if count == 0 else min(count, occ)
    return f"edited development/{rel} ({n} replacement{'s' if n != 1 else ''})"


async def dev_run_shell(args: dict, ctx) -> str:
    """Run a shell command inside the development/ workspace."""
    cmd = (args.get("command") or "").strip()
    if not cmd:
        return "error: command is required"
    dev = ctx.dev_dir
    dev.mkdir(parents=True, exist_ok=True)
    timeout = int(args.get("timeout", 120))
    env = os.environ.copy()
    env.update({"NO_COLOR": "1", "FORCE_COLOR": "0", "PY_COLORS": "0"})
    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(dev),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **popen_kwargs(),
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc is not None:
            await terminate_tree(proc.pid)
        return "error: command timed out"
    except asyncio.CancelledError:
        if proc is not None:
            await terminate_tree(proc.pid)
        raise
    MAX = 8000
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    out = f"exit={proc.returncode}\n--stdout--\n{stdout}\n--stderr--\n{stderr}"
    if len(out) > MAX:
        out = out[:MAX] + f"\n…[truncated {len(out) - MAX} chars]"
    return out


def _resolve(rel: str, dev_dir: pathlib.Path) -> pathlib.Path | None:
    """Resolve a relative path under dev_dir; return None if it escapes."""
    try:
        full = (dev_dir / rel).resolve()
        full.relative_to(dev_dir.resolve())
        return full
    except (ValueError, OSError):
        return None


TOOLS = {
    "dev_list": {
        "schema": _schema(
            "dev_list",
            "List the development workspace contents as a JSON tree. Use this to see what's been created.",
            {},
        ),
        "handler": dev_list,
    },
    "dev_read_file": {
        "schema": _schema(
            "dev_read_file",
            "Read a file from the development/ sandbox (or a byte-offset slice). "
            "Provide the path relative to development/. Use offset + limit to read "
            "large files in chunks; omitting both returns the whole file.",
            {"path": {"type": "string", "description": "relative path inside development/"},
             "offset": {"type": "integer", "description": "byte offset to start reading from"},
             "limit": {"type": "integer", "description": "max bytes to read"}},
            ["path"],
        ),
        "handler": dev_read_file,
    },
    "dev_write_file": {
        "schema": _schema(
            "dev_write_file",
            "Create or overwrite a file in the development/ sandbox (for building software projects). "
            "Provide the path relative to development/ and the content string.",
            {"path": {"type": "string", "description": "relative path inside development/"},
             "content": {"type": "string", "description": "file content"}},
            ["path", "content"],
        ),
        "handler": dev_write_file,
    },
    "dev_edit_file": {
        "schema": _schema(
            "dev_edit_file",
            "Edit a file in the development/ sandbox by replacing an exact substring. "
            "Prefer this over dev_write_file for small changes — the 'old' text must match exactly.",
            {"path": {"type": "string"}, "old": {"type": "string"},
             "new": {"type": "string"},
             "count": {"type": "integer", "description": "replacements; omit or 1 for single, 0 for all"}},
            ["path", "old", "new"],
        ),
        "handler": dev_edit_file,
    },
    "dev_run_shell": {
        "schema": _schema(
            "dev_run_shell",
            "Run a shell command inside the development/ sandbox. Use for compiling, installing deps "
            "(npm install, uv pip install), running tests, or running the built software. "
            "The working directory is set to development/.",
            {"command": {"type": "string", "description": "shell command to run"},
             "timeout": {"type": "integer", "description": "timeout in seconds (default 120)"}},
            ["command"],
        ),
        "handler": dev_run_shell,
    },
}
