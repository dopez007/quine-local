"""Plugin SDK + loader (Phase 4 platform foundation).

A *plugin* is any module in this package (`app/features/`) that defines a module-level
`PLUGIN` dict and, optionally:
  • `router` — a FastAPI `APIRouter` the app mounts, and/or
  • `TOOLS`  — Run-agent tools (same shape as the `tools/` registry: name → {schema, handler}),
  • `setup(app)` — a lifecycle hook called once at load, before the server starts: register
    startup/shutdown handlers, background tasks, middleware. A raising setup marks the
    plugin errored (and skips its router/tools) without breaking the others.

`main.py` calls `load(app)` at startup to discover plugins, mount their routers, and merge
their tools, then exposes them at `GET /api/plugins`. True to the microkernel design, a new
plugin is just a module the agent writes here via self-modification (no kernel change needed).

Minimal example (`app/features/hello_plugin.py`):

    from fastapi import APIRouter
    PLUGIN = {"name": "hello", "version": "1.0.0", "description": "..."}
    router = APIRouter(prefix="/api/plugins/hello")

    @router.get("/ping")
    async def ping(): return {"pong": True}
"""

from __future__ import annotations

import importlib
import importlib.util
import pathlib
import pkgutil
import sys
from dataclasses import dataclass, field
from typing import Callable

from fastapi import APIRouter

import features  # the package we scan for plugin modules

# Modules in this package that are infrastructure, not plugins.
_SKIP = {"plugins"}


@dataclass
class PluginInfo:
    name: str
    version: str
    description: str
    module: str
    router: APIRouter | None = None
    tools: dict = field(default_factory=dict)
    setup: Callable | None = None  # optional lifecycle hook: called as setup(app) at load
    error: str | None = None
    installed: bool = False  # True = installed at runtime (data dir); False = built-in

    def describe(self) -> dict:
        routes = []
        if self.router is not None:
            for r in self.router.routes:
                routes.append({"path": getattr(r, "path", ""),
                               "methods": sorted(getattr(r, "methods", None) or [])})
        return {"name": self.name, "version": self.version, "description": self.description,
                "module": self.module, "routes": routes, "tools": sorted(self.tools),
                "source": "installed" if self.installed else "builtin", "error": self.error}


def _module_to_info(module, origin: str, *, installed: bool = False) -> PluginInfo | None:
    meta = getattr(module, "PLUGIN", None)
    if not isinstance(meta, dict) or "name" not in meta:
        return None
    setup = getattr(module, "setup", None)
    return PluginInfo(
        name=str(meta["name"]), version=str(meta.get("version", "0.0.0")),
        description=str(meta.get("description", "")), module=origin,
        router=getattr(module, "router", None), tools=dict(getattr(module, "TOOLS", {}) or {}),
        setup=setup if callable(setup) else None,
        installed=installed,
    )


def discover() -> list[PluginInfo]:
    """Find every built-in plugin module under `app/features/` (fault-tolerant: a broken plugin
    is reported with an `error`, never crashing discovery)."""
    found: list[PluginInfo] = []
    for mod in pkgutil.iter_modules(features.__path__):
        if mod.name in _SKIP or mod.name.startswith("_"):
            continue
        fqmn = f"features.{mod.name}"
        try:
            module = importlib.import_module(fqmn)
        except Exception as exc:  # a bad plugin must not break the others
            found.append(PluginInfo(name=mod.name, version="?", description="",
                                    module=fqmn, error=f"import failed: {exc}"))
            continue
        info = _module_to_info(module, fqmn)
        if info is not None:  # else: an ordinary feature module, not a declared plugin
            found.append(info)
    return sorted(found, key=lambda p: p.name)


def load_file(path: str | pathlib.Path) -> PluginInfo:
    """Import a single installed plugin module from a file path (raises on failure)."""
    path = pathlib.Path(path)
    mod_name = f"installed_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    info = _module_to_info(module, f"installed:{path.name}", installed=True)
    if info is None:
        raise ValueError("module does not declare a PLUGIN dict")
    return info


def discover_dir(dirpath: str | pathlib.Path) -> list[PluginInfo]:
    """Discover installed plugins from a directory (e.g. the data partition). Fault-tolerant."""
    d = pathlib.Path(dirpath)
    found: list[PluginInfo] = []
    if not d.is_dir():
        return found
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            found.append(load_file(f))
        except Exception as exc:
            found.append(PluginInfo(name=f.stem, version="?", description="",
                                    module=f"installed:{f.name}", error=f"load failed: {exc}",
                                    installed=True))
    return found


def load(app, extra_dir: str | pathlib.Path | None = None) -> tuple[list[PluginInfo], dict]:
    """Mount each healthy plugin's router on `app`; return (plugins, merged tools). Built-in
    plugins (app/features/) plus any installed under `extra_dir` (the data partition)."""
    plugins = discover()
    if extra_dir:
        plugins += discover_dir(extra_dir)
    tools: dict = {}
    for p in plugins:
        if p.error:
            continue
        if p.setup is not None:
            try:
                p.setup(app)
            except Exception as exc:  # a bad setup must not break the other plugins
                p.error = f"setup failed: {exc}"
                continue
        if p.router is not None:
            app.include_router(p.router)
        tools.update(p.tools)
    return plugins, tools
