"""Harness tools — read-only views of the kernel via its privileged syscalls.

These never mutate anything: they go through `ctx.syscall_get(path)` which the kernel
exposes (status / versions / audit). Each tool is `async def(args, ctx) -> str`.

Note: version *diffs* are deliberately NOT exposed here — surfacing a diff would leak the
app's own source, so diff review is reserved for operator-facing version tools.
"""

from __future__ import annotations

import json


def _schema(name: str, description: str, properties: dict, required=None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


async def get_status(args: dict, ctx) -> str:
    return json.dumps(await ctx.syscall_get("/status"))


async def list_versions(args: dict, ctx) -> str:
    return json.dumps(await ctx.syscall_get("/versions"))


async def read_audit(args: dict, ctx) -> str:
    limit = args.get("limit", 30)
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 30
    data = await ctx.syscall_get(f"/audit?limit={limit}")
    entries = data.get("audit", []) if isinstance(data, dict) else []
    if not entries:
        return "no audit entries"
    lines = []
    for e in entries:
        ts = (e.get("ts") or "").replace("T", " ")[:19]
        rest = {k: v for k, v in e.items() if k not in ("ts", "event")}
        detail = " ".join(f"{k}={v}" for k, v in rest.items())
        lines.append(f"{ts}  {e.get('event', '?')}  {detail}".rstrip())
    return "\n".join(lines)


TOOLS = {
    "get_status": {
        "schema": _schema("get_status", "Get harness status (active version, slot, port).", {}),
        "handler": get_status,
    },
    "list_versions": {
        "schema": _schema("list_versions", "List recorded app versions (newest first).", {}),
        "handler": list_versions,
    },
    "read_audit": {
        "schema": _schema(
            "read_audit",
            "Read recent harness audit events (requests, commits, reboots, rollbacks).",
            {"limit": {"type": "integer", "description": "How many recent events (1–200)."}},
        ),
        "handler": read_audit,
    },
}
