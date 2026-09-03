"""Gateway: the reverse proxy + syscall boundary (ring-0 ↔ ring-3 line).

A fixed-port server that:
  • handles `/api/syscall/*` itself (privileged kernel operations), and
  • reverse-proxies everything else to whichever app slot is currently active.

Because the browser only ever talks to this stable port, a reboot (blue-green slot
switch) is invisible — the URL never changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from kernel import (agent_runtime, checks, evals, events, kernelmod, llm, metering,
                    opauth, registry, state_store, triggers, versioning)
from kernel.core import Kernel

# Hop-by-hop headers we must not forward verbatim (content-encoding is preserved
# because we stream the raw, still-encoded body).
_HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding", "content-length",
    "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade",
}


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(timeout=None)
    app.state.kernel = Kernel()
    await app.state.kernel.boot()
    # Let a kernel-update approval stop uvicorn cleanly (see kernel/__main__ + core.request_restart).
    app.state.kernel._server = getattr(app.state, "server", None)
    yield
    with contextlib.suppress(Exception):
        await app.state.kernel.shutdown()
    with contextlib.suppress(Exception):
        await app.state.client.aclose()


app = FastAPI(title="Quine Kernel Gateway", lifespan=lifespan)


# ── Optional edge auth ─────────────────────────────────────────────────────────────
# A deployment can set KERNEL_AUTH_TOKEN to require authentication at the gateway edge. When the
# variable is unset, the gate stays open so local development and the test suite are unaffected.
KERNEL_AUTH_TOKEN_ENV = "KERNEL_AUTH_TOKEN"
_AUTH_OPEN_PATHS = frozenset({"/health"})  # watchdog / load-balancer probe stays public
# Inbound webhooks come from external senders who can't hold the kernel token, so they are
# exempt from edge auth — and instead verified by a per-trigger HMAC signature (kernel.triggers).
_AUTH_OPEN_PREFIXES = ("/api/syscall/webhook/",)


@app.middleware("http")
async def _edge_auth(request: Request, call_next):
    """Require KERNEL_AUTH_TOKEN (if configured) on every request except /health and the
    HMAC-verified webhook endpoints.

    EventSource cannot send an Authorization header, so SSE endpoints also accept the
    token as a `?token=` query param. Denials are audited."""
    token = os.environ.get(KERNEL_AUTH_TOKEN_ENV)
    _open = (request.url.path in _AUTH_OPEN_PATHS
             or request.url.path.startswith(_AUTH_OPEN_PREFIXES))
    if token and not _open:
        auth = request.headers.get("authorization", "")
        presented = auth[7:].strip() if auth[:7].lower() == "bearer " else request.query_params.get("token")
        # Constant-time compare so the token can't be recovered via a timing side channel.
        # Bytes, not str: compare_digest raises TypeError on non-ASCII input (headers are
        # latin-1-decoded), and a garbage credential must yield 401, never a 500.
        if presented is None or not hmac.compare_digest(presented.encode("utf-8"),
                                                        token.encode("utf-8")):
            with contextlib.suppress(Exception):
                state_store.audit("auth_denied", path=request.url.path, method=request.method)
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ── Operator authorization (opt-in; see kernel/opauth.py) ─────────────────────────
# POST syscalls that carry promotion authority, rewrite history, or reconfigure autonomy.
# The app process can reach every other syscall for its own features; THESE require the
# kernel-held operator credential once operator_auth.enabled is on.
_OPERATOR_POST_PATHS = frozenset({
    "/api/syscall/approve", "/api/syscall/reject",
    "/api/syscall/rollback", "/api/syscall/rollback_to",
    "/api/syscall/revert", "/api/syscall/reapply",
    "/api/syscall/config",
    "/api/syscall/triggers", "/api/syscall/triggers/delete", "/api/syscall/triggers/toggle",
    "/api/syscall/checks/toggle",
    "/api/syscall/evals", "/api/syscall/evals/delete", "/api/syscall/evals/toggle",
    "/api/syscall/evals/run",
    "/api/syscall/line/promote", "/api/syscall/preview/promote",
    "/api/syscall/kernel/change_request", "/api/syscall/kernel/approve",
    "/api/syscall/kernel/reject", "/api/syscall/kernel/rollback",
})


@app.middleware("http")
async def _operator_gate(request: Request, call_next):
    """Require operator authority (X-Operator-Key header, or the HttpOnly session from
    /operator) on the syscalls above. No-op while operator_auth.enabled is off."""
    if request.method == "POST" and request.url.path in _OPERATOR_POST_PATHS:
        kernel = getattr(app.state, "kernel", None)
        config = getattr(kernel, "config", None) or {}
        if not opauth.verify_request(request, config):
            with contextlib.suppress(Exception):
                state_store.audit("operator_denied", path=request.url.path)
            return JSONResponse(
                {"ok": False, "reason": "operator authorization required — unlock at "
                                        "/operator or present X-Operator-Key"},
                status_code=403)
    return await call_next(request)


_OPERATOR_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Quine — Operator</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font:15px/1.5 system-ui,sans-serif;background:#101019;color:#e8e8f0;
      display:flex;justify-content:center;padding:12vh 16px}
 main{max-width:430px}h1{font-size:1.2rem}p{color:#9a9ab0}
 input{width:100%;padding:9px;border-radius:6px;border:1px solid #33334a;
       background:#181826;color:inherit;font:inherit}
 button{margin-top:10px;padding:9px 16px;border-radius:6px;border:0;cursor:pointer;
        background:#5b5bd6;color:#fff;font:inherit}
 #msg{margin-top:12px}.ok{color:#7dd97d}.err{color:#e07d7d}code{color:#b8b8d0}
</style></head><body><main>
<h1>Operator unlock</h1>
<p>This page is served by the <em>kernel</em> (ring 0), never by the app. Paste the
operator key from <code>state/operator_key</code> to unlock approvals and autonomy
settings in this browser for a limited session.</p>
<input id="key" type="password" placeholder="operator key" autofocus>
<button id="go">Unlock</button> <button id="lock" style="background:#33334a">Lock</button>
<div id="msg"></div>
<script>
 const msg=(t,c)=>{const m=document.getElementById('msg');m.textContent=t;m.className=c};
 const refresh=async()=>{try{const r=await fetch('api/syscall/operator/status');
   const d=await r.json();
   msg(d.enabled?(d.unlocked?'Status: unlocked':'Status: locked')
                :'operator_auth is DISABLED — this gate is not enforcing anything yet.',
       d.unlocked?'ok':'')}catch(e){}};
 document.getElementById('go').onclick=async()=>{
   const r=await fetch('api/syscall/operator/unlock',{method:'POST',
     headers:{'content-type':'application/json'},
     body:JSON.stringify({key:document.getElementById('key').value})});
   const d=await r.json().catch(()=>({}));
   if(r.ok&&d.ok){msg('Unlocked for '+d.session_hours+'h — you can close this tab.','ok')}
   else{msg('Denied: '+(d.reason||r.status),'err')}};
 document.getElementById('lock').onclick=async()=>{
   await fetch('api/syscall/operator/lock',{method:'POST'});msg('Locked.','')};
 refresh();
</script></main></body></html>"""


@app.get("/operator")
async def operator_page() -> Response:
    """The ring-0 unlock console. Kernel-served on purpose: a self-mod can rewrite every
    app page, but never this one."""
    return Response(_OPERATOR_PAGE, media_type="text/html")


@app.get("/api/syscall/operator/status")
async def operator_status(request: Request) -> JSONResponse:
    config = getattr(getattr(app.state, "kernel", None), "config", None) or {}
    return JSONResponse({"enabled": opauth.enabled(config),
                         "unlocked": opauth.verify_request(request, config)})


@app.post("/api/syscall/operator/unlock")
async def operator_unlock(request: Request, payload: dict) -> JSONResponse:
    config = getattr(getattr(app.state, "kernel", None), "config", None) or {}
    token = opauth.unlock(str((payload or {}).get("key") or ""), config)
    if token is None:
        return JSONResponse({"ok": False, "reason": "invalid operator key"}, status_code=401)
    hours = opauth.session_hours(config)
    resp = JSONResponse({"ok": True, "session_hours": hours})
    # HttpOnly: app-served JS can never READ the credential (it may ride same-origin
    # requests while the session lives — the accepted residual, see kernel/opauth.py).
    resp.set_cookie(opauth.COOKIE, token, max_age=int(hours * 3600),
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/api/syscall/operator/lock")
async def operator_lock(request: Request) -> JSONResponse:
    opauth.lock(request.cookies.get(opauth.COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(opauth.COOKIE, path="/")
    return resp


# ── Syscalls (declared BEFORE the catch-all so they take precedence) ──────────────
@app.get("/api/syscall/status")
async def syscall_status() -> JSONResponse:
    return JSONResponse(app.state.kernel.status())


@app.get("/api/syscall/spend")
async def syscall_spend() -> JSONResponse:
    """Return the instance's current-month model spend and configured budget.

    The mutable app may read usage but never provider keys.
    """
    return JSONResponse(metering.snapshot())


@app.get("/api/syscall/versions")
async def syscall_versions(limit: int = 50, offset: int = 0) -> JSONResponse:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return JSONResponse({
        "versions": versioning.list_versions(limit, offset),
        "total": versioning.count_versions(),
        "head": versioning.head() if versioning.has_history() else None,
    })


@app.get("/api/syscall/audit")
async def syscall_audit(limit: int = 100, offset: int = 0,
                        event: str | None = None, since: str | None = None) -> JSONResponse:
    """The audit tail with optional server-side filters (?event= exact name, ?since=
    ISO-8601 lower bound) and pagination (?offset= skips the N newest matches)."""
    return JSONResponse({"audit": state_store.read_audit(
        limit, offset=offset, event=(event or "").strip() or None,
        since=(since or "").strip() or None)})


@app.get("/api/syscall/audit/verify")
async def syscall_audit_verify() -> JSONResponse:
    """Re-compute the audit log's hash chain: proves the trail wasn't edited in place."""
    return JSONResponse(state_store.verify_audit())


@app.get("/api/syscall/audit/export")
async def syscall_audit_export(format: str = "json", limit: int = 100_000) -> Response:
    """Download the append-only audit trail as JSON or CSV (compliance / SIEM ingest)."""
    rows = state_store.read_audit(limit)
    if format == "csv":
        import csv
        import io

        cols = ["ts", "event"]
        for r in rows:  # stable header: ts, event, then any other keys in first-seen order
            for k in r:
                if k not in cols:
                    cols.append(k)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for r in rows:
            writer.writerow([
                json.dumps(v) if isinstance(v, (dict, list)) else ("" if v is None else v)
                for v in (r.get(k) for k in cols)
            ])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=audit.csv"})
    return Response(json.dumps(rows, indent=2), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=audit.json"})


_DIFF_CEILING = 2_000_000  # bound memory on a pathological (e.g. multi-MB binary) diff


def _cap_diff(text: str) -> str:
    if len(text) > _DIFF_CEILING:
        return text[:_DIFF_CEILING] + "\n…[diff exceeds 2 MB — truncated; use `git show` on the version branch]"
    return text


@app.get("/api/syscall/diff")
async def syscall_diff(version: str) -> JSONResponse:
    """Show exactly what a version changed (vs its parent), for review/trust.
    Accepts a full/short sha, v<seq> number, or label."""
    match = versioning.resolve_version(version)
    if not match:
        return JSONResponse({"ok": False, "error": f"unknown version {version}"}, status_code=404)
    return JSONResponse({"ok": True, "version": match, "diff": _cap_diff(versioning.diff(match))})


@app.get("/api/syscall/compare")
async def syscall_compare(a: str, b: str) -> JSONResponse:
    """Unified diff between ANY two versions (what changed going a → b) — review a
    rollback's net effect, or the active version vs any historical one."""
    full_a = versioning.resolve_version(a)
    full_b = versioning.resolve_version(b)
    if not full_a or not full_b:
        missing = a if not full_a else b
        return JSONResponse({"ok": False, "error": f"unknown version {missing}"}, status_code=404)
    return JSONResponse({
        "ok": True, "a": full_a, "b": full_b,
        "diff": _cap_diff(versioning.diff_range(full_a, full_b)),
    })


@app.post("/api/syscall/label")
async def syscall_label(payload: dict) -> JSONResponse:
    """Set (or clear, with an empty label) a human-friendly, unique name on a version —
    labels resolve anywhere a version reference is accepted (diff/compare/rollback/…)."""
    ref = ((payload or {}).get("version") or "").strip()
    label = ((payload or {}).get("label") or "").strip()
    if not ref:
        return JSONResponse({"ok": False, "reason": "version required"}, status_code=400)
    result = app.state.kernel.label_version(ref, label)
    if not result["ok"]:
        code = 404 if str(result.get("reason", "")).startswith("unknown version") else 400
        return JSONResponse(result, status_code=code)
    return JSONResponse(result)


@app.get("/api/syscall/config")
async def syscall_config_get() -> JSONResponse:
    """Current runtime config (no secrets) for the Settings UI / agent introspection."""
    return JSONResponse({"ok": True, "config": state_store.public_config()})


@app.post("/api/syscall/config")
async def syscall_config_set(payload: dict) -> JSONResponse:
    """Change allow-listed, bounded runtime params (temperature, model, max_steps,
    enabled tools, watchdog timeout). This is how the agent/user tune the agent
    WITHOUT kernel write access — anything outside the allowlist is rejected."""
    payload = payload or {}
    _patch = payload.get("patch")
    patch = _patch if isinstance(_patch, dict) else payload
    ok, errors, cfg = state_store.update_config(patch)
    if not ok:
        return JSONResponse({"ok": False, "errors": errors, "config": cfg}, status_code=400)
    app.state.kernel.config = cfg  # apply live: next self-mod / watchdog use new values
    if any(k.startswith("watchdog.monitor") for k in patch):
        app.state.kernel.apply_monitor_config()  # live monitor follows its config
    state_store.audit("config_updated", keys=sorted(patch.keys()))
    return JSONResponse({"ok": True, "config": cfg})


@app.get("/api/syscall/models")
async def syscall_models() -> JSONResponse:
    """litellm provider/model catalog for the Settings agent picker (no secrets, cached). Lets
    the UI offer a model dropdown per provider instead of hardcoding a short list."""
    return JSONResponse({"providers": llm.provider_catalog()})


@app.post("/api/syscall/validate")
async def syscall_validate() -> JSONResponse:
    """Validate the CURRENT self-mod task's staging. Used by the (keyless) runtime
    worker so its fix-retry loop stays where the model is; the kernel re-validates as
    the authoritative backstop before committing."""
    staging = getattr(app.state.kernel, "current_staging", None)
    if staging is None:
        return JSONResponse({"ok": False, "report": "no active self-mod task"}, status_code=409)
    ok, report = await asyncio.to_thread(agent_runtime.validate_staging, staging)
    return JSONResponse({"ok": ok, "report": report})


@app.post("/api/syscall/rollback")
async def syscall_rollback() -> JSONResponse:
    return JSONResponse(await app.state.kernel.rollback())


@app.post("/api/syscall/revert")
async def syscall_revert(payload: dict) -> JSONResponse:
    """Undo ONE version's changes while keeping everything after it (git revert →
    validate → commit → health-gated reboot). Accepts sha, v<seq>, or label."""
    ref = ((payload or {}).get("version") or "").strip()
    if not ref:
        return JSONResponse({"ok": False, "reason": "version required"}, status_code=400)
    return JSONResponse(await app.state.kernel.revert_version(ref))


@app.post("/api/syscall/reapply")
async def syscall_reapply(payload: dict) -> JSONResponse:
    """Re-apply an abandoned version's changes onto the current line (cherry-pick →
    validate → commit → health-gated reboot). Accepts sha, v<seq>, or label."""
    ref = ((payload or {}).get("version") or "").strip()
    if not ref:
        return JSONResponse({"ok": False, "reason": "version required"}, status_code=400)
    return JSONResponse(await app.state.kernel.reapply_version(ref))


@app.post("/api/syscall/rollback_to")
async def syscall_rollback_to(payload: dict) -> JSONResponse:
    sha = (payload or {}).get("version")
    if not sha:
        return JSONResponse({"ok": False, "reason": "version required"}, status_code=400)
    return JSONResponse(await app.state.kernel.rollback_to(sha))


@app.post("/api/syscall/change_request")
async def syscall_change_request(request: Request, payload: dict) -> JSONResponse:
    payload = payload or {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"ok": False, "reason": "prompt required"}, status_code=400)
    # Optional "continue from a commit": re-base the edit on `base_version` and resume the
    # conversation saved for `resume_task` (the task that produced that version).
    # Optional `line`: target a named line instead of production (see kernel.change_request).
    base_version = (payload.get("base_version") or "").strip() or None
    resume_task = (payload.get("resume_task") or "").strip() or None
    line = (payload.get("line") or "").strip() or None
    # Whether the SUBMITTER holds operator authority: with operator_auth on, an
    # unattended enqueue (no key/session — e.g. app-process code) always HOLDS for
    # approval instead of auto-promoting (see core._should_hold).
    operator = opauth.verify_request(request, app.state.kernel.config)
    return JSONResponse(await app.state.kernel.change_request(
        prompt, base_version=base_version, resume_task=resume_task, line=line,
        operator=operator))


@app.get("/api/syscall/version_conversation")
async def syscall_version_conversation(version: str | None = None) -> JSONResponse:
    """Resolve a version ref (sha | v<seq> | label) to the task that produced it and whether a
    resumable conversation snapshot exists for it — so the UI can enable/label the "Continue"
    action. The transcript itself is served (source-concealed) by the app backend."""
    if not version:
        return JSONResponse({"ok": False, "reason": "version required"}, status_code=400)
    sha = versioning.resolve_version(version)
    if not sha:
        return JSONResponse({"ok": False, "reason": f"unknown version {version}"}, status_code=404)
    meta = registry.get(sha) or {}
    task = meta.get("task")
    has_convo = bool(task) and (state_store.DATA_DIR / "selfmod_convos" / f"{task}.jsonl").exists()
    return JSONResponse({"ok": True, "version": sha, "short": sha[:8],
                         "seq": meta.get("seq"), "task": task, "has_conversation": has_convo})


@app.get("/api/syscall/pending")
async def syscall_pending() -> JSONResponse:
    """Versions committed but awaiting approval (governance gate). Review each with the
    /diff syscall, then approve/reject. Empty unless agent.require_approval is on."""
    return JSONResponse({"pending": app.state.kernel.list_pending()})


@app.post("/api/syscall/approve")
async def syscall_approve(payload: dict) -> JSONResponse:
    sha = (payload or {}).get("sha", "").strip()
    if not sha:
        return JSONResponse({"ok": False, "reason": "sha required"}, status_code=400)
    return JSONResponse(await app.state.kernel.approve_version(sha))


@app.post("/api/syscall/reject")
async def syscall_reject(payload: dict) -> JSONResponse:
    sha = (payload or {}).get("sha", "").strip()
    if not sha:
        return JSONResponse({"ok": False, "reason": "sha required"}, status_code=400)
    return JSONResponse(app.state.kernel.reject_version(sha))


# ── preview environments + named lines ────────────────────────────────────────────
@app.post("/api/syscall/preview")
async def syscall_preview(payload: dict) -> JSONResponse:
    """Boot a preview env: {version: <ref>} previews any version (incl. a pending
    candidate), {line: <name>} (re)spawns a line's preview at its tip. Optional {name}."""
    payload = payload or {}
    kernel = app.state.kernel
    line = (payload.get("line") or "").strip()
    name = (payload.get("name") or "").strip() or None
    if line:
        tip = versioning.line_tip(line)
        if tip is None:
            return JSONResponse({"ok": False, "reason": f"unknown line '{line}'"}, status_code=404)
        result = await kernel.previews.create(name or line, tip, line=line, replace=True)
    else:
        ref = (payload.get("version") or "").strip()
        if not ref:
            return JSONResponse({"ok": False, "reason": "version or line required"}, status_code=400)
        result = await kernel.preview_version(ref, name)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/syscall/previews")
async def syscall_previews() -> JSONResponse:
    return JSONResponse({"previews": app.state.kernel.previews.list()})


@app.post("/api/syscall/preview/stop")
async def syscall_preview_stop(payload: dict) -> JSONResponse:
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "reason": "name required"}, status_code=400)
    result = await app.state.kernel.previews.stop(name)
    return JSONResponse(result, status_code=200 if result.get("ok") else 404)


@app.post("/api/syscall/preview/promote")
async def syscall_preview_promote(payload: dict) -> JSONResponse:
    """Ship what the preview is showing: a line preview promotes its line; a pending
    candidate goes through approval; any other version reboots prod to it (health +
    regression gated, like every promotion)."""
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "reason": "name required"}, status_code=400)
    return JSONResponse(await app.state.kernel.preview_promote(name))


@app.post("/api/syscall/line")
async def syscall_line_create(payload: dict) -> JSONResponse:
    """Create a named line (experiment/staging branch) from any version (default: the
    current head) and spin up its preview."""
    payload = payload or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "reason": "name required"}, status_code=400)
    result = await app.state.kernel.create_line(
        name, (payload.get("from") or "").strip() or None,
        description=(payload.get("description") or "").strip())
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/syscall/lines")
async def syscall_lines() -> JSONResponse:
    return JSONResponse({"lines": app.state.kernel.list_lines()})


@app.post("/api/syscall/line/promote")
async def syscall_line_promote(payload: dict) -> JSONResponse:
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "reason": "name required"}, status_code=400)
    return JSONResponse(await app.state.kernel.promote_line(name))


@app.post("/api/syscall/line/delete")
async def syscall_line_delete(payload: dict) -> JSONResponse:
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "reason": "name required"}, status_code=400)
    result = await app.state.kernel.delete_line(name)
    return JSONResponse(result, status_code=200 if result.get("ok") else 404)


@app.get("/api/syscall/checks")
async def syscall_checks() -> JSONResponse:
    """The Verification Gate's frozen regression suite (state/checks.json): every check a
    future candidate must pass, with provenance (origin version/task/prompt), enable state,
    and its last run's result. Enriched with the origin's seq/label for display."""
    meta = registry.all_versions()
    rows = []
    for c in checks.list_checks():
        origin = meta.get(c.get("origin") or "", {})
        rows.append({**c, "origin_seq": origin.get("seq"), "origin_label": origin.get("label"),
                     "origin_status": origin.get("status")})
    return JSONResponse({"checks": rows,
                         "enabled": bool(app.state.kernel.config
                                         .get("verifier", {}).get("enabled"))})


@app.post("/api/syscall/checks/toggle")
async def syscall_checks_toggle(payload: dict) -> JSONResponse:
    """Operator enable/disable of one frozen check. A manual disable is sticky (survives
    lifecycle transitions); a manual enable overrides a lifecycle-disable. Audited."""
    check_id = ((payload or {}).get("id") or "").strip()
    enabled = (payload or {}).get("enabled")
    if not check_id or not isinstance(enabled, bool):
        return JSONResponse({"ok": False, "reason": "id and enabled (bool) required"},
                            status_code=400)
    ok, detail = checks.set_check_enabled(check_id, enabled)
    if not ok:
        return JSONResponse({"ok": False, "reason": detail}, status_code=404)
    state_store.audit("check_toggled", check=check_id, enabled=enabled)
    return JSONResponse({"ok": True, "id": check_id, "status": detail})


# ── agent evals (the held-out benchmark gating changes to the agent's runtime) ─────
@app.get("/api/syscall/evals")
async def syscall_evals() -> JSONResponse:
    """The benchmark task store (state/evals.json) + the gate's config, for the UI."""
    cfg = app.state.kernel.config.get("evals", {}) or {}
    return JSONResponse({"tasks": evals.list_tasks(),
                         "enabled": bool(cfg.get("enabled")),
                         "strict": bool(cfg.get("strict")),
                         "paths": evals.scope_paths(app.state.kernel.config)})


@app.post("/api/syscall/evals")
async def syscall_evals_save(payload: dict) -> JSONResponse:
    """Create or update one benchmark task (operator-only surface)."""
    ok, err, entry = evals.upsert_task(payload or {})
    if not ok:
        return JSONResponse({"ok": False, "reason": err}, status_code=400)
    return JSONResponse({"ok": True, "task": entry})


@app.post("/api/syscall/evals/delete")
async def syscall_evals_delete(payload: dict) -> JSONResponse:
    tid = ((payload or {}).get("id") or "").strip()
    if not tid:
        return JSONResponse({"ok": False, "reason": "id required"}, status_code=400)
    if not evals.delete_task(tid):
        return JSONResponse({"ok": False, "reason": f"unknown eval task {tid}"}, status_code=404)
    return JSONResponse({"ok": True, "id": tid, "deleted": True})


@app.post("/api/syscall/evals/toggle")
async def syscall_evals_toggle(payload: dict) -> JSONResponse:
    tid = ((payload or {}).get("id") or "").strip()
    on = (payload or {}).get("enabled")
    if not tid or not isinstance(on, bool):
        return JSONResponse({"ok": False, "reason": "id and enabled (bool) required"},
                            status_code=400)
    if not evals.set_task_enabled(tid, on):
        return JSONResponse({"ok": False, "reason": f"unknown eval task {tid}"}, status_code=404)
    return JSONResponse({"ok": True, "id": tid, "enabled": on})


@app.post("/api/syscall/evals/run")
async def syscall_evals_run(payload: dict) -> JSONResponse:
    """Manual benchmark of any version (default: the active one) — e.g. to baseline the
    current runtime right after defining tasks. Runs the full worker-per-task suite, so
    it can take a while; scoped like a candidate run is NOT applied (no scope check)."""
    ref = ((payload or {}).get("version") or "").strip()
    kernel = app.state.kernel
    if ref:
        sha = versioning.resolve_version(ref)
        if not sha:
            return JSONResponse({"ok": False, "reason": f"unknown version {ref}"}, status_code=404)
    else:
        sha = (kernel.status().get("active") or {}).get("version")
        if not sha:
            return JSONResponse({"ok": False, "reason": "no active version"}, status_code=409)
    if not evals.enabled_tasks():
        return JSONResponse({"ok": False, "reason": "no enabled eval tasks"}, status_code=400)
    report = await evals.run_evals(sha, kernel.config)
    state_store.audit("evals_manual_run", version=sha[:12], ok=report["ok"],
                      passed=report["passed"], total=report["total"])
    return JSONResponse({"ok": True, "version": sha, "short": sha[:8], "report": report})


# ── autonomous triggers ────────────────────────────────────────────────────────────
# ── Gated Kernel Self-Update (operator-only; the CP data-plane denylists these) ────────
@app.get("/api/syscall/kernel/status")
async def syscall_kernel_status() -> JSONResponse:
    """Feature state, active-vs-shipped kernel digest, signed-mode, pending candidate."""
    return JSONResponse(app.state.kernel.kernel_status())


@app.get("/api/syscall/kernel/versions")
async def syscall_kernel_versions() -> JSONResponse:
    return JSONResponse({"versions": kernelmod.list_kernel_versions()})


@app.get("/api/syscall/kernel/pending")
async def syscall_kernel_pending() -> JSONResponse:
    return JSONResponse({"pending": state_store.read_pending_kernel()})


@app.post("/api/syscall/kernel/change_request")
async def syscall_kernel_change(payload: dict) -> JSONResponse:
    prompt = ((payload or {}).get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"ok": False, "reason": "prompt required"}, status_code=400)
    result = await app.state.kernel.kernel_change_request(prompt)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/syscall/kernel/approve")
async def syscall_kernel_approve(payload: dict) -> JSONResponse:
    sha = ((payload or {}).get("sha") or "").strip()
    sig = ((payload or {}).get("signature") or "").strip() or None
    if not sha:
        return JSONResponse({"ok": False, "reason": "sha required"}, status_code=400)
    result = app.state.kernel.approve_kernel_version(sha, sig)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/syscall/kernel/reject")
async def syscall_kernel_reject(payload: dict) -> JSONResponse:
    sha = ((payload or {}).get("sha") or "").strip()
    if not sha:
        return JSONResponse({"ok": False, "reason": "sha required"}, status_code=400)
    result = app.state.kernel.reject_kernel_version(sha)
    return JSONResponse(result, status_code=200 if result.get("ok") else 404)


@app.post("/api/syscall/kernel/rollback")
async def syscall_kernel_rollback() -> JSONResponse:
    result = app.state.kernel.rollback_kernel()
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/syscall/triggers")
async def syscall_triggers() -> JSONResponse:
    """The autonomous trigger registry (webhook secrets redacted) + the master-switch/cap
    config, for the Settings 'Automation' UI."""
    tcfg = app.state.kernel.config.get("triggers", {}) or {}
    return JSONResponse({"triggers": triggers.list_triggers(), "config": {
        "enabled": bool(tcfg.get("enabled")), "max_per_day": tcfg.get("max_per_day", 5),
        "auto_promote": bool(tcfg.get("auto_promote")),
        "verifier_enabled": bool(app.state.kernel.config.get("verifier", {}).get("enabled"))}})


@app.post("/api/syscall/triggers")
async def syscall_trigger_save(payload: dict) -> JSONResponse:
    """Create or update a trigger. A newly created webhook trigger returns its HMAC secret
    ONCE (redacted on every later read) — plus the signed-request recipe."""
    ok, err, entry = triggers.upsert_trigger(payload or {})
    if not ok:
        return JSONResponse({"ok": False, "reason": err}, status_code=400)
    assert entry is not None
    resp: dict = {"ok": True, "trigger": entry}
    if entry.get("kind") == "webhook":
        resp["webhook_url"] = f"/api/syscall/webhook/{entry['id']}"
        if entry.get("secret"):
            resp["secret_shown_once"] = entry["secret"]
            resp["signature_header"] = "X-Quine-Signature: sha256=HMAC_SHA256(secret, raw_body)"
    return JSONResponse(resp)


@app.post("/api/syscall/triggers/delete")
async def syscall_trigger_delete(payload: dict) -> JSONResponse:
    tid = ((payload or {}).get("id") or "").strip()
    if not tid:
        return JSONResponse({"ok": False, "reason": "id required"}, status_code=400)
    if not triggers.delete_trigger(tid):
        return JSONResponse({"ok": False, "reason": f"unknown trigger {tid}"}, status_code=404)
    return JSONResponse({"ok": True, "id": tid, "deleted": True})


@app.post("/api/syscall/triggers/toggle")
async def syscall_trigger_toggle(payload: dict) -> JSONResponse:
    tid = ((payload or {}).get("id") or "").strip()
    enabled = (payload or {}).get("enabled")
    if not tid or not isinstance(enabled, bool):
        return JSONResponse({"ok": False, "reason": "id and enabled (bool) required"},
                            status_code=400)
    if not triggers.set_trigger_enabled(tid, enabled):
        return JSONResponse({"ok": False, "reason": f"unknown trigger {tid}"}, status_code=404)
    return JSONResponse({"ok": True, "id": tid, "enabled": enabled})


@app.post("/api/syscall/webhook/{trigger_id}")
async def syscall_webhook(trigger_id: str, request: Request) -> JSONResponse:
    """Inbound webhook: fires a webhook trigger's self-mod. EXEMPT from edge auth (external
    senders can't hold the kernel token) — instead verified by the per-trigger HMAC over the
    raw body (X-Quine-Signature: sha256=…). Fails closed on a bad signature."""
    body = await request.body()
    sig = request.headers.get("x-quine-signature")
    ok, status, resp = app.state.kernel.triggers.handle_webhook(trigger_id, body, sig)
    return JSONResponse(resp, status_code=status)


@app.get("/api/syscall/task")
async def syscall_task(id: str | None = None) -> JSONResponse:
    """Recover a self-mod task's progress (status + full event log) so the UI can rebuild
    a run that outlived the tab/page that started it. Defaults to the latest task."""
    task_id = id or state_store.current_task_id()
    if not task_id:
        return JSONResponse({"ok": True, "task": None, "events": []})
    return JSONResponse({
        "ok": True,
        "task": state_store.read_task_status(task_id),
        "events": state_store.read_task_events(task_id),
    })


@app.post("/api/syscall/cancel")
async def syscall_cancel() -> JSONResponse:
    """Abort the in-flight self-mod (kills the worker; no commit)."""
    return JSONResponse(await app.state.kernel.cancel())


@app.post("/api/syscall/dequeue")
async def syscall_dequeue(payload: dict) -> JSONResponse:
    """Remove a still-queued task from the self-mod backlog before it starts."""
    return JSONResponse(app.state.kernel.dequeue((payload or {}).get("task_id", "")))


@app.post("/api/syscall/steer")
async def syscall_steer(payload: dict) -> JSONResponse:
    """User → agent: queue a mid-run steering message to redirect the running task."""
    return JSONResponse(app.state.kernel.enqueue_steer((payload or {}).get("message", "")))


@app.get("/api/syscall/steer")
async def syscall_steer_drain() -> JSONResponse:
    """Agent worker → kernel: drain queued steering messages (polled each loop step)."""
    return JSONResponse({"messages": app.state.kernel.drain_steer()})


@app.post("/api/syscall/llm_call")
async def syscall_llm_call(payload: dict) -> JSONResponse:
    """The ONE model primitive exposed to app code. Uses kernel-held keys; the app
    never sees them. Agent-built features (subagents, RAG, …) compose on top of this."""
    payload = payload or {}
    model = payload.get("model")
    if not model:
        return JSONResponse({"ok": False, "error": "model required"}, status_code=400)
    sampling = {k: payload[k] for k in ("temperature", "max_tokens", "top_p") if k in payload}
    try:
        if payload.get("kind") == "embed":
            data = await llm.embed(model, payload.get("input"))
        else:
            data = await llm.chat(
                model, payload.get("messages", []), tools=payload.get("tools"), **sampling
            )
        return JSONResponse({"ok": True, "response": data})
    except metering.BudgetExceeded as exc:  # P2.4 — monthly spend cap reached
        return JSONResponse(
            {"ok": False, "error": str(exc), "code": "budget_exceeded",
             "spend": metering.snapshot()},
            status_code=402,
        )
    except Exception as exc:  # missing key, bad model, provider error, …
        # Lead with the exception type + model so the agent/UI can tell apart the common
        # cases at a glance — e.g. NotFoundError (model name wrong) vs AuthenticationError
        # (key missing/invalid) for model='openai/gpt-5.4-mini'.
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}", "model": model},
            status_code=502,
        )


@app.post("/api/syscall/llm_stream")
async def syscall_llm_stream(payload: dict) -> StreamingResponse:
    """Streaming counterpart of llm_call: an SSE stream of model deltas. App-built
    features (e.g. the Run tab's agent loop) relay this to the browser for live output."""
    payload = payload or {}
    model = payload.get("model")
    messages = payload.get("messages", [])
    tools = payload.get("tools")
    sampling = {k: payload[k] for k in ("temperature", "max_tokens", "top_p") if k in payload}

    async def gen():
        if not model:
            yield "data: " + json.dumps({"error": "model required"}) + "\n\n"
            return
        try:
            async for chunk in llm.chat_stream(model, messages, tools=tools, **sampling):
                yield "data: " + json.dumps(chunk) + "\n\n"
            yield "data: " + json.dumps({"done": True}) + "\n\n"
        except Exception as exc:  # missing key, bad model, provider error, …
            yield "data: " + json.dumps({"error": f"{type(exc).__name__}: {exc}"}) + "\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/syscall/events")
async def syscall_events() -> StreamingResponse:
    """Server-Sent Events stream of live self-modification progress. Hosted by the
    gateway, so it survives app reboots (the browser stays connected throughout)."""

    async def gen():
        queue = events.bus.register()
        try:
            yield "retry: 3000\n\n"
            while True:
                event = await queue.get()
                yield "data: " + json.dumps(event) + "\n\n"
        finally:
            events.bus.unregister(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Preview routing (cookie-based) ────────────────────────────────────────────────
# A preview URL must serve the SPA whose assets live at absolute paths (/assets/…), so a
# path-prefix proxy would break it — instead /preview/<name> plants a cookie and the
# catch-all below routes THIS BROWSER's app-surface requests to the preview process.
# Syscalls are handled above the catch-all, so the control surface always addresses the
# real system regardless of the cookie. One port, works unchanged behind a trusted
# upstream data-plane proxy that forwards the Set-Cookie.
PREVIEW_COOKIE = "quine_preview"


# The redirect target is relative ("../" climbs from /preview/<x> to the mount root), so it lands
# correctly both when served directly and behind a trusted path-prefixed reverse proxy.
@app.get("/preview/exit")
async def preview_exit() -> Response:
    resp = RedirectResponse("../", status_code=302)
    resp.delete_cookie(PREVIEW_COOKIE, path="/")
    return resp


@app.get("/preview/{name}")
async def preview_enter(name: str) -> Response:
    kernel: Kernel = app.state.kernel
    if kernel.previews.get(name) is None:
        return JSONResponse({"ok": False, "reason": f"no running preview '{name}' — start "
                                                    "one from the Versions tab or the "
                                                    "/api/syscall/preview syscall"},
                            status_code=404)
    resp = RedirectResponse("../", status_code=302)
    resp.set_cookie(PREVIEW_COOKIE, name, path="/", samesite="lax", httponly=True)
    return resp


# ── Reverse proxy (catch-all) ─────────────────────────────────────────────────────
@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request) -> Response:
    kernel: Kernel = app.state.kernel
    cur = kernel.current

    # Preview routing: a valid preview cookie steers this browser's app-surface traffic
    # to the preview process; a stale one falls through to the active app.
    preview_name = request.cookies.get(PREVIEW_COOKIE)
    preview = kernel.previews.get(preview_name) if preview_name else None
    if preview is not None:
        kernel.previews.touch(preview.name)
        cur = preview.handle

    if cur is None or not cur.alive():
        return Response("app is rebooting…", status_code=503)

    client: httpx.AsyncClient = app.state.client
    url = f"http://127.0.0.1:{cur.port}/{path}"
    body = await request.body()
    # Strip edge-auth material before crossing into ring 3: the mutable app must never see the
    # kernel's KERNEL_AUTH_TOKEN (the Authorization header or the ?token= SSE parameter), or a
    # malicious self-modification could capture it. The app does no auth of its own.
    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "authorization")}
    fwd_params = [(k, v) for k, v in request.query_params.multi_items() if k.lower() != "token"]

    upstream = client.build_request(
        request.method, url,
        params=fwd_params,  # pyright: ignore[reportArgumentType]  # httpx accepts list[tuple]
        content=body, headers=fwd_headers,
    )
    resp = await client.send(upstream, stream=True)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    if preview is not None:
        out_headers["X-Quine-Preview"] = preview.name  # debuggability: which env answered
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=out_headers,
        background=BackgroundTask(resp.aclose),
    )
