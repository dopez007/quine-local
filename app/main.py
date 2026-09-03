"""Seed application (ring 3 / user space) — version 2.

A React (Vite) single-page UI plus a small backend the agent is meant to extend.
Beyond serving the built UI, it provides one usable feature — the "Run" tab's agent:
a streaming, tool-using chat over the kernel's `llm_stream` primitive, with
conversations persisted under the data partition. Nothing here is privileged; provider
keys never reach this process.

Run contract (set by the kernel/bootloader):
  • cwd is the slot dir, so this module is importable as `main`.
  • env QUINE_DATA_DIR    → the persistent user-data partition.
  • env QUINE_SYSCALL_URL → base URL for privileged kernel calls.
  • Provider secrets are deliberately NOT present in this environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import time
import uuid
import zipfile
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import errorlog  # error tracker — persistent, version-stamped error records (the "Sentry")
from tools import (  # extensible Run-agent tools (see tools/)
    SCHEMAS,
    ToolContext,
    allows_repeated_calls,
    execute,
)
from tools import knowledge as kb  # knowledge-base store helpers (chunk/ingest/list/delete)
from tools import instructions as ins  # in-app manual: shipped seeds + DATA_DIR overrides
from features import plugins as plugin_sdk  # Phase 4: drop-in plugin SDK (see features/plugins.py)

# Changing this string is the simplest visible proof that a reboot swapped versions.
APP_BUILD = "seed: Quine harness"

HERE = pathlib.Path(__file__).resolve().parent
DIST = HERE / "frontend" / "dist"
ASSETS = DIST / "assets"

DATA_DIR = pathlib.Path(os.environ.get("QUINE_DATA_DIR") or (HERE / ".data"))
CONVO_DIR = DATA_DIR / "conversations"
NOTES_DIR = DATA_DIR / "notes"
# The development sandbox holds software the agent BUILDS FOR THE USER — user data, not app
# source. It must live on the data partition: HERE is the *slot*, which the kernel rmtree's and
# re-extracts from git on every version switch (kernel/versioning.deploy), so a slot-local
# workspace was destroyed by the next reboot. See _rescue_legacy_dev_workspace below.
DEV_DIR = DATA_DIR / "development"
SYSCALL_URL = os.environ.get("QUINE_SYSCALL_URL", "")
# When a deployment configures KERNEL_AUTH_TOKEN, every gateway request must present it — including
# the app's own loopback calls back to the /api/syscall/* boundary, or edge auth answers 401. Unset
# in a local development setup means no header is sent.
KERNEL_AUTH_TOKEN = os.environ.get("KERNEL_AUTH_TOKEN", "")


def _syscall_headers(base: dict | None = None) -> dict:
    """Headers for a call to the kernel syscall boundary, carrying the edge token if set."""
    headers = dict(base or {})
    if KERNEL_AUTH_TOKEN:
        headers["authorization"] = f"Bearer {KERNEL_AUTH_TOKEN}"
    return headers


app = FastAPI(title="Quine App")

# Hashed JS/CSS the Vite build emits. Guarded so `import main` works even before a build.
if ASSETS.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS)), name="assets")

# Discover + mount plugins: built-ins (features/) plus any installed at runtime into the data
# partition (INSTALLED_DIR — persists across reboots/version swaps). Routers are mounted now and
# enabled plugins' tools merged into the Run agent; listed at GET /api/plugins, installed via
# POST .../install, toggled via POST .../{name}/{enable,disable}.
INSTALLED_DIR = DATA_DIR / "plugins"
PLUGIN_STATE_FILE = INSTALLED_DIR / "_state.json"  # {name: enabled} — `_`-prefixed so it's not a plugin
PLUGINS, _ = plugin_sdk.load(app, extra_dir=INSTALLED_DIR)
PLUGIN_TOOLS: dict = {}                  # enabled plugins' tools (mutated in place)
ALL_SCHEMAS = list(SCHEMAS)              # core + enabled-plugin schemas (mutated in place)

PLUGIN_NAME_RE = re.compile(r"[a-z][a-z0-9_]{2,40}")


def _load_plugin_state() -> dict:
    if PLUGIN_STATE_FILE.exists():
        try:
            return json.loads(PLUGIN_STATE_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    return {}


def _save_plugin_state(state: dict) -> None:
    INSTALLED_DIR.mkdir(parents=True, exist_ok=True)
    PLUGIN_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _plugin_enabled(name: str) -> bool:
    return bool(_load_plugin_state().get(name, True))  # enabled by default


def _rebuild_tools() -> None:
    """Recompute the agent's plugin toolset from the currently ENABLED plugins (in place, so
    the streaming loop and ALL_SCHEMAS see the change live)."""
    PLUGIN_TOOLS.clear()
    for p in PLUGINS:
        if not p.error and _plugin_enabled(p.name):
            PLUGIN_TOOLS.update(p.tools)
    ALL_SCHEMAS[:] = SCHEMAS + [t["schema"] for t in PLUGIN_TOOLS.values()]


_rebuild_tools()


@app.middleware("http")
async def _disabled_plugin_gate(request, call_next):
    """A disabled plugin's own routes 404 (the router stays mounted but is taken offline).
    Matches the plugin's DECLARED routes — never the management endpoints (install/toggle/
    uninstall live in the same /api/plugins/<name> namespace but are not plugin routes), so a
    disabled plugin can always still be re-enabled or removed."""
    path = request.url.path
    if path.startswith("/api/plugins/"):
        state = _load_plugin_state()
        for p in PLUGINS:
            if p.router is None or state.get(p.name, True):
                continue
            for route in p.router.routes:
                rp = getattr(route, "path", "")
                if rp and (path == rp or path.startswith(rp.rstrip("/") + "/")):
                    return JSONResponse({"error": f"plugin '{p.name}' is disabled"}, status_code=404)
    return await call_next(request)


# Registered AFTER the plugin gate so it wraps it (outermost): every unhandled exception —
# route handlers, plugins, other middleware — lands in the error tracker instead of dying
# as an uncaptured stderr traceback. The client still gets a JSON 500.
@app.middleware("http")
async def _error_capture(request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        matched_route = getattr(request.scope.get("route"), "path", None)
        errorlog.capture(
            exc,
            source="app",
            route=matched_route or request.url.path,
            method=request.method,
        )
        return JSONResponse(
            {"error": f"internal error: {type(exc).__name__}: {exc}"}, status_code=500)


def _register_plugin(info) -> None:
    """Hot-register a freshly loaded plugin: mount its router, (re)build the enabled toolset,
    and replace any same-named entry in the listing."""
    global PLUGINS
    PLUGINS = [p for p in PLUGINS if p.name != info.name] + [info]
    if info.router is not None:
        app.include_router(info.router)
    _rebuild_tools()


def _plugin_view(p) -> dict:
    return {**p.describe(), "enabled": _plugin_enabled(p.name)}


@app.get("/api/plugins")
async def list_plugins() -> dict:
    """Installed plugins and their enabled state."""
    return {"plugins": [_plugin_view(p) for p in PLUGINS]}


@app.post("/api/plugins/install")
async def install_plugin(payload: dict) -> JSONResponse:
    """Install a plugin by writing its source into the data partition and hot-loading it.

    The source is validated for syntax, then persisted so it survives reboots. It runs in the
    unprivileged app process.
    """
    name = (payload.get("name") or "").strip()
    source = payload.get("source") or ""
    if not PLUGIN_NAME_RE.fullmatch(name):
        return JSONResponse({"error": "invalid plugin name (use a-z, 0-9, _; 3–41 chars)"},
                            status_code=400)
    if not source.strip():
        return JSONResponse({"error": "source required"}, status_code=400)
    try:
        compile(source, f"<plugin:{name}>", "exec")
    except SyntaxError as exc:
        return JSONResponse({"error": f"syntax error: {exc}"}, status_code=400)
    INSTALLED_DIR.mkdir(parents=True, exist_ok=True)
    path = INSTALLED_DIR / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    try:
        info = plugin_sdk.load_file(path)
    except Exception as exc:
        path.unlink(missing_ok=True)
        return JSONResponse({"error": f"plugin failed to load: {exc}"}, status_code=400)
    _register_plugin(info)
    return JSONResponse({"ok": True, "plugin": _plugin_view(info)})


@app.post("/api/plugins/{name}/{action}")
async def toggle_plugin(name: str, action: str) -> JSONResponse:
    """Enable/disable a plugin without uninstalling it. Disabling removes its tools from the
    agent immediately and takes its routes offline (404); state persists across reboots."""
    if action not in ("enable", "disable"):
        return JSONResponse({"error": "unknown action"}, status_code=404)
    if not any(p.name == name for p in PLUGINS):
        return JSONResponse({"error": "unknown plugin"}, status_code=404)
    state = _load_plugin_state()
    state[name] = action == "enable"
    _save_plugin_state(state)
    _rebuild_tools()
    return JSONResponse({"ok": True, "name": name, "enabled": state[name]})


@app.delete("/api/plugins/{name}")
async def uninstall_plugin(name: str) -> JSONResponse:
    """Uninstall a runtime-installed plugin: delete its file and drop its tools/listing. Any
    already-mounted routes clear on the next reboot."""
    global PLUGINS
    path = INSTALLED_DIR / f"{name}.py"
    existing = next((p for p in PLUGINS if p.name == name and p.installed), None)
    if not existing or not path.exists():
        return JSONResponse({"error": "no such installed plugin"}, status_code=404)
    path.unlink(missing_ok=True)
    PLUGINS = [p for p in PLUGINS if p.name != name]
    state = _load_plugin_state()
    if state.pop(name, None) is not None:
        _save_plugin_state(state)
    _rebuild_tools()
    return JSONResponse({"ok": True, "note": "routes (if any) clear on next reboot"})


def _execute_tool(name: str, args: dict, ctx: ToolContext):
    """Run a tool by name, checking enabled plugin tools first, then the core registry.
    Returns the handler coroutine (awaited by the caller)."""
    plugin_tool = PLUGIN_TOOLS.get(name)
    if plugin_tool is not None:
        return plugin_tool["handler"](args or {}, ctx)
    return execute(name, args, ctx)


def _tool_allows_repeated_calls(name: str) -> bool:
    """Polling tools may legitimately repeat the same arguments while external work advances."""
    plugin_tool = PLUGIN_TOOLS.get(name)
    if plugin_tool is not None:
        return bool(plugin_tool.get("allow_repeats", False))
    return allows_repeated_calls(name)


# ── backend config (persisted under data dir) ──────────────────────────────────────
CONFIG_PATH = DATA_DIR / "backend_config.json"


def _load_backend_config() -> dict:
    defaults = {
        "max_rounds": 200,        # tool-round limit per exchange (200 ≈ no limit)
    }
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            defaults.update(stored)
        except Exception:
            pass
    return defaults


def _save_backend_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(CONFIG_PATH, json.dumps(cfg, indent=2))


@app.get("/api/agent/config")
async def get_backend_config() -> dict:
    return _load_backend_config()


@app.put("/api/agent/config")
async def set_backend_config(payload: dict) -> JSONResponse:
    cfg = _load_backend_config()
    if "max_rounds" in payload:
        try:
            val = int(payload["max_rounds"])
        except (TypeError, ValueError):  # bad input is a 400, never a 500 into the error tracker
            return JSONResponse({"error": "max_rounds must be a number (1–500)"}, status_code=400)
        if val < 1 or val > 500:
            return JSONResponse({"error": "max_rounds must be 1–500"}, status_code=400)
        cfg["max_rounds"] = val
    # Optional web-search provider (key stored app-side, never in the kernel).
    if isinstance(payload.get("search"), dict):
        s = payload["search"]
        cfg["search"] = {"provider": str(s.get("provider", "")).strip(),
                         "api_key": str(s.get("api_key", "")).strip()}
    # Optional knowledge settings (opt-in semantic search over uploaded docs).
    if isinstance(payload.get("knowledge"), dict):
        k = payload["knowledge"]
        cfg["knowledge"] = {"use_embeddings": bool(k.get("use_embeddings", False)),
                            "embed_model": str(k.get("embed_model", "")).strip()}
    _save_backend_config(cfg)
    return JSONResponse({"ok": True, "config": cfg})


# ── persistent feature settings (agent-extensible KV store under the data dir) ──────
# A free-form, NAMESPACED store for settings a feature must PERSIST across reboots and
# version switches (e.g. an integration's account + password). Unlike backend_config above
# (which is allow-listed), ANY namespace/key is accepted — so a feature the agent builds can
# save its own settings with no kernel or allow-list change. Secrets live only here, on the
# private data partition; never in the source tree or git. Server code reads real values via
# get_setting(); the HTTP API redacts secret-looking values so the UI never echoes them back.
SETTINGS_PATH = DATA_DIR / "settings.json"
_SECRET_KEY_RE = re.compile(r"pass|secret|token|api[_-]?key|credential|auth", re.I)


def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            d = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _save_settings(d: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(SETTINGS_PATH, json.dumps(d, indent=2))


def get_setting(namespace: str, key: str | None = None, default=None):
    """Server-side accessor for feature code — returns REAL values (incl. secrets).
    get_setting('gmail') → the whole namespace dict; get_setting('gmail', 'password')."""
    ns = _load_settings().get(namespace, {})
    if not isinstance(ns, dict):
        return default
    return ns if key is None else ns.get(key, default)


def set_setting(namespace: str, patch: dict) -> dict:
    """Merge `patch` into a namespace and persist it. Returns the updated namespace."""
    data = _load_settings()
    cur = data.get(namespace)
    ns = cur if isinstance(cur, dict) else {}
    ns.update(patch or {})
    data[namespace] = ns
    _save_settings(data)
    return ns


def _redact_settings(ns: dict) -> dict:
    """Mask secret-looking values for API responses (a non-empty secret → '••••••')."""
    return {k: ("••••••" if isinstance(v, str) and v and _SECRET_KEY_RE.search(k) else v)
            for k, v in (ns or {}).items()}


@app.get("/api/settings")
async def list_settings() -> dict:
    """All persisted settings namespaces, with secret-looking values redacted."""
    data = _load_settings()
    return {"settings": {ns: _redact_settings(v) for ns, v in data.items() if isinstance(v, dict)}}


@app.get("/api/settings/{namespace}")
async def get_settings_ns(namespace: str) -> dict:
    return {"namespace": namespace, "settings": _redact_settings(get_setting(namespace) or {})}


@app.put("/api/settings/{namespace}")
async def put_settings_ns(namespace: str, payload: dict) -> JSONResponse:
    """Merge-update a namespace. Empty-string values are dropped, so a settings form can
    leave a password field blank to keep the stored one. Returns the redacted namespace."""
    patch = {k: v for k, v in (payload or {}).items() if not (isinstance(v, str) and v == "")}
    ns = set_setting(namespace, patch)
    return JSONResponse({"ok": True, "namespace": namespace, "settings": _redact_settings(ns)})


@app.delete("/api/settings/{namespace}")
async def delete_settings_ns(namespace: str) -> dict:
    data = _load_settings()
    existed = namespace in data
    if existed:
        del data[namespace]
        _save_settings(data)
    return {"ok": existed}


# ── health & info ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    """POST target for the watchdog. Must return 200 for a version to be promoted.
    `preview` names the preview environment this process serves (None in production) —
    the UI shows a banner off it so you always know which env you're clicking around."""
    return {"status": "ok", "build": APP_BUILD,
            "preview": os.environ.get("QUINE_PREVIEW_NAME") or None}


@app.get("/api/app/info")
async def info() -> dict:
    return {
        "build": APP_BUILD,
        "data_dir": str(DATA_DIR),
        "syscall_url": SYSCALL_URL,
        "ui_built": (DIST / "index.html").exists(),
    }


# ── conversation storage (persisted under the data partition) ──────────────────────
def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip().lower()).strip("-")
    return s or "untitled"


def _is_running(cid: str) -> bool:
    """Return whether an agent run is in flight for this conversation."""
    hub = _RUN_HUBS.get(cid)
    return bool(hub is not None and hub.is_running())


def _convo_path(cid: str) -> pathlib.Path:
    return CONVO_DIR / (_slug(cid) + ".json")


def _load_convo(cid: str) -> dict | None:
    p = _convo_path(cid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_write_text(path: pathlib.Path, text: str) -> None:
    """Write a file so a kill can never leave it half-written.

    `write_text` truncates the file and then writes, so a hard stop in that window (Ctrl+C in the
    console, `docker stop`, power cut) left a 0-byte or torn JSON on disk. For a conversation that
    meant the chat DISAPPEARED — `_load_convo` can't parse it and the listing skips it. Writing a
    temp file, flushing it to the platter, then `os.replace`-ing it into place is atomic on both
    POSIX and Windows: a reader sees either the old file or the new one, never a broken one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _save_convo(convo: dict) -> None:
    CONVO_DIR.mkdir(parents=True, exist_ok=True)
    convo["updated"] = time.time()
    _atomic_write_text(_convo_path(convo["id"]), json.dumps(convo, indent=2))


@app.get("/api/agent/conversations")
async def list_conversations() -> dict:
    CONVO_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for p in CONVO_DIR.glob("*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        cid = (c or {}).get("id") if isinstance(c, dict) else None
        if not cid:
            continue  # one corrupt/hand-edited file must not 500 the whole listing
        items.append(
            {
                "id": cid,
                "title": c.get("title") or "Untitled",
                "updated": c.get("updated", 0),
                "messages": len(c.get("messages", [])),
                "running": _is_running(cid),
            }
        )
    items.sort(key=lambda x: x["updated"], reverse=True)
    return {"conversations": items}


@app.post("/api/agent/conversations")
async def create_conversation() -> dict:
    cid = "c" + uuid.uuid4().hex[:10]
    convo = {"id": cid, "title": "", "created": time.time(), "updated": time.time(), "messages": []}
    _save_convo(convo)
    return convo


@app.get("/api/agent/conversations/{cid}")
async def get_conversation(cid: str) -> JSONResponse:
    convo = _load_convo(cid)
    if convo is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Display history deliberately expands an assistant turn into the events a person saw: its
    # reasoning, each tool invocation/result, and its visible reply. The canonical conversation
    # keeps the provider's required assistant/tool pairing below; this is a safe UI projection.
    display = []
    for m in convo.get("messages", []):
        role = m.get("role")
        if role == "user" and (m.get("content") or "").strip():
            display.append({"role": "user", "content": m.get("content", "")})
            continue
        if role != "assistant":
            continue

        interrupted = bool(m.get("partial") or m.get("stopped"))
        reasoning = m.get("reasoning")
        if reasoning:
            entry = {"role": "thinking", "content": reasoning}
            if interrupted:
                entry["interrupted"] = True
            display.append(entry)

        content = m.get("content", "")
        if content.strip():
            entry = {"role": "assistant", "content": content}
            if m.get("usage"):
                entry["usage"] = m["usage"]
            if interrupted:
                entry["interrupted"] = True  # a reply that was cut short (kill/stop) — say so
            display.append(entry)

        # Never surface a tool result's content (it can contain file/source data). Persisted
        # summaries are already source-free; keep just name, sanitized args, and status.
        for index, tool in enumerate(m.get("tools") or []):
            name = tool.get("name")
            if not name:
                continue
            call_entry = {"role": "tool_call", "name": name,
                          "args": tool.get("args") or {}}
            if index == 0 and m.get("usage"):
                call_entry["usage"] = m["usage"]
            display.append(call_entry)
            display.append({"role": "tool_result", "name": name,
                            "status": tool.get("status") or "done"})
    # `running` lets a tab that just reloaded mid-run know the agent is still working here, so it
    # can show the activity indicator and a Stop button instead of a dead-looking transcript.
    return JSONResponse({"id": convo["id"], "title": convo.get("title"), "messages": display,
                         "running": _is_running(cid)})


@app.post("/api/agent/conversations/{cid}/stop")
async def stop_conversation_run(cid: str) -> dict:
    """Stop the agent run in flight in this conversation.

    Server-side on purpose: closing a sender stream cannot stop a run after a reload, and that run
    would keep spending tokens with no way to call it off. The run checks this between chunks and
    tool calls, saves the partial reply, and tells every viewer it stopped."""
    hub = _RUN_HUBS.get(cid)
    stopping = bool(hub and hub.stop())
    _gc_hub(cid)
    return {"ok": True, "stopping": stopping, "running": _is_running(cid)}


@app.delete("/api/agent/conversations/{cid}", response_model=None)
async def delete_conversation(cid: str) -> dict | JSONResponse:
    hub = _RUN_HUBS.get(cid)
    if hub is not None and hub.is_running():
        return JSONResponse(
            {"error": "stop the active run before deleting this conversation", "code": "busy"},
            status_code=409,
        )
    p = _convo_path(cid)
    if p.exists():
        p.unlink()
    return {"ok": True}


# ── token cost estimation (editable; USD per 1M tokens: prompt / cached / completion) ─
# Estimates only — override via backend_config.json {"pricing": {...}}. Keys match on a
# substring of the model id (e.g. "deepseek" matches "deepseek/deepseek-v4-flash").
DEFAULT_PRICING = {
    "deepseek": {"prompt": 0.14, "cached": 0.07, "completion": 0.28},
    "claude":   {"prompt": 3.00, "cached": 0.30, "completion": 15.00},
    "gpt":      {"prompt": 2.50, "cached": 1.25, "completion": 10.00},
    "default":  {"prompt": 0.14, "cached": 0.07, "completion": 0.28},
}


def _cached_of(u: dict) -> int:
    """Normalize a cached-prompt-token count across providers from a usage dict."""
    details = u.get("prompt_tokens_details") or {}
    return int(
        u.get("cached_tokens")
        or u.get("prompt_cache_hit_tokens")
        or (details.get("cached_tokens") if isinstance(details, dict) else 0)
        or u.get("cache_read_input_tokens")
        or 0
    )


def _price_for(model: str) -> dict:
    pricing = {**DEFAULT_PRICING, **(_load_backend_config().get("pricing") or {})}
    m = (model or "").lower()
    for key, rates in pricing.items():
        if key != "default" and key in m:
            return rates
    return pricing.get("default", DEFAULT_PRICING["default"])


def _cost_usd(prompt: int, cached: int, completion: int, model: str) -> float:
    r = _price_for(model)
    uncached = max((prompt or 0) - (cached or 0), 0)
    return round(
        (uncached * r["prompt"] + (cached or 0) * r["cached"] + (completion or 0) * r["completion"])
        / 1_000_000,
        4,
    )


# ── embedding cost (priced separately — embeddings cost far less than chat tokens) ──
# USD per 1M tokens, matched on a substring of the embed model id. Override via
# backend_config "embed_pricing". The knowledge tool records each embed call's usage to
# EMBED_USAGE_PATH; it's folded into token-usage + the usage-rollup so it gets billed.
DEFAULT_EMBED_PRICING = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "ada-002": 0.10,
    "voyage": 0.12,
    "mistral-embed": 0.10,
    "embed": 0.10,          # cohere embed-*
    "default": 0.10,
}
EMBED_USAGE_PATH = DATA_DIR / "embedding_usage.json"


def _embed_price(model: str) -> float:
    pricing = {**DEFAULT_EMBED_PRICING, **(_load_backend_config().get("embed_pricing") or {})}
    m = (model or "").lower()
    for key, rate in pricing.items():
        if key != "default" and key in m:
            return rate
    return pricing.get("default", DEFAULT_EMBED_PRICING["default"])


def _embed_cost_usd(tokens: int, model: str) -> float:
    return round((tokens or 0) * _embed_price(model) / 1_000_000, 6)


def _load_embed_usage(cutoff: float | None = None) -> dict:
    """Aggregate embedding token usage + estimated $ recorded by the knowledge tool."""
    agg = {"tokens": 0, "cost_usd": 0.0, "calls": 0}
    if not EMBED_USAGE_PATH.exists():
        return agg
    try:
        recs = json.loads(EMBED_USAGE_PATH.read_text(encoding="utf-8")) or []
    except Exception:
        return agg
    for r in recs:
        if cutoff is not None and (r.get("timestamp") or 0) < cutoff:
            continue
        t = int(r.get("tokens") or 0)
        agg["tokens"] += t
        agg["cost_usd"] += _embed_cost_usd(t, r.get("model", ""))
        agg["calls"] += 1
    agg["cost_usd"] = round(agg["cost_usd"], 6)
    return agg


def _parse_since(since: str | None) -> float | None:
    """Parse a billing-window start as epoch seconds or ISO-8601 (None = all time)."""
    if not since:
        return None
    try:
        return float(since)
    except (TypeError, ValueError):
        pass
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ── token usage (aggregated across all conversations) ─────────────────────────────
@app.get("/api/agent/token-usage")
async def token_usage() -> dict:
    """Aggregated token usage — incl. cached tokens + estimated $ cost — per conversation."""
    CONVO_DIR.mkdir(parents=True, exist_ok=True)
    per_convo = []
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
              "cached_tokens": 0, "cost_usd": 0.0}
    for p in sorted(CONVO_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(c, dict) or not c.get("id"):
            continue  # one corrupt/hand-edited file must not 500 the usage rollup
        ct = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
              "cached_tokens": 0, "rounds": 0}
        for m in c.get("messages", []):
            u = m.get("usage") or {}
            if not u:
                continue
            ct["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
            ct["completion_tokens"] += u.get("completion_tokens", 0) or 0
            ct["total_tokens"] += u.get("total_tokens", 0) or 0
            ct["cached_tokens"] += _cached_of(u)
            ct["rounds"] += 1
        if ct["total_tokens"] == 0:
            ct["total_tokens"] = ct["prompt_tokens"] + ct["completion_tokens"]
        if ct["prompt_tokens"] or ct["completion_tokens"]:
            cost = _cost_usd(ct["prompt_tokens"], ct["cached_tokens"], ct["completion_tokens"], c.get("model", ""))
            for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
                totals[k] += ct[k]
            totals["cost_usd"] += cost
            per_convo.append({
                "id": c["id"], "title": c.get("title") or "Untitled",
                "messages": len(c.get("messages", [])), "cost_usd": round(cost, 4), **ct,
            })
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    # Embeddings are a separate bucket (like self-mod) — the UI combines the three; keeping
    # them out of `totals` keeps the chat per-conversation table's footer consistent.
    return {"conversations": per_convo, "totals": totals, "embeddings": _load_embed_usage()}


# ── self-modify commit tracking ────────────────────────────────────────────────────
SELFMODIFY_COMMITS_PATH = DATA_DIR / "selfmodify_commits.json"


def _load_commits() -> list[dict]:
    if SELFMODIFY_COMMITS_PATH.exists():
        try:
            return json.loads(SELFMODIFY_COMMITS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_commits(commits: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SELFMODIFY_COMMITS_PATH.write_text(json.dumps(commits, indent=2), encoding="utf-8")


@app.get("/api/agent/selfmodify-commits")
async def selfmodify_commits() -> dict:
    """All recorded self-modify commits with token usage, cached tokens + estimated $ cost."""
    commits = _load_commits()
    # Self-mod uses the kernel's configured agent model; fetch it for pricing (best-effort).
    model = ""
    try:
        cfg = await _syscall_get("/config")
        model = ((cfg or {}).get("config", {}).get("agent", {}) or {}).get("model", "")
    except Exception:
        pass
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
              "cached_tokens": 0, "cost_usd": 0.0, "commits": len(commits)}
    out = []
    for c in commits:
        u = c.get("usage", {}) or {}
        cached = _cached_of(u)
        cost = _cost_usd(u.get("prompt_tokens", 0), cached, u.get("completion_tokens", 0), model)
        totals["prompt_tokens"] += u.get("prompt_tokens", 0)
        totals["completion_tokens"] += u.get("completion_tokens", 0)
        totals["total_tokens"] += u.get("total_tokens", 0)
        totals["cached_tokens"] += cached
        totals["cost_usd"] += cost
        out.append({**c, "usage": {**u, "cached_tokens": cached}, "cost_usd": round(cost, 4)})
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    return {"commits": out, "totals": totals}


@app.post("/api/agent/selfmodify-commits")
async def record_selfmodify_commit(payload: dict) -> JSONResponse:
    """Record a self-modify commit with its token usage. Called by the agent runtime."""
    try:
        prompt_tokens = int(payload.get("prompt_tokens", 0) or 0)
        completion_tokens = int(payload.get("completion_tokens", 0) or 0)
    except (TypeError, ValueError):  # bad input is a 400, never a 500 into the error tracker
        return JSONResponse({"error": "prompt_tokens/completion_tokens must be numbers"},
                            status_code=400)
    commits = _load_commits()
    entry = {
        "id": "sm_" + uuid.uuid4().hex[:8],
        "timestamp": time.time(),
        "message": (payload.get("message") or "").strip(),
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    commits.append(entry)
    _save_commits(commits)
    return JSONResponse({"ok": True, "commit": entry})


# ── usage rollup (display-only local spend observability) ───────────────────────────
@app.get("/api/agent/usage-rollup")
async def usage_rollup(since: str | None = None) -> dict:
    """Combined chat and self-mod token usage with estimated local provider cost.

    The instance owner pays the configured provider directly with their own key. This display uses
    the same pricing as the Usage tab so its figure matches the UI. Chat is windowed by conversation
    mtime and self-modification by commit timestamp, both best-effort.
    """
    cutoff = _parse_since(since)

    chat = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_tokens": 0, "cost_usd": 0.0, "conversations": 0}
    CONVO_DIR.mkdir(parents=True, exist_ok=True)
    for p in CONVO_DIR.glob("*.json"):
        if cutoff is not None and p.stat().st_mtime < cutoff:
            continue
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        pt = comp = tt = cached = 0
        for m in c.get("messages", []):
            u = m.get("usage") or {}
            if not u:
                continue
            pt += u.get("prompt_tokens", 0) or 0
            comp += u.get("completion_tokens", 0) or 0
            tt += u.get("total_tokens", 0) or 0
            cached += _cached_of(u)
        if not (pt or comp):
            continue
        chat["prompt_tokens"] += pt
        chat["completion_tokens"] += comp
        chat["total_tokens"] += tt or (pt + comp)
        chat["cached_tokens"] += cached
        chat["cost_usd"] += _cost_usd(pt, cached, comp, c.get("model", ""))
        chat["conversations"] += 1
    chat["cost_usd"] = round(chat["cost_usd"], 4)

    model = ""
    try:
        cfg = await _syscall_get("/config")
        model = ((cfg or {}).get("config", {}).get("agent", {}) or {}).get("model", "")
    except Exception:
        pass
    sm = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
          "cached_tokens": 0, "cost_usd": 0.0, "commits": 0}
    for c in _load_commits():
        if cutoff is not None and (c.get("timestamp") or 0) < cutoff:
            continue
        u = c.get("usage", {}) or {}
        cached = _cached_of(u)
        sm["prompt_tokens"] += u.get("prompt_tokens", 0)
        sm["completion_tokens"] += u.get("completion_tokens", 0)
        sm["total_tokens"] += u.get("total_tokens", 0)
        sm["cached_tokens"] += cached
        sm["cost_usd"] += _cost_usd(u.get("prompt_tokens", 0), cached, u.get("completion_tokens", 0), model)
        sm["commits"] += 1
    sm["cost_usd"] = round(sm["cost_usd"], 4)

    emb = _load_embed_usage(cutoff)
    totals = {k: chat[k] + sm[k]
              for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")}
    totals["prompt_tokens"] += emb["tokens"]   # embeddings consume input tokens
    totals["total_tokens"] += emb["tokens"]
    totals["cost_usd"] = round(chat["cost_usd"] + sm["cost_usd"] + emb["cost_usd"], 6)
    return {"since": since, "generated_at": time.time(),
            "totals": totals, "chat": chat, "selfmod": sm, "embeddings": emb}


# ── self-mod conversation session (shared with the runtime worker via DATA_DIR) ────
SELFMOD_SESSION_PATH = DATA_DIR / "agent_session.json"
SELFMOD_CONVO_PATH = DATA_DIR / "agent_conversation.jsonl"
# Durable, task-keyed transcript snapshots written by the runtime on each commit. The version
# registry links a version's sha → its task_id, so these are the conversations a "continue from
# a commit" run resumes from. (Distinct from the legacy timestamp archives above.)
SELFMOD_CONVOS_DIR = DATA_DIR / "selfmod_convos"


@app.get("/api/agent/selfmod-session")
async def selfmod_session() -> dict:
    """Whether the self-mod agent has a live (uncommitted) conversation to continue."""
    sess = {}
    if SELFMOD_SESSION_PATH.exists():
        try:
            sess = json.loads(SELFMOD_SESSION_PATH.read_text(encoding="utf-8"))
        except Exception:
            sess = {}
    n = 0
    if SELFMOD_CONVO_PATH.exists():
        try:
            n = sum(1 for ln in SELFMOD_CONVO_PATH.read_text(encoding="utf-8").splitlines() if ln.strip())
        except Exception:
            n = 0
    return {"active": bool(sess) and not sess.get("committed"),
            "committed": bool(sess.get("committed")), "messages": n}


@app.post("/api/agent/selfmod-session/reset")
async def reset_selfmod_session() -> dict:
    """Retire the current self-mod conversation (archived, not deleted) so the NEXT
    self-modify request starts a brand-new conversation."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    archived = False
    if SELFMOD_CONVO_PATH.exists():
        try:
            SELFMOD_CONVO_PATH.rename(DATA_DIR / f"agent_conversation.{int(time.time())}.jsonl")
            archived = True
        except Exception:
            try:
                SELFMOD_CONVO_PATH.unlink()
                archived = True
            except Exception:
                pass
    try:
        SELFMOD_SESSION_PATH.write_text(
            json.dumps({"active": False, "committed": True}), encoding="utf-8"
        )
    except Exception:
        pass
    return {"ok": True, "archived": archived}


# ── self-mod conversation history (archived transcripts the user can revisit/reuse) ──
# Each retired conversation is kept as DATA_DIR/agent_conversation.{ts}.jsonl. These let
# the user reopen a past (e.g. cancelled) task to improve its prompt or resume its context.
def _selfmod_convo_file(cid: str) -> pathlib.Path | None:
    """Resolve a conversation id to its file, guarding against path traversal:
      • 'active'  → the live convo;
      • all-digits → a legacy timestamp archive (agent_conversation.<ts>.jsonl);
      • otherwise alphanumeric → a task-keyed snapshot (selfmod_convos/<task_id>.jsonl), the
        transcript addressable from the version that task produced.
    Anything else (path separators, dots, …) is rejected."""
    if cid == "active":
        return SELFMOD_CONVO_PATH
    if cid.isdigit():
        return DATA_DIR / f"agent_conversation.{cid}.jsonl"
    if cid.isalnum():
        return SELFMOD_CONVOS_DIR / f"{cid}.jsonl"
    return None


def _read_convo_msgs(path: pathlib.Path) -> list[dict]:
    msgs: list[dict] = []
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                msgs.append(json.loads(ln))
            except Exception:
                pass
    except Exception:
        pass
    return msgs


def _first_user_prompt(msgs: list[dict]) -> str:
    for m in msgs:
        if m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else json.dumps(c)
    return ""


@app.get("/api/agent/selfmod-conversations")
async def list_selfmod_conversations() -> dict:
    """Self-mod conversations, newest first: id, original prompt, message count, ts, and — for
    committed runs — the `task` id (join key to the version it produced, so the UI can offer
    "Continue from vN"). Task-keyed commit snapshots are listed first; legacy timestamp archives
    (e.g. cancelled/uncommitted runs with no version) follow."""
    items = []
    # Task-keyed snapshots (one per committed version) — id == task_id, joinable to a version.
    if SELFMOD_CONVOS_DIR.exists():
        for p in SELFMOD_CONVOS_DIR.glob("*.jsonl"):
            task_id = p.stem
            if not task_id.isalnum():
                continue
            msgs = _read_convo_msgs(p)
            try:
                ts = p.stat().st_mtime
            except OSError:
                ts = 0.0
            items.append({
                "id": task_id,
                "task": task_id,
                "ts": ts,
                "messages": sum(1 for m in msgs if m.get("role") != "system"),
                "prompt": _first_user_prompt(msgs)[:2000],
            })
    # Legacy timestamp archives (uncommitted/cancelled conversations, not tied to a version).
    for p in DATA_DIR.glob("agent_conversation.*.jsonl"):
        suffix = p.name[len("agent_conversation."):-len(".jsonl")]
        if not suffix.isdigit():
            continue
        msgs = _read_convo_msgs(p)
        items.append({
            "id": suffix,
            "task": None,
            "ts": float(suffix),
            "messages": sum(1 for m in msgs if m.get("role") != "system"),
            "prompt": _first_user_prompt(msgs)[:2000],
        })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return {"conversations": items}


@app.get("/api/agent/selfmod-conversations/{cid}")
async def get_selfmod_conversation(cid: str) -> JSONResponse:
    """A conversation's transcript (system + tool messages dropped, content capped) for viewing.

    `tool`-role messages are the RESULTS of the agent's read_file/search/validate calls — i.e.
    the app's own source and diffs. Surfacing them would let a user read the code the harness is
    built from, so they're withheld; only the user/assistant turns are shown. (write_file/edit_file
    payloads live in the assistant message's tool_calls, which this endpoint already omits.)"""
    p = _selfmod_convo_file(cid)
    if p is None or not p.exists():
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    out = []
    for m in _read_convo_msgs(p):
        role = m.get("role", "")
        if role in ("system", "tool"):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            content = json.dumps(content)
        out.append({"role": role, "content": content[:4000],
                    "has_tool_calls": bool(m.get("tool_calls"))})
    return JSONResponse({"id": cid, "messages": out})


@app.post("/api/agent/selfmod-conversations/{cid}/resume")
async def resume_selfmod_conversation(cid: str) -> JSONResponse:
    """Reopen an archived conversation as the active one so the next change request
    continues it with full context. The current active convo (if any) is archived first,
    so nothing is lost — this is a clean swap, not an overwrite."""
    src = _selfmod_convo_file(cid)
    if cid == "active" or src is None or not src.exists():
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SELFMOD_CONVO_PATH.exists():
        try:
            os.replace(SELFMOD_CONVO_PATH, DATA_DIR / f"agent_conversation.{int(time.time())}.jsonl")
        except Exception:
            try:
                SELFMOD_CONVO_PATH.unlink()
            except Exception:
                pass
    # A task-keyed commit snapshot is durable (a version may be continued more than once), so
    # COPY it into the active slot; a legacy timestamp archive is one-shot, so MOVE it.
    try:
        is_snapshot = src.parent == SELFMOD_CONVOS_DIR
    except Exception:
        is_snapshot = False
    if is_snapshot:
        shutil.copyfile(src, SELFMOD_CONVO_PATH)
    else:
        os.replace(src, SELFMOD_CONVO_PATH)  # the archive becomes the live conversation again
    try:
        SELFMOD_SESSION_PATH.write_text(
            json.dumps({"active": True, "committed": False, "started": time.time()}),
            encoding="utf-8")
    except Exception:
        pass
    n = sum(1 for ln in _read_convo_msgs(SELFMOD_CONVO_PATH))
    return JSONResponse({"ok": True, "messages": n})


# ── artifacts CRUD (the Run tab's artifacts panel) ────────────────────────────────
@app.get("/api/artifacts")
async def list_artifacts() -> dict:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(NOTES_DIR.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        snippet = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        items.append({"title": p.stem, "chars": len(text), "snippet": snippet[:80]})
    return {"artifacts": items}


@app.get("/api/artifacts/{title:path}")
async def get_artifact(title: str) -> JSONResponse:
    slug = _slug(title)
    p = NOTES_DIR / (slug + ".md")
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"title": slug, "body": p.read_text(encoding="utf-8")})


@app.put("/api/artifacts/{title:path}")
async def save_artifact(title: str, payload: dict) -> dict:
    slug = _slug(title)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    body = (payload or {}).get("body", "")
    p = NOTES_DIR / (slug + ".md")
    p.write_text(body, encoding="utf-8")
    return {"title": slug, "chars": len(body)}


@app.delete("/api/artifacts/{title:path}")
async def delete_artifact(title: str) -> dict:
    slug = _slug(title)
    p = NOTES_DIR / (slug + ".md")
    if p.exists():
        p.unlink()
    return {"ok": True}


# ── development workspace (the dev/ sandbox for user-built software) ───────────────
# DEV_DIR is defined with the other data-partition paths at the top of this module.


def _has_user_files(d: pathlib.Path) -> bool:
    """True if `d` holds anything the user would miss (ignores .gitkeep and friends)."""
    try:
        return any(p.is_file() and not p.name.startswith(".") for p in d.rglob("*"))
    except OSError:
        return False


def _rescue_legacy_dev_workspace() -> None:
    """One-shot: import a pre-fix development/ workspace onto the data partition.

    Before the fix the sandbox lived inside the app slot, which is deleted and re-extracted
    from git on every version switch — so a self-mod silently destroyed everything the agent
    had built. When THIS version first boots it runs in the *inactive* slot while the old one
    is still on disk intact, so its files are still rescuable: copy them across before the old
    slot is reclaimed. Idempotent (only runs while the new location is empty) and never raises —
    boot must not hinge on a best-effort migration.
    """
    try:
        if _has_user_files(DEV_DIR):
            return
        legacy = [HERE / "development"]                          # this slot / a source-tree run
        if HERE.parent.is_dir():                                 # sibling slots (a ↔ b, previews)
            legacy += [d / "development" for d in HERE.parent.iterdir()
                       if d.is_dir() and d != HERE]
        found = [p for p in legacy if p.is_dir() and _has_user_files(p)]
        if not found:
            return
        src = max(found, key=lambda p: p.stat().st_mtime)        # the most recently used one
        DEV_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, DEV_DIR, dirs_exist_ok=True)
    except Exception:
        pass


_rescue_legacy_dev_workspace()


def _walk_dev(rel_dir: str = "") -> list:
    """Recursively list files/dirs in the development workspace, relative to dev root."""
    target = DEV_DIR / rel_dir if rel_dir else DEV_DIR
    if not target.is_dir():
        return []
    entries = []
    for child in sorted(target.iterdir()):
        if child.name.startswith("."):
            continue
        rel = child.relative_to(DEV_DIR)
        if child.is_dir():
            entries.append({"name": str(rel), "kind": "dir", "children": _walk_dev(str(rel))})
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            entries.append({"name": str(rel), "kind": "file", "size": size})
    return entries


@app.get("/api/development")
async def list_dev_workspace() -> dict:
    """List the development workspace contents as a tree."""
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    tree = _walk_dev()
    return {"root": str(DEV_DIR), "tree": tree}


@app.get("/api/development/files/{path:path}")
async def get_dev_file(path: str) -> JSONResponse:
    """Read a file from the development workspace."""
    target = (DEV_DIR / path).resolve()
    try:
        target.relative_to(DEV_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "path escapes"}, status_code=400)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"path": path, "content": text, "size": len(text)})


@app.put("/api/development/files/{path:path}")
async def write_dev_file(path: str, payload: dict) -> JSONResponse:
    """Write a file to the development workspace."""
    target = (DEV_DIR / path).resolve()
    try:
        target.relative_to(DEV_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "path escapes"}, status_code=400)
    content = (payload or {}).get("content", "")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return JSONResponse({"path": path, "size": len(content), "ok": True})


@app.delete("/api/development/files/{path:path}")
async def delete_dev_file(path: str) -> JSONResponse:
    """Delete a file or empty directory from the development workspace."""
    target = (DEV_DIR / path).resolve()
    try:
        target.relative_to(DEV_DIR.resolve())
    except ValueError:
        return JSONResponse({"error": "path escapes"}, status_code=400)
    if not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(str(target))
    return JSONResponse({"path": path, "ok": True})


@app.get("/api/development/export")
async def export_dev_workspace() -> StreamingResponse:
    """Take-out: download everything the agent built in development/ as a .zip so created
    software is portable. Hidden files (.git, …) are skipped, matching the file listing."""
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DEV_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(DEV_DIR)
            if any(part.startswith(".") for part in rel.parts):
                continue
            zf.write(path, arcname=str(rel))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=development.zip"},
    )


# ── knowledge base (uploaded docs for the Run agent's search_knowledge tool) ────────
KNOWLEDGE_DIR = DATA_DIR / "knowledge"


def _extract_text(filename: str, raw: bytes) -> str:
    """Best-effort text extraction. PDFs via pypdf (optional dep); everything else is
    decoded as text (utf-8, then latin-1 as a fallback)."""
    suffix = pathlib.Path(filename).suffix.lower()
    if suffix == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(raw))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception:
            return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


@app.get("/api/knowledge")
async def list_knowledge() -> dict:
    """Uploaded documents with chunk + char counts."""
    return {"documents": kb.list_docs(KNOWLEDGE_DIR)}


# Cap knowledge ingest size so a single upload/paste can't exhaust the kernel container's memory.
# Generous by default; override with QUINE_MAX_UPLOAD_BYTES.
_MAX_UPLOAD_BYTES = int(os.environ.get("QUINE_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)) or 25 * 1024 * 1024)


@app.post("/api/knowledge/text")
async def add_knowledge_text(payload: dict) -> JSONResponse:
    """Ingest a document from JSON {title, content} — dependency-free path (also handy for
    the agent / programmatic use)."""
    title = (payload.get("title") or "").strip()
    content = payload.get("content") or ""
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)
    if not content.strip():
        return JSONResponse({"error": "content required"}, status_code=400)
    if len(content.encode("utf-8", "ignore")) > _MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": f"content too large (limit {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"},
            status_code=413)
    return JSONResponse({"ok": True, "document": kb.ingest(KNOWLEDGE_DIR, title, content)})


@app.post("/api/knowledge/upload")
async def upload_knowledge(file: UploadFile = File(...)) -> JSONResponse:
    """Ingest an uploaded file (text/markdown/csv/code, or PDF). Chunks + persists it."""
    # Read one byte past the limit so we can reject oversized files without buffering the whole
    # thing into memory (DoS guard); a legitimate file is well under the cap.
    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": f"file too large (limit {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"},
            status_code=413)
    name = file.filename or "untitled"
    text = _extract_text(name, raw)
    if not text.strip():
        return JSONResponse({"error": "could not extract any text from the file"},
                            status_code=400)
    info = kb.ingest(KNOWLEDGE_DIR, pathlib.Path(name).stem, text)
    return JSONResponse({"ok": True, "document": info})


@app.delete("/api/knowledge/{title:path}")
async def delete_knowledge(title: str) -> dict:
    return {"ok": kb.delete_doc(KNOWLEDGE_DIR, title)}


# ── instructions: the in-app manual (shipped seeds overlaid by DATA_DIR overrides) ──
# Read merges app/instructions/*.md with DATA_DIR/instructions/*.md; writes go to DATA_DIR
# so user edits persist without touching source. The agent maintains these via its
# *_instruction tools; the Self-Modify agent edits the app/instructions/*.md seeds.
@app.get("/api/instructions")
async def list_instructions() -> dict:
    return {"documents": ins.list_all(DATA_DIR)}


@app.get("/api/instructions/{slug}")
async def get_instruction(slug: str) -> JSONResponse:
    doc = ins.get_one(DATA_DIR, slug)
    if doc is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(doc)


@app.put("/api/instructions/{slug}")
async def put_instruction(slug: str, payload: dict) -> JSONResponse:
    content = payload.get("content") or ""
    if not content.strip():
        return JSONResponse({"error": "content required"}, status_code=400)
    info = ins.upsert(DATA_DIR, slug, content,
                      title=payload.get("title", ""), category=payload.get("category", ""),
                      order=payload.get("order"))
    return JSONResponse({"ok": True, "document": info})


@app.delete("/api/instructions/{slug}")
async def delete_instruction(slug: str) -> dict:
    """Drop a DATA_DIR override; reverts to the shipped seed if one exists."""
    return ins.remove(DATA_DIR, slug)


# ── error tracker (the harness's "Sentry" — capture/store logic in errorlog.py) ─────
@app.get("/api/errors")
async def list_errors(since: str | None = None, version: str | None = None,
                      include_resolved: bool = False) -> dict:
    """Grouped errors, unified: app-side records (errors.jsonl) plus boot/health failures
    merged from the kernel's version registry (best-effort — offline shows app-side only)."""
    ver = (version or "").strip() or None
    groups = errorlog.list_groups(since=_parse_since(since), version=ver,
                                  include_resolved=include_resolved)
    boot: list[dict] = []
    try:
        data = await _syscall_get("/versions")
        boot = errorlog.boot_groups(
            (data or {}).get("versions") or [], include_resolved=include_resolved
        )
        if ver:
            boot = [g for g in boot if any(v.startswith(ver) for v in g["versions"])]
    except Exception:
        boot = []
    return {"groups": boot + groups,
            "summary": errorlog.unresolved_summary(errorlog.APP_VERSION or None),
            "active_version": errorlog.APP_VERSION}


@app.get("/api/errors/{fingerprint}")
async def get_error_group(fingerprint: str) -> JSONResponse:
    """Newest occurrences (full tracebacks) of one error group."""
    occurrences = errorlog.get_group(fingerprint)
    if not occurrences:
        return JSONResponse({"error": "no such error group"}, status_code=404)
    return JSONResponse({"fingerprint": fingerprint, "occurrences": occurrences})


@app.post("/api/errors/report")
async def report_error(payload: dict) -> JSONResponse:
    """Manual capture — for frontend JS errors and any code that prefers HTTP over
    `from errorlog import capture` (e.g. an agent-built external worker)."""
    message = ((payload or {}).get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    ctx = payload.get("context")
    entry = errorlog.record(
        None, source=str(payload.get("source") or "manual")[:40],
        exc_type=str(payload.get("exc_type") or "Error"), message=message,
        traceback=str(payload.get("traceback") or ""),
        route=(str(payload.get("route")) if payload.get("route") else None),
        context=ctx if isinstance(ctx, dict) else {},
    )
    if entry is None:
        return JSONResponse({"error": "could not record"}, status_code=500)
    return JSONResponse({"ok": True, "id": entry["id"], "fingerprint": entry["fingerprint"]})


@app.post("/api/errors/{fingerprint}/resolve")
async def resolve_error_group(fingerprint: str, payload: dict | None = None) -> JSONResponse:
    if not errorlog.resolve(fingerprint, note=((payload or {}).get("note") or "")):
        return JSONResponse({"error": "no such error group"}, status_code=404)
    return JSONResponse({"ok": True, "fingerprint": fingerprint, "resolved": True})


@app.post("/api/errors/{fingerprint}/unresolve")
async def unresolve_error_group(fingerprint: str) -> JSONResponse:
    if not errorlog.unresolve(fingerprint):
        return JSONResponse({"error": "group is not resolved"}, status_code=404)
    return JSONResponse({"ok": True, "fingerprint": fingerprint, "resolved": False})


@app.delete("/api/errors")
async def clear_errors() -> dict:
    boot_fingerprints: list[str] = []
    try:
        data = await _syscall_get("/versions")
        versions = (data or {}).get("versions") or []
        boot_fingerprints = [
            group["fingerprint"]
            for group in errorlog.boot_groups(
                versions, limit=max(len(versions), 1), include_resolved=True
            )
        ]
    except Exception:
        pass
    errorlog.clear(boot_fingerprints)
    return {"ok": True}


# ── the agent: tools + a streaming tool-using loop over llm_stream ──────────────────
# Tools live in the extensible `tools/` package; add new ones there, not here.
AGENT_SYSTEM = (
    "You are the in-app assistant for Quine. Be concise and helpful. You can use "
    "tools to save/list/read the user's artifacts (stored on the server) and to read the "
    "harness status, version history, and the audit log. Use tools only "
    "when they help.\n\n"
    "You can access the web: web_search finds pages and web_fetch reads a URL's text — use "
    "them for current information or anything outside your training data. You can also "
    "search the user's uploaded documents with search_knowledge — use it to answer "
    "questions about files they've provided.\n\n"
    "You also have development tools (dev_*) for the development/ sandbox — a dedicated "
    "workspace for building arbitrary software projects. These let you read, write, edit, "
    "list files, and run shell commands inside development/. Use them when the user asks "
    "you to build, create, or develop software. When a background command is still running, "
    "use bg_wait to actually wait; do not pretend to wait or repeatedly poll bg_read_log.\n\n"
    "The app has an Instructions tab — a living USER manual with one doc per tab/feature. "
    "Keep it accurate with your instruction tools: list_instructions, read_instruction, "
    "write_instruction, delete_instruction. Whenever you add a new capability the user can "
    "see (a tab, a tool, a workflow) or change how something works, write or update the "
    "matching doc with write_instruction (concise markdown; set a clear slug, title, and "
    "category) so the manual never drifts from the app. Write these docs FOR THE END USER — "
    "what the feature does and how to use it, in plain language — never about your own "
    "tools, prompts, or internals."
)

async def _syscall_get(path: str) -> dict:
    if not SYSCALL_URL:
        return {"error": "no syscall url"}
    async with httpx.AsyncClient(timeout=15) as c:
        return (await c.get(SYSCALL_URL + path, headers=_syscall_headers())).json()


async def _syscall_post(path: str, payload: dict) -> dict:
    """POST a kernel syscall (e.g. /llm_call for embeddings). Keys stay in the kernel."""
    if not SYSCALL_URL:
        return {"ok": False, "error": "no syscall url"}
    async with httpx.AsyncClient(timeout=120) as c:
        return (await c.post(SYSCALL_URL + path, json=payload, headers=_syscall_headers())).json()


# What the Run-agent tools are allowed to touch: notes/data dirs, read-only syscalls, the
# app's backend config (web-search key), and a POST syscall (embeddings via /llm_call).
TOOL_CTX = ToolContext(
    notes_dir=NOTES_DIR, syscall_get=_syscall_get, dev_dir=DEV_DIR,
    data_dir=DATA_DIR, config_get=_load_backend_config, syscall_post=_syscall_post,
)


async def _kernel_stream(payload: dict):
    """Yield parsed chunk dicts from the kernel's llm_stream SSE."""
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", SYSCALL_URL + "/llm_stream", json=payload,
                                 headers=_syscall_headers()) as resp:
            async for line in resp.aiter_lines():
                line = line.strip()
                if line.startswith("data:"):
                    body = line[5:].strip()
                    if body:
                        try:
                            yield json.loads(body)
                        except json.JSONDecodeError:
                            pass


# ── liveness of a run: silence is an event, not a state ──────────────────────────────
# A model call has no deadline of its own: a provider that accepts the request and then goes
# quiet (or a dropped connection that never resets) would hang the run forever — and the UI,
# which can only render the frames it receives, would keep saying "working" while nothing at all
# was happening. These turn silence into something the run can act on and the user can see.
STREAM_IDLE_SECONDS = 45.0    # no chunk for this long ⇒ the model call is stalled, not slow
STATUS_INTERVAL = 5.0         # emit proof-of-life to the UI this often while waiting
STREAM_MAX_RETRIES = 2        # re-issue a stalled call that produced NOTHING (safe to repeat)
RETRY_BACKOFF_SECONDS = 2.0   # linear backoff between those retries
LOOP_REPEAT_LIMIT = 3         # identical tool call this many times in a row ⇒ the agent is looping
LIVE_SAVE_SECONDS = 1.0       # how often the in-flight reply is checkpointed to disk

_TOOL_DETAIL_OMIT_KEYS = {
    "content", "code", "text", "old_str", "new_str", "patch", "body", "data", "source"
}


class _ModelStalled(Exception):
    """The model produced nothing for STREAM_IDLE_SECONDS — the call is hung, not slow."""

    def __init__(self, silent_for: float) -> None:
        super().__init__(f"no response from the model for {int(silent_for)}s")
        self.silent_for = silent_for


class _RunStopped(Exception):
    """Stop was pressed (by any viewer) — wind the run down and keep what it produced."""


def _public_tool_args(args: dict) -> dict:
    """Small, source-safe argument preview for Run-tab tool chips."""
    public = {}
    for key, value in (args or {}).items():
        if key in _TOOL_DETAIL_OMIT_KEYS and isinstance(value, str):
            public[key] = f"[{len(value)} chars omitted]"
        elif isinstance(value, str):
            public[key] = value if len(value) <= 160 else value[:157] + "..."
        elif isinstance(value, (int, float, bool)) or value is None:
            public[key] = value
        elif isinstance(value, list):
            public[key] = f"[{len(value)} items]"
        elif isinstance(value, dict):
            public[key] = f"{{{len(value)} keys}}"
        else:
            public[key] = str(value)
    return public


async def _model_chunks(payload: dict) -> AsyncIterator[tuple[str, Any]]:
    """Stream a model call as ("chunk", data) — plus ("status", seconds_silent) pings whenever it
    goes quiet, so the caller can prove to the user that it's still waiting. Raises _ModelStalled
    once the silence passes STREAM_IDLE_SECONDS."""
    stream = _kernel_stream(payload)
    pending: asyncio.Task | None = None
    try:
        while True:
            pending = asyncio.ensure_future(stream.__anext__())
            silent = 0.0
            while True:
                try:
                    # shield: a wait_for timeout must not cancel the in-flight read — we go back
                    # to waiting on the SAME task after each status ping.
                    chunk = await asyncio.wait_for(asyncio.shield(pending), timeout=STATUS_INTERVAL)
                except asyncio.TimeoutError:
                    silent += STATUS_INTERVAL
                    if silent >= STREAM_IDLE_SECONDS:
                        raise _ModelStalled(silent) from None
                    yield ("status", silent)
                    continue
                except StopAsyncIteration:  # the model finished this call
                    return
                break
            yield ("chunk", chunk)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        with contextlib.suppress(Exception):
            await stream.aclose()


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj) + "\n\n"


# ── live run fan-out: one agent run per conversation, broadcast to every viewer ──────
# Several browser tabs may have the same conversation open. The agent run is driven once and its
# event stream is fanned out to every open viewer: the sender streams it back on the POST, while
# other viewers follow the same run live via GET .../stream. A viewer who joins mid-run replays the
# frames so far (from `buffer`), then follows live. Keyed by conversation id; the hub is discarded
# once it is idle with no subscribers. In-memory state matches the single-process app model, so no
# cross-process bus is needed.
RUN_START_GRACE_SECONDS = 5.0  # release a claimed run whose response body never starts


class _RunHub:
    def __init__(self, cid: str) -> None:
        self.cid = cid
        self.subscribers: set[asyncio.Queue[str]] = set()
        self.buffer: list[str] = []   # SSE frames of the CURRENT run, for mid-run catch-up
        self.active: bool = False
        self.stopping: bool = False   # a Stop was requested — the run checks this and winds down
        self.generation = 0
        self._run_task: asyncio.Task | None = None
        self._start_timer: asyncio.TimerHandle | None = None
        self._stop_event: asyncio.Event | None = None

    def _cancel_start_timer(self) -> None:
        if self._start_timer is not None:
            self._start_timer.cancel()
            self._start_timer = None

    def begin(self) -> int:
        """Claim the hub for a fresh run (resets the catch-up buffer).

        The claim happens before FastAPI starts iterating the StreamingResponse so concurrent sends
        still get a 409. If the client disappears in that tiny gap, `start()` is never called; the
        grace timer releases that otherwise-permanent claim and tells any watchers the run ended.
        """
        self._cancel_start_timer()
        self.generation += 1
        generation = self.generation
        self.buffer = []
        self.active = True
        self.stopping = False
        self._run_task = None
        self._stop_event = asyncio.Event()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Tests and administrative callers may claim a hub synchronously to model a busy run.
            # Real response claims always happen inside the ASGI loop and receive the safety timer.
            self._start_timer = None
        else:
            self._start_timer = loop.call_later(
                RUN_START_GRACE_SECONDS, self._expire_unstarted, generation,
            )
        return generation

    def start(self, generation: int) -> bool:
        """Attach the task that is actually driving the claimed response stream."""
        if generation != self.generation or not self.active or self.stopping:
            return False
        self._cancel_start_timer()
        self._run_task = asyncio.current_task()
        return True

    def _expire_unstarted(self, generation: int) -> None:
        self._start_timer = None
        if generation != self.generation or not self.active or self._run_task is not None:
            return
        self.stopping = True
        self.publish(_sse({"type": "stopped"}))
        self.publish(_sse({"type": "done"}))
        self.end(generation)
        _gc_hub(self.cid)

    def end(self, generation: int | None = None) -> None:
        if generation is not None and generation != self.generation:
            return
        self._cancel_start_timer()
        self.active = False
        self.stopping = False
        self._run_task = None
        if self._stop_event is not None:
            self._stop_event.set()
        self._stop_event = None

    def is_running(self) -> bool:
        """Return live state, repairing a claim whose driver task died without finalizing."""
        if self.active and self._run_task is not None and self._run_task.done():
            self.publish(_sse({"type": "done"}))
            self.end(self.generation)
        return self.active

    def stop(self) -> bool:
        """Ask the in-flight run to wind down.

        A claimed response can be stopped before its body starts. There is no driver task in that
        case, so finish the hub here instead of waiting forever for a generator that never ran.
        """
        if not self.is_running():
            return False
        self.stopping = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._run_task is None:
            self.publish(_sse({"type": "stopped"}))
            self.publish(_sse({"type": "done"}))
            self.end(self.generation)
        return True

    async def wait_stopped(self) -> None:
        """Wait until Stop is requested or this run ends."""
        if self.stopping or not self.is_running():
            return
        ev = self._stop_event
        if ev is None:
            while self.is_running() and not self.stopping:
                await asyncio.sleep(0.05)
            return
        await ev.wait()

    def publish(self, frame: str) -> None:
        """Fan a raw SSE frame out to every subscriber (and buffer it for late joiners)."""
        if self.active:
            self.buffer.append(frame)
        for q in list(self.subscribers):
            q.put_nowait(frame)

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        if self.is_running():
            for frame in self.buffer:  # catch a late joiner up to the live edge
                q.put_nowait(frame)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self.subscribers.discard(q)


_RUN_HUBS: dict[str, _RunHub] = {}


def _hub_for(cid: str) -> _RunHub:
    hub = _RUN_HUBS.get(cid)
    if hub is None:
        hub = _RunHub(cid)
        _RUN_HUBS[cid] = hub
    return hub


def _gc_hub(cid: str) -> None:
    """Drop a conversation's hub once nothing is using it (idle + no subscribers), so watching
    then leaving a conversation doesn't leak an entry per conversation."""
    hub = _RUN_HUBS.get(cid)
    if hub is not None and not hub.is_running() and not hub.subscribers:
        _RUN_HUBS.pop(cid, None)


def _sanitize_history(msgs: list[dict]) -> list[dict]:
    """Enforce the provider's tool-message contract before sending history upstream.

    Providers require every `tool` message to directly follow the assistant message
    whose `tool_calls` opened its id, and every opened tool_call to have a response.
    This re-pairs each assistant's tool_calls with their results (repairing the old bug
    where results were saved *before* the assistant message), drops orphaned `tool`
    messages, and fills any missing result so a dangling tool_call can't be sent."""
    from collections import defaultdict, deque

    pool: dict[str, deque] = defaultdict(deque)
    for m in msgs:
        if m.get("role") == "tool":
            pool[str(m.get("tool_call_id", ""))].append(m)

    out: list[dict] = []
    for m in msgs:
        role = m.get("role")
        if role == "tool":
            continue  # emitted (if valid) right after its owning assistant, below
        if m.get("partial") or m.get("stopped"):
            # Bookkeeping flags on a checkpointed/interrupted reply — ours, not the provider's.
            m = {k: v for k, v in m.items() if k not in ("partial", "stopped")}
        out.append(m)
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tid = str(tc.get("id", ""))
                q = pool.get(tid)
                if q:
                    out.append(q.popleft())
                else:
                    out.append({"role": "tool", "tool_call_id": tid,
                                "content": "(no result recorded)"})
    return out


@app.post("/api/agent/conversations/{cid}/message")
async def send_message(cid: str, payload: dict):
    convo = _load_convo(cid)
    if convo is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    content = (payload or {}).get("content", "").strip()
    model = (payload or {}).get("model") or ""
    temperature = (payload or {}).get("temperature", 0.7)
    if not content:
        return JSONResponse({"error": "content required"}, status_code=400)

    # One agent run per conversation at a time. If a run is already streaming here, reject the
    # concurrent send — the client can follow the in-flight run via GET .../stream instead of
    # racing a second one against the same history.
    hub = _hub_for(cid)
    if hub.is_running():
        return JSONResponse(
            {"error": "a response is already in progress for this conversation", "code": "busy"},
            status_code=409)

    backend_cfg = _load_backend_config()
    max_rounds = int(backend_cfg.get("max_rounds", 200))

    convo.setdefault("messages", []).append({"role": "user", "content": content})
    if not convo.get("title"):
        convo["title"] = content[:48]
    if model:
        convo["model"] = model
    _save_convo(convo)

    # Claim the hub for this run and broadcast the question so watchers render it immediately. The
    # sender already showed it optimistically and ignores its own `user` echo; only other viewers
    # act on it. begin() runs synchronously here (no await since the busy-check) so two racing
    # sends can't both pass the guard. The generation prevents a late response body from reviving a
    # claim that Stop or the unstarted-response timeout already retired.
    generation = hub.begin()
    hub.publish(_sse({"type": "user", "content": content}))

    async def gen():
        if not hub.start(generation):
            yield _sse({"type": "stopped"})
            yield _sse({"type": "done"})
            return

        # Every frame goes to the hub (fan-out to watchers) AND is streamed back to the sender.
        def emit(obj: dict) -> str:
            frame = _sse(obj)
            hub.publish(frame)
            return frame

        async def run_tool_or_stop(name: str, args: dict) -> str:
            """Run one tool, but let the Stop button interrupt the await immediately."""
            tool_task = asyncio.create_task(_execute_tool(name, args, TOOL_CTX))
            stop_task = asyncio.create_task(hub.wait_stopped())
            try:
                done, pending = await asyncio.wait(
                    {tool_task, stop_task},
                    timeout=30.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    tool_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await tool_task
                    raise asyncio.TimeoutError
                if stop_task in done and hub.stopping:
                    tool_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await tool_task
                    raise _RunStopped
                return await tool_task
            finally:
                for task in (tool_task, stop_task):
                    if not task.done():
                        task.cancel()

        # The reply is checkpointed to disk WHILE it streams, not only when it completes. A long
        # answer used to exist solely in the response stream until the round ended, so a hard kill
        # (Ctrl+C in the console) threw it away — the user watched the agent type an answer and
        # then found nothing on reload. This keeps a `partial` assistant message on disk, updated
        # in place at most once a second, and drops it the moment the real message is appended.
        live_msg: dict | None = None
        last_checkpoint = 0.0

        def checkpoint(text: str, reasoning: str) -> None:
            nonlocal live_msg, last_checkpoint
            if live_msg is None:
                live_msg = {"role": "assistant", "content": "", "partial": True}
                convo["messages"].append(live_msg)
            live_msg["content"] = text
            if reasoning:
                live_msg["reasoning"] = reasoning
            now = time.monotonic()
            if now - last_checkpoint >= LIVE_SAVE_SECONDS:
                last_checkpoint = now
                _save_convo(convo)

        def drop_partial() -> None:
            """Retire the checkpoint — the caller is about to append the real message."""
            nonlocal live_msg
            if live_msg is not None and live_msg in convo["messages"]:
                convo["messages"].remove(live_msg)
            live_msg = None

        def finalize_partial(stopped: bool = False) -> None:
            """Keep the checkpoint as the answer (interrupted run): it's what the user saw."""
            nonlocal live_msg
            if live_msg is not None:
                live_msg.pop("partial", None)
                if stopped:
                    live_msg["stopped"] = True
            live_msg = None

        try:
            if not SYSCALL_URL:
                yield emit({"type": "error", "error": "kernel syscall URL not configured"})
                yield emit({"type": "done"})
                return
            if not model:
                yield emit({"type": "error", "error": "no model set — pick one in the Run tab."})
                yield emit({"type": "done"})
                return

            messages = [{"role": "system", "content": AGENT_SYSTEM},
                        *_sanitize_history(convo["messages"])]
            # Track total token usage for this user+assistant exchange
            round_usage: dict = {}
            reasoning_parts: list[str] = []
            last_tool_sig: str | None = None   # loop breaker: the previous round's tool-call batch
            repeats = 0
            for _ in range(max_rounds):
                text_parts: list[str] = []
                acc: dict[int, dict] = {}
                reason_mark = len(reasoning_parts)  # rewind point if we retry this round
                attempt = 0
                while True:  # one model call, re-issued if it stalls before producing anything
                    try:
                        async for kind, val in _model_chunks(
                            {"model": model, "messages": messages, "tools": ALL_SCHEMAS,
                             "temperature": temperature}
                        ):
                            if hub.stopping:
                                raise _RunStopped
                            if kind == "status":
                                # Proof of life while the model is quiet: the UI shows "waiting on
                                # the model — 15s" instead of an indistinguishable-from-hung spinner.
                                yield emit({"type": "status", "phase": "waiting_model",
                                            "seconds": int(val)})
                                continue
                            chunk = val
                            if chunk.get("error"):
                                yield emit({"type": "error", "error": chunk["error"]})
                                yield emit({"type": "done"})
                                return
                            if chunk.get("reasoning"):
                                reasoning_parts.append(chunk["reasoning"])
                                yield emit({"type": "reasoning", "text": chunk["reasoning"]})
                                checkpoint("".join(text_parts), "".join(reasoning_parts))
                            if chunk.get("text"):
                                text_parts.append(chunk["text"])
                                yield emit({"type": "token", "text": chunk["text"]})
                                checkpoint("".join(text_parts), "".join(reasoning_parts))
                            # Capture token usage from the final chunk that carries it
                            usage_chunk = chunk.get("usage") or chunk.get("tokens") or {}
                            if usage_chunk:
                                # Merge: keep latest values to handle progressive reporting
                                for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                          "input_tokens", "output_tokens", "prompt_tokens_details",
                                          "prompt_cache_hit_tokens", "cache_read_input_tokens"):
                                    if k in usage_chunk:
                                        round_usage[k] = usage_chunk[k]
                                _c = _cached_of(usage_chunk)
                                if _c:
                                    round_usage["cached_tokens"] = _c
                            for tc in chunk.get("tool_calls") or []:
                                slot = acc.setdefault(
                                    tc.get("index", 0), {"id": None, "name": "", "arguments": ""}
                                )
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                if tc.get("name"):
                                    slot["name"] = tc["name"]
                                if tc.get("arguments"):
                                    slot["arguments"] += tc["arguments"]
                    except _ModelStalled as stall:
                        produced = bool(text_parts or acc)
                        # Nothing came back at all ⇒ the call itself is dead (a provider that
                        # accepted the request and went quiet). Re-issuing it is safe — nothing to
                        # duplicate — and usually works, so retry rather than leaving the user
                        # staring at a spinner that means nothing.
                        if not produced and attempt < STREAM_MAX_RETRIES:
                            attempt += 1
                            del reasoning_parts[reason_mark:]   # don't replay the dead call's output
                            yield emit({"type": "retry", "attempt": attempt,
                                        "max": STREAM_MAX_RETRIES, "reason": str(stall)})
                            await asyncio.sleep(min(RETRY_BACKOFF_SECONDS * attempt, 5))
                            continue
                        # Either it streamed something before dying (retrying would duplicate it) or
                        # the retries are spent. Keep whatever arrived and say plainly what happened.
                        finalize_partial(stopped=True)
                        _save_convo(convo)
                        yield emit({"type": "error", "error": (
                            f"{stall} — the reply was cut short; what arrived has been saved."
                            if produced else
                            f"{stall}. Retried {attempt}× and it never answered — check the model "
                            f"and provider key in Settings, then try again.")})
                        yield emit({"type": "done"})
                        return
                    break  # the call completed

                text = "".join(text_parts)
                tool_calls = [
                    {
                        "id": v["id"] or f"call_{i}",
                        "type": "function",
                        "function": {"name": v["name"], "arguments": v["arguments"] or "{}"},
                    }
                    for i, (_, v) in enumerate(sorted(acc.items()))
                    if v["name"]
                ]

                if not text and not tool_calls:
                    if reasoning_parts:
                        finalize_partial(stopped=True)
                    else:
                        drop_partial()
                    _save_convo(convo)
                    message = (
                        "the model returned no answer — check the model/provider "
                        "configuration and try again."
                    )
                    errorlog.record(
                        None,
                        source="run-agent",
                        exc_type="EmptyModelResponse",
                        message=message,
                        route="/api/agent/conversations/{cid}/message",
                        context={"conversation": cid, "model": model},
                    )
                    yield emit({"type": "error", "error": message})
                    yield emit({"type": "done"})
                    return

                if tool_calls:
                    # Loop breaker. Polling/status tools are allowed to repeat while external work
                    # advances; only non-repeatable calls count toward the identical-call limit.
                    # In a mixed batch the real work still participates, so adding bg_read_log cannot
                    # disguise a repeated dev_write_file/dev_list call.
                    checked_calls = [
                        c for c in tool_calls
                        if not _tool_allows_repeated_calls(c["function"]["name"])
                    ]
                    if checked_calls:
                        sig = json.dumps([
                            [c["function"]["name"], c["function"]["arguments"]]
                            for c in checked_calls
                        ], sort_keys=True)
                        repeats = repeats + 1 if sig == last_tool_sig else 1
                        last_tool_sig = sig
                        if repeats >= LOOP_REPEAT_LIMIT:
                            drop_partial()
                            names = ", ".join(sorted({
                                c["function"]["name"] for c in checked_calls
                            }))
                            note = (f"(stopped: the agent called {names} with the same arguments "
                                    f"{repeats}× in a row without making progress — it was stuck in "
                                    f"a loop. Try rephrasing, or ask it to take a different approach.)")
                            convo["messages"].append({"role": "assistant", "content": note})
                            _save_convo(convo)
                            yield emit({"type": "loop", "tool": names, "repeats": repeats})
                            yield emit({"type": "assistant", "content": note})
                            yield emit({"type": "done"})
                            return
                    # A polling-only round leaves real-call history untouched, so alternating a
                    # status check cannot reset a genuine loop.

                    drop_partial()  # the real assistant message supersedes the checkpoint
                    # The assistant message that ANNOUNCES the tool calls must come
                    # before its tool results — the provider rejects a `tool` message
                    # whose preceding message has no matching `tool_calls`. Append it
                    # first, then fill in its `tools` summary as each call runs.
                    asst = {"role": "assistant", "content": text, "tool_calls": tool_calls}
                    if round_usage:
                        asst["usage"] = round_usage.copy()
                    if reasoning_parts:
                        asst["reasoning"] = "".join(reasoning_parts)
                    messages.append(asst)
                    convo["messages"].append(asst)
                    _save_convo(convo)

                    tool_summary: list[dict] = []
                    for call in tool_calls:
                        if hub.stopping:  # honour Stop between calls, not just between rounds
                            raise _RunStopped
                        name = call["function"]["name"]
                        try:
                            args = json.loads(call["function"]["arguments"] or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        public_args = _public_tool_args(args)
                        yield emit({"type": "tool_call", "name": name, "args": public_args})
                        failure_recorded = False
                        try:
                            result = await run_tool_or_stop(name, args)
                        except _RunStopped:
                            raise
                        except asyncio.TimeoutError:
                            result = f"error: tool '{name}' timed out after 30s"
                        except Exception as exc:  # record + keep the run alive
                            errorlog.capture(exc, source="run-agent",
                                             route=f"tool:{name}",
                                             context={"tool": name, "conversation": cid})
                            failure_recorded = True
                            result = f"error: tool '{name}' failed: {type(exc).__name__}: {exc}"
                        status = "failed" if str(result).startswith("error:") else "done"
                        if status == "failed" and not failure_recorded:
                            errorlog.record(
                                None,
                                source="run-agent",
                                exc_type="ToolError",
                                message=str(result),
                                route=f"tool:{name}",
                                context={"tool": name, "conversation": cid},
                            )
                        # Display-only: send/persist the tool's NAME + STATUS, never its result.
                        # A tool result can be file/source content (dev_read_file, etc.); the UI
                        # must not preview it. The model still gets the full result via `tool_msg`.
                        yield emit({"type": "tool_result", "name": name, "status": status})
                        tool_summary.append({"name": name, "args": public_args, "status": status})
                        tool_msg = {"role": "tool", "tool_call_id": call["id"], "content": result}
                        messages.append(tool_msg)
                        convo["messages"].append(tool_msg)

                    if tool_summary:
                        asst["tools"] = tool_summary  # mutates the dict already in both lists
                    _save_convo(convo)
                    round_usage = {}
                    reasoning_parts = []
                    continue

                drop_partial()  # the finished message supersedes the checkpoint
                reasoning_text = "".join(reasoning_parts)
                asst_msg: dict[str, object] = {"role": "assistant", "content": text}
                if reasoning_text:
                    asst_msg["reasoning"] = reasoning_text
                if round_usage:
                    asst_msg["usage"] = round_usage.copy()
                convo["messages"].append(asst_msg)
                _save_convo(convo)
                ev: dict[str, object] = {"type": "assistant", "content": text}
                if reasoning_text:
                    ev["reasoning"] = reasoning_text
                if round_usage:
                    ev["usage"] = round_usage.copy()
                yield emit(ev)
                yield emit({"type": "done"})
                return

            drop_partial()
            limit_note = f"(stopped: reached the tool-round limit of {max_rounds})"
            convo["messages"].append({"role": "assistant", "content": limit_note})
            _save_convo(convo)
            yield emit({"type": "assistant", "content": limit_note})
            yield emit({"type": "done"})
        except _RunStopped:
            # Somebody pressed Stop (possibly in another tab, or after a reload). Keep the reply
            # as far as it got — it's what the user watched being written — and tell every viewer.
            finalize_partial(stopped=True)
            _save_convo(convo)
            yield emit({"type": "stopped"})
            yield emit({"type": "done"})
            return
        except asyncio.CancelledError:
            # The sender disconnected — keep whatever streamed so far and exit cleanly. This run
            # drives the fan-out, so also tell any watchers it ended (publish, not yield: the
            # generator is being torn down) rather than leaving them on a stream that stopped.
            finalize_partial(stopped=True)
            _save_convo(convo)
            hub.publish(_sse({"type": "done"}))
            return
        except Exception as exc:
            errorlog.capture(exc, source="run-agent",
                             route="/api/agent/conversations/{cid}/message")
            yield emit({"type": "error", "error": str(exc)})
            yield emit({"type": "done"})
        finally:
            # Release the hub whether the run finished, errored, or was cancelled, so the next
            # send isn't wrongly rejected as busy and an idle hub can be reclaimed.
            hub.end(generation)
            _gc_hub(cid)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/agent/conversations/{cid}/stream")
async def watch_conversation(cid: str):
    """Follow a conversation's live agent run without starting one.

    Replays the in-flight run's frames on connect, then follows live. A periodic heartbeat keeps
    the connection from idling out and surfaces a dead client promptly. Read-only — it never
    triggers a run.
    """
    if _load_convo(cid) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    hub = _hub_for(cid)

    async def gen():
        q = hub.subscribe()
        try:
            yield ": subscribed\n\n"  # flush response headers to the client immediately
            while True:
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    frame = ": keepalive\n\n"  # a write failure here cancels a gone-away watcher
                yield frame
        finally:
            hub.unsubscribe(q)
            _gc_hub(cid)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── serve the built UI (index at /, hashed files via the /assets mount) ─────────────
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    idx = DIST / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Quine</h1><p>UI not built yet — run "
        "<code>npm install &amp;&amp; npm run build</code> in <code>app/frontend</code>.</p>"
    )
