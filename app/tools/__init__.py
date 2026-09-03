"""Run-agent tool registry (app layer — extend me).

Add a tool: create a module here — `tools/<name>.py` — that defines a module-level
`TOOLS` dict mapping tool name -> {"schema": <OpenAI tool schema>, "handler": async
(args, ctx) -> str, optional "allow_repeats": bool} (see notes.py / harness.py for the shape).
It is AUTO-DISCOVERED and
registered on the next reboot — you do NOT need to edit this file. You may also add
entries to an existing module's `TOOLS`. Modules whose name starts with "_" are treated
as private helpers and skipped; a module that fails to import is skipped (its error is
recorded in DISCOVERY_ERRORS) so one bad tool can't take the rest offline.

`main.py` wires `SCHEMAS` + `execute()` into the streaming Run-tab loop.

Nothing here is privileged: file tools touch only the data partition (`ctx.notes_dir`);
harness tools read through the kernel's read-only syscalls (`ctx.syscall_get`).
"""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class ToolContext:
    """What tools are allowed to touch — passed in from main.py.

    The first three are the original capabilities; the rest were added for the day-one
    feature set and default to None so older callers/tests keep working:
      • data_dir     — the persistent data partition (knowledge store lives here)
      • config_get   — returns the app's backend_config.json (web-search provider key, …)
      • syscall_post — POST a kernel syscall (used for embeddings via /llm_call kind:embed)
    """
    notes_dir: pathlib.Path
    syscall_get: Callable[[str], Awaitable[dict]]
    dev_dir: pathlib.Path
    data_dir: pathlib.Path | None = None
    config_get: Callable[[], dict] | None = None
    syscall_post: Callable[[str, dict], Awaitable[dict]] | None = None

# module name -> import error; a broken tool module is skipped (not fatal), recorded here.
DISCOVERY_ERRORS: dict[str, str] = {}


def _discover() -> dict:
    """Import every non-private submodule and merge any module-level `TOOLS` dict it
    exposes, so a newly added tool file is registered with NO wiring in this file."""
    DISCOVERY_ERRORS.clear()
    registry: dict = {}
    for modinfo in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        name = modinfo.name
        if name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:  # one broken tool module must not break the others
            DISCOVERY_ERRORS[name] = f"{type(exc).__name__}: {exc}"
            print(f"[tools] skipped {name}: {DISCOVERY_ERRORS[name]}", file=sys.stderr)
            continue
        tools = getattr(mod, "TOOLS", None)
        if isinstance(tools, dict):
            registry.update(tools)
    return registry


# name -> {"schema": <openai schema>, "handler": async (args, ctx) -> str}
REGISTRY: dict = _discover()

# Schemas to advertise to the model.
SCHEMAS = [t["schema"] for t in REGISTRY.values()]


def allows_repeated_calls(name: str) -> bool:
    """Whether identical calls are legitimate polling rather than evidence of a model loop."""
    return bool(REGISTRY.get(name, {}).get("allow_repeats", False))


async def execute(name: str, args: dict, ctx: ToolContext) -> str:
    tool = REGISTRY.get(name)
    if tool is None:
        return f"error: unknown tool {name}"
    try:
        return await tool["handler"](args or {}, ctx)
    except Exception as exc:  # tools must never crash the chat loop
        return f"error: {exc}"
