"""The Advisor: Quine's self-reflection loop (a ring-3 plugin).

Mines the system's own telemetry — unresolved error groups, the version registry's
failure history (health/verify failures, rollbacks, rejections), the audit tail, model
spend, and the Verification Gate's checks — and turns it into concrete improvement
*proposals*: each one a ready-to-run change-request prompt with a rationale, cited
evidence, and acceptance criteria. This is the observe→propose half of the self-improving
loop; the act→validate→promote half is the kernel's existing self-mod pipeline.

Safety posture: the Advisor is read-only over code. Proposals are TEXT under the data
partition; nothing here enqueues a self-mod unattended. A human runs a proposal from the
Self-Modify tab (an ordinary `change_request`, origin=user, normal approval posture), or
*schedules* one — which creates a ONE-SHOT kernel schedule trigger (`config.once`), so an
unattended run inherits every ring-0 trigger rail: the master switch, the daily cap, and
the hold-unless-verified posture. Full autonomy is the `advisor` TRIGGER KIND (ring 0,
operator-configured): the kernel polls `/auto_queue` below and files open proposals
itself, under the same rails — ring 3 proposes, ring 0 disposes. The optional scheduled
ANALYSIS loop only ever produces text; it never touches the pipeline. Analysis goes
through the `/llm_call` syscall, so it is metered and budget-capped and provider keys
never enter this process.

Offline: with the scripted engine there is no model, so analysis parses literal
`__ADVISOR_PROPOSAL__ <json>` markers out of the gathered signals (planted e.g. in a
seeded error record) — the same test-affordance pattern as the verifier's
`__VERIFY_CHECK__`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import re
import threading
import time
import uuid
from typing import Any, Iterator

import httpx
from fastapi import APIRouter

import errorlog

PLUGIN = {
    "name": "advisor",
    "version": "1.0.0",
    "description": "Self-reflection loop: mines the system's own telemetry (errors, failed "
                   "versions, audit, spend, checks) into ready-to-run improvement proposals.",
}

MARKER = "__ADVISOR_PROPOSAL__"

MAX_NEW_PER_RUN = 5        # proposals accepted from one analysis
MAX_OPEN = 20              # open proposals kept before new ones are refused
MAX_KEPT = 100             # total entries retained (history included)
COOLDOWN_DAYS = 14         # dismissed/run fingerprints don't re-propose within this window
_PROMPT_BUDGET = 24000     # telemetry chars shown to the analyst model

# Registry statuses that represent a failed/undone change — the paper's "mine failures".
_FAILURE_STATUSES = {"health_failed", "verify_failed", "rejected", "rolled_back",
                     "reverted", "abandoned"}

AUTO_MAX_MINUTES = 10080          # scheduled-analysis interval ceiling (a week)
_AUTO_POLL_SECONDS = 60           # how often the clock loop re-checks the config
_TRIGGER_TEMPLATE_MAX = 4000      # kernel triggers._TEMPLATE_MAX — keep in sync
_HHMM_RE = re.compile(r"^\d{1,2}:\d{2}$")

HERE = pathlib.Path(__file__).resolve().parent
DATA_DIR = pathlib.Path(os.environ.get("QUINE_DATA_DIR") or (HERE.parent / ".data"))
ADVISOR_DIR = DATA_DIR / "advisor"
STORE_FILE = ADVISOR_DIR / "proposals.json"
CONFIG_FILE = ADVISOR_DIR / "config.json"

_LOCK = threading.Lock()          # store file access
_ANALYZE_LOCK = asyncio.Lock()    # one analysis at a time

_SYSTEM = """\
You are the Advisor of a self-modifying app harness ("Quine"). You are given telemetry the
system collected about itself: unresolved runtime error groups, recent version outcomes
(health/verification failures, rollbacks, rejections), transcripts of failed
self-modification runs (which tools errored, where the agent struggled), the audit tail,
model spend, and verification checks. Mine it for the highest-leverage improvements and
output proposals. Harness friction counts as much as app bugs: if the failed-run
transcripts show the agent repeatedly tripping over a tool or a prompt gap, propose a fix
to the agent's own runtime.

A proposal:
  {"title": "<short imperative statement>",
   "rationale": "<why this matters, grounded in the telemetry>",
   "effort": "small|medium|large",
   "evidence": [{"kind": "error|version|audit|spend|check", "ref": "<fingerprint/version/id>",
                 "summary": "<one line>"}],
   "prompt": "<a complete, self-contained change request>",
   "acceptance_criteria": ["<observable behavior>", ...]}

Rules:
- At most {max_new} proposals; fewer, sharper proposals beat many shallow ones.
- Every proposal must cite real evidence from the telemetry (actual fingerprints, version
  ids, audit events) — never invent references.
- "prompt" is handed VERBATIM to the self-modifying agent later, with none of this
  telemetry attached: embed the key evidence in it (error message, traceback excerpt,
  failing route) and state the acceptance criteria, so it stands alone.
- Propose changes to the app layer only (features, routes, UI, the agent's own runtime) —
  never the kernel, protected state, or secrets.
- Skip anything whose title is in already_proposed (it is open or was recently dismissed).
- Nothing worth proposing is a fine outcome.

Output STRICT JSON, nothing else: {"proposals": [<proposal>, ...]}
"""


# ── syscall helpers (local copies: importing `main` here would be circular — main loads
# plugins at import time). Env is read per-call so tests can stub late. ─────────────────
def _syscall_url() -> str:
    return os.environ.get("QUINE_SYSCALL_URL", "")


def _headers() -> dict:
    token = os.environ.get("KERNEL_AUTH_TOKEN", "")
    return {"authorization": f"Bearer {token}"} if token else {}


async def _syscall_get(path: str) -> dict:
    if not _syscall_url():
        return {"error": "no syscall url"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            return (await c.get(_syscall_url() + path, headers=_headers())).json()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _syscall_post(path: str, payload: dict, timeout: float | None = 120) -> dict:
    if not _syscall_url():
        return {"ok": False, "error": "no syscall url"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            return (await c.post(_syscall_url() + path, json=payload,
                                 headers=_headers())).json()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── proposal store (data partition: runtime state, not code — survives reboots) ────────
def _read_store() -> dict:
    if STORE_FILE.exists():
        try:
            d = json.loads(STORE_FILE.read_text(encoding="utf-8")) or {}
            if isinstance(d, dict):
                d.setdefault("proposals", [])
                d.setdefault("dismissed", {})
                d.setdefault("last_analysis", None)
                return d
        except Exception:
            pass
    return {"proposals": [], "dismissed": {}, "last_analysis": None}


def _write_store(d: dict) -> None:
    ADVISOR_DIR.mkdir(parents=True, exist_ok=True)
    rows = d.get("proposals", [])
    overflow = len(rows) - MAX_KEPT
    if overflow > 0:
        # Retention drops HISTORY (run/dismissed), oldest first — never a live entry: a
        # truncated "scheduled" proposal would vanish from the panel while its one-shot
        # trigger stays armed. The final slice keeps the cap hard regardless.
        trimmed = []
        for p in rows:
            if overflow > 0 and p.get("status") not in ("open", "scheduled"):
                overflow -= 1
                continue
            trimmed.append(p)
        rows = trimmed[-MAX_KEPT:]
    d["proposals"] = rows
    STORE_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


# ── advisor config (data partition; the only knob is the scheduled-analysis clock) ─────
def read_config() -> dict:
    cfg = {"auto_analyze_minutes": 0}  # 0 = scheduled analysis off (the default)
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                cfg.update({k: raw[k] for k in cfg if k in raw})
        except Exception:
            pass
    return cfg


def write_config(patch: dict) -> tuple[dict | None, str]:
    minutes = patch.get("auto_analyze_minutes")
    if (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
            or not 0 <= minutes <= AUTO_MAX_MINUTES):
        return None, f"auto_analyze_minutes must be a number 0..{AUTO_MAX_MINUTES} (0 = off)"
    cfg = read_config()
    cfg["auto_analyze_minutes"] = int(minutes)
    ADVISOR_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg, ""


# ── pure helpers (unit-tested directly) ─────────────────────────────────────────────────
def _trim(value: Any, limit: int) -> str:
    s = str(value or "")
    return s if len(s) <= limit else s[:limit] + "…"


def iter_strings(obj: Any) -> Iterator[str]:
    """Every string value in a nested JSON-ish structure (marker scan input)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from iter_strings(v)


def parse_marker_proposals(obj: Any) -> tuple[list[Any], str]:
    """Scripted-engine path: collect `__ADVISOR_PROPOSAL__ <json>` payloads (one spec or a
    list) from every string in `obj`. Returns (raw_specs, error)."""
    decoder = json.JSONDecoder()
    raw: list[Any] = []
    for text in iter_strings(obj):
        idx = text.find(MARKER)
        while idx != -1:
            rest = text[idx + len(MARKER):].lstrip()
            try:
                value, _ = decoder.raw_decode(rest)
            except ValueError:
                return [], f"unparseable {MARKER} payload"
            raw.extend(value if isinstance(value, list) else [value])
            idx = text.find(MARKER, idx + len(MARKER))
    return raw, ""


def _extract_json(text: str) -> Any:
    """First JSON value in a model reply (tolerates markdown fences / prose around it)."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(text[i:])
                return value
            except ValueError:
                continue
    raise ValueError("no JSON found in reply")


def validate_proposal(spec: Any) -> tuple[dict | None, str]:
    """Normalize one raw proposal spec. Only a missing title/prompt rejects; everything
    else is coerced and bounded (model output is untrusted)."""
    if not isinstance(spec, dict):
        return None, "proposal must be an object"
    title = str(spec.get("title") or "").strip()
    prompt = str(spec.get("prompt") or "").strip()
    if not title:
        return None, "proposal missing title"
    if not prompt:
        return None, "proposal missing prompt"
    effort = str(spec.get("effort") or "medium").strip().lower()
    if effort not in ("small", "medium", "large"):
        effort = "medium"
    evidence = []
    for ev in (spec.get("evidence") or [])[:8]:
        if isinstance(ev, dict):
            evidence.append({"kind": _trim(ev.get("kind"), 20),
                             "ref": _trim(ev.get("ref"), 80),
                             "summary": _trim(ev.get("summary"), 300)})
    criteria = [_trim(c, 300) for c in (spec.get("acceptance_criteria") or [])[:6]
                if str(c or "").strip()]
    return {"title": _trim(title, 200), "rationale": _trim(spec.get("rationale"), 2000),
            "effort": effort, "evidence": evidence, "prompt": _trim(prompt, 6000),
            "acceptance_criteria": criteria}, ""


def proposal_fingerprint(p: dict) -> str:
    """Stable identity for dedupe/cooldown: normalized title + cited evidence refs."""
    title = " ".join(str(p.get("title") or "").lower().split())
    refs = sorted(str(ev.get("ref") or "") for ev in p.get("evidence") or [])
    return hashlib.sha256("|".join([title, *refs]).encode("utf-8")).hexdigest()[:16]


def _suppressed_until(store: dict, now: float) -> dict[str, float]:
    """fingerprint → epoch until which it must not be re-proposed. Open proposals suppress
    indefinitely; dismissed/run ones for COOLDOWN_DAYS (then the topic re-arms)."""
    cooldown = COOLDOWN_DAYS * 86400
    until: dict[str, float] = {}
    for fp, ts in (store.get("dismissed") or {}).items():
        until[fp] = max(until.get(fp, 0), float(ts) + cooldown)
    for p in store.get("proposals") or []:
        fp = p.get("fingerprint") or ""
        if p.get("status") in ("open", "scheduled"):
            until[fp] = float("inf")
        elif p.get("status") == "run":
            until[fp] = max(until.get(fp, 0), float(p.get("run_at") or now) + cooldown)
        elif p.get("status") == "dismissed":
            until[fp] = max(until.get(fp, 0), float(p.get("dismissed_at") or 0) + cooldown)
    return until


def merge_proposals(store: dict, specs: list[Any], now: float) -> tuple[list[dict], str]:
    """Validate + dedupe raw specs into the store (mutates it). Returns (added, note)."""
    until = _suppressed_until(store, now)
    added: list[dict] = []
    note = ""
    for spec in specs:
        if len(added) >= MAX_NEW_PER_RUN:
            note = f"capped at {MAX_NEW_PER_RUN} new proposals per analysis"
            break
        norm, err = validate_proposal(spec)
        if norm is None:
            note = err
            continue
        fp = proposal_fingerprint(norm)
        if now < until.get(fp, 0):
            continue  # open, or inside a dismissal/run cooldown
        open_count = sum(1 for p in store["proposals"] if p.get("status") == "open")
        if open_count >= MAX_OPEN:
            note = f"open-proposal cap ({MAX_OPEN}) reached — dismiss some first"
            break
        entry = {"id": "p" + uuid.uuid4().hex[:8], "fingerprint": fp, "created": now,
                 "status": "open", **norm}
        store["proposals"].append(entry)
        until[fp] = float("inf")
        added.append(entry)
    return added, note


def build_analysis_messages(signals: dict, already_proposed: list[str]) -> list[dict]:
    payload = {"telemetry": signals, "already_proposed": already_proposed}
    return [
        {"role": "system", "content": _SYSTEM.replace("{max_new}", str(MAX_NEW_PER_RUN))},
        {"role": "user", "content": json.dumps(payload, indent=1)[:_PROMPT_BUDGET]},
    ]


# ── signal gathering (read-only; every source degrades to an error note, never raises) ──
def _scalars(d: Any, cap: int = 24) -> dict:
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in list(d.items())[:cap]
            if isinstance(v, (str, int, float, bool)) or v is None}


def _trim_error_group(g: dict) -> dict:
    return {"fingerprint": g.get("fingerprint"), "exc_type": g.get("exc_type"),
            "message": _trim(g.get("message"), 2000), "route": g.get("route"),
            "source": g.get("source"), "count": g.get("count"),
            "last_ts": g.get("last_ts"), "versions": g.get("versions"),
            "traceback_tail": _trim(g.get("last_traceback"), 1200)}


def _trim_version(v: dict) -> dict:
    health = v.get("health") or {}
    return {"version": (v.get("version") or v.get("sha") or "")[:12],
            "seq": v.get("seq"), "label": v.get("label"), "status": v.get("status"),
            "task": v.get("task"), "prompt": _trim(v.get("prompt"), 240),
            "health_reason": _trim(health.get("reason"), 300),
            "log_tail": _trim(health.get("log_tail"), 800)}


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    out: list[dict] = []
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def mine_transcript(msgs: list[dict]) -> dict:
    """Friction extract from a self-mod transcript: the request, the tool-result errors the
    agent hit, and its final note — the paper's "mine failures" applied to the agent's own
    runs, so the Advisor can propose fixes to the harness (prompt/tools), not just the app."""
    prompt = ""
    tool_errors: list[str] = []
    last_assistant = ""
    steps = 0
    for m in msgs:
        role = m.get("role")
        content = m.get("content")
        text = content if isinstance(content, str) else json.dumps(content or "")
        if role == "user" and not prompt:
            prompt = _trim(text, 240)
        elif role == "assistant":
            if m.get("tool_calls"):
                steps += 1
            if text.strip():
                last_assistant = _trim(text, 300)
        elif role == "tool":
            low = text.lower()
            if "error" in low or "traceback" in low or "failed" in low:
                tool_errors.append(_trim(text, 300))
    return {"prompt": prompt, "steps": steps, "tool_errors": tool_errors[-4:],
            "last_assistant": last_assistant}


def _trim_audit(row: dict) -> dict:
    out = {"ts": row.get("ts"), "event": row.get("event")}
    extras = {k: _trim(v, 120) for k, v in row.items()
              if k not in ("ts", "event") and isinstance(v, (str, int, float, bool))}
    out.update(dict(list(extras.items())[:3]))
    return out


def _trim_check(c: dict) -> dict:
    last = c.get("last_result") or {}
    return {"id": c.get("id"), "name": (c.get("spec") or {}).get("name"),
            "origin": (c.get("origin") or "")[:12], "origin_status": c.get("origin_status"),
            "enabled": c.get("enabled"),
            "last_ok": last.get("ok") if isinstance(last, dict) else None}


async def gather_signals() -> dict:
    """Snapshot of everything the Advisor reasons over — trimmed hard so the analysis
    prompt stays bounded. Failed syscalls become {"error": ...} notes, not exceptions."""
    signals: dict[str, Any] = {}

    groups = errorlog.list_groups(include_resolved=False)
    signals["errors"] = [_trim_error_group(g) for g in groups[:12]]

    versions = await _syscall_get("/versions?limit=50")
    if "error" in versions:
        signals["versions"] = versions
    else:
        rows = versions.get("versions") or []
        by_status: dict[str, int] = {}
        for v in rows:
            s = str(v.get("status") or "unknown")
            by_status[s] = by_status.get(s, 0) + 1
        failures = [v for v in rows if v.get("status") in _FAILURE_STATUSES]
        signals["versions"] = {"counts_by_status": by_status,
                               "recent_failures": [_trim_version(v) for v in failures[:10]]}
        # Transcripts of the failed runs themselves (commit snapshots on the data partition):
        # what the agent tried, which tools errored, how it signed off.
        transcripts = []
        for v in failures[:3]:
            task = str(v.get("task") or "")
            path = DATA_DIR / "selfmod_convos" / f"{task}.jsonl"
            if task and path.exists():
                transcripts.append({"version": (v.get("version") or v.get("sha") or "")[:12],
                                    "status": v.get("status"), "task": task,
                                    **mine_transcript(_read_jsonl(path))})
        signals["failed_run_transcripts"] = transcripts

    audit = await _syscall_get("/audit?limit=40")
    signals["audit"] = ([_trim_audit(r) for r in audit.get("audit") or []]
                        if "error" not in audit else audit)

    spend = await _syscall_get("/spend")
    signals["spend"] = _scalars(spend) or spend

    checks = await _syscall_get("/checks")
    if "error" in checks:
        signals["checks"] = checks
    else:
        signals["checks"] = {"gate_enabled": checks.get("enabled"),
                             "checks": [_trim_check(c) for c in (checks.get("checks") or [])[:20]]}

    status = await _syscall_get("/status")
    signals["status"] = _scalars(status) or status
    return signals


# ── analysis ────────────────────────────────────────────────────────────────────────────
async def run_analysis() -> dict:
    """One observe→propose pass: gather signals, derive proposals (model, or scripted
    markers), merge them into the store. Returns a summary for the UI."""
    if _ANALYZE_LOCK.locked():
        return {"ok": False, "reason": "analysis already running"}
    async with _ANALYZE_LOCK:
        now = time.time()
        cfg_resp = await _syscall_get("/config")
        cfg = cfg_resp.get("config") or {}
        engine = (cfg.get("agent") or {}).get("engine") or ""
        signals = await gather_signals()

        if engine == "scripted":
            # Offline: markers may hide in strings the prompt-trimming would cut, so scan
            # the RAW error groups too, not just the trimmed signal snapshot.
            raw = [errorlog.list_groups(include_resolved=False), signals]
            specs, err = parse_marker_proposals(raw)
            if err:
                return _finish_analysis(now, engine, ok=False, reason=err)
        else:
            model = (cfg.get("agent") or {}).get("model") or ""
            if not model:
                return _finish_analysis(now, engine, ok=False,
                                        reason="no model configured (agent.model)")
            with _LOCK:
                store = _read_store()
            already = [p["title"] for p in store["proposals"]
                       if p.get("status") in ("open", "dismissed", "run")][-40:]
            resp = await _syscall_post("/llm_call", {
                "model": model, "temperature": 0.0, "max_tokens": 4000,
                "messages": build_analysis_messages(signals, already)})
            if not resp.get("ok"):
                return _finish_analysis(now, engine, ok=False,
                                        reason=f"llm_call failed: {resp.get('error')}")
            try:
                content = resp["response"]["choices"][0]["message"]["content"] or ""
                data = _extract_json(content)
            except Exception as exc:
                return _finish_analysis(now, engine, ok=False,
                                        reason=f"unparseable analysis reply: {exc}")
            specs = data.get("proposals") if isinstance(data, dict) else data
            if not isinstance(specs, list):
                return _finish_analysis(now, engine, ok=False,
                                        reason="analysis reply had no proposals list")

        with _LOCK:
            store = _read_store()
            added, note = merge_proposals(store, specs, now)
            summary = _summarize(now, engine, ok=True, new=len(added), note=note,
                                 signals=signals)
            store["last_analysis"] = summary
            _write_store(store)
        return {**summary, "added": added}


def _summarize(now: float, engine: str, *, ok: bool, new: int = 0, reason: str = "",
               note: str = "", signals: dict | None = None) -> dict:
    counts = {}
    if signals:
        errs = signals.get("errors")
        vers = (signals.get("versions") or {})
        counts = {"error_groups": len(errs) if isinstance(errs, list) else 0,
                  "recent_failures": len(vers.get("recent_failures") or [])
                  if isinstance(vers, dict) else 0}
    return {"ok": ok, "ts": now, "engine": engine, "new": new,
            "reason": reason or note, "signal_counts": counts}


def _finish_analysis(now: float, engine: str, *, ok: bool, reason: str) -> dict:
    summary = _summarize(now, engine, ok=ok, reason=reason)
    with _LOCK:
        store = _read_store()
        store["last_analysis"] = summary
        _write_store(store)
    return summary


# ── scheduled analysis (the clock only ever produces text — never a self-mod) ──────────
async def auto_tick(now: float | None = None) -> bool:
    """One poll of the scheduled-analysis clock: run an analysis when the configured
    interval has elapsed. Stamps last_auto_ts BEFORE running so a failing analysis backs
    off for a full interval instead of hot-looping. Returns True when an analysis ran."""
    now = time.time() if now is None else now
    interval = float(read_config().get("auto_analyze_minutes") or 0)
    if interval <= 0:
        return False
    with _LOCK:
        store = _read_store()
        if now - float(store.get("last_auto_ts") or 0) < interval * 60:
            return False
        store["last_auto_ts"] = now
        _write_store(store)
    await run_analysis()
    return True


_AUTO_TASK: asyncio.Task | None = None


async def _auto_loop() -> None:
    while True:
        try:
            await auto_tick()
        except Exception as exc:  # the clock must survive any analysis failure
            errorlog.capture(exc, source="advisor")
        await asyncio.sleep(_AUTO_POLL_SECONDS)


def setup(app) -> None:
    """Plugin lifecycle hook (see features/plugins.py): run the scheduled-analysis clock
    for the app's lifetime. With auto_analyze_minutes=0 (the default) the loop just idles;
    flipping the config takes effect within a poll — no restart needed."""
    async def _start() -> None:
        global _AUTO_TASK
        if _AUTO_TASK is None or _AUTO_TASK.done():
            _AUTO_TASK = asyncio.get_running_loop().create_task(_auto_loop())

    async def _stop() -> None:
        global _AUTO_TASK
        if _AUTO_TASK is not None:
            _AUTO_TASK.cancel()
            _AUTO_TASK = None

    # starlette ≥1.3 dropped add_event_handler; the router's hook lists remain the
    # version-stable seam (they run inside the default lifespan).
    app.router.on_startup.append(_start)
    app.router.on_shutdown.append(_stop)


# ── HTTP surface ─────────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/plugins/advisor", tags=["plugin:advisor"])


async def _sync_scheduled() -> None:
    """Reconcile proposals against the kernel's trigger registry. Scheduled ones: a fired
    one-shot becomes status "run" (cooldown applies); a trigger the operator deleted
    re-opens its proposal. Open ones: an `advisor`-kind trigger that auto-filed a proposal
    recorded its id in fired_fingerprints — mirror that here as "run" too. Quietly skipped
    when the kernel is unreachable."""
    with _LOCK:
        if not any(p.get("status") in ("scheduled", "open")
                   for p in _read_store()["proposals"]):
            return
    data = await _syscall_get("/triggers")
    if "error" in data:
        return
    rows = {t.get("id"): t for t in data.get("triggers") or []}
    auto_filed: dict[str, float] = {}  # proposal id → fired ts (advisor-kind bookkeeping)
    for t in data.get("triggers") or []:
        if t.get("kind") == "advisor":
            for pid, ts in (t.get("fired_fingerprints") or {}).items():
                auto_filed[pid] = max(auto_filed.get(pid, 0), float(ts or 0))
    with _LOCK:
        store = _read_store()
        changed = False
        for p in store["proposals"]:
            if p.get("status") == "open" and p.get("id") in auto_filed:
                p["status"] = "run"
                p["run_at"] = auto_filed[p["id"]]
                changed = True
            if p.get("status") != "scheduled":
                continue
            trg = rows.get(p.get("trigger_id"))
            if trg is None:  # operator deleted the trigger — the proposal is usable again
                p["status"] = "open"
                p.pop("trigger_id", None)
                p.pop("scheduled_for", None)
                changed = True
            elif trg.get("last_fired"):
                p["status"] = "run"
                p["run_at"] = trg["last_fired"]
                task = (trg.get("last_result") or {}).get("task")
                if task:
                    p["run_task"] = task
                changed = True
        if changed:
            _write_store(store)


@router.get("/proposals")
async def list_proposals() -> dict:
    await _sync_scheduled()
    with _LOCK:
        store = _read_store()
    proposals = list(reversed(store["proposals"]))  # newest first
    return {"proposals": proposals,
            "open": sum(1 for p in proposals if p.get("status") == "open"),
            "last_analysis": store.get("last_analysis"),
            "config": read_config()}


@router.get("/auto_queue")
async def auto_queue() -> dict:
    """What an `advisor`-kind kernel trigger polls: open proposals as ready-to-fire
    change-request prompts (oldest first — FIFO fairness). Read-only: ring 3 only ever
    PROPOSES here; whether/when anything is filed is decided by the operator-configured
    ring-0 trigger and its rails (master switch, daily cap, cooldown, hold posture)."""
    with _LOCK:
        store = _read_store()
    rows = [{"id": p["id"], "title": p["title"], "prompt": run_prompt(p)}
            for p in store["proposals"] if p.get("status") == "open"][:5]
    return {"proposals": rows}


@router.get("/config")
async def get_config() -> dict:
    return {"ok": True, "config": read_config()}


@router.post("/config")
async def set_config(payload: dict) -> dict:
    cfg, err = write_config(payload or {})
    if cfg is None:
        return {"ok": False, "reason": err}
    return {"ok": True, "config": cfg}


@router.post("/analyze")
async def analyze() -> dict:
    return await run_analysis()


@router.post("/dismiss")
async def dismiss(payload: dict) -> dict:
    pid = str((payload or {}).get("id") or "").strip()
    now = time.time()
    with _LOCK:
        store = _read_store()
        for p in store["proposals"]:
            if p.get("id") == pid and p.get("status") == "open":
                p["status"] = "dismissed"
                p["dismissed_at"] = now
                store["dismissed"][p.get("fingerprint") or pid] = now
                _write_store(store)
                return {"ok": True, "id": pid}
    return {"ok": False, "reason": "no open proposal with that id"}


def run_prompt(p: dict) -> str:
    """The change-request text a proposal submits: provenance tag + the self-contained
    prompt + the cited evidence and acceptance criteria (restated — the raw telemetry
    itself never travels)."""
    parts = [f"[advisor:{p['id']}] {p['title']}", "", p["prompt"]]
    if p.get("evidence"):
        parts += ["", "Evidence (from the system's own telemetry):"]
        parts += [f"- {ev.get('kind')}: {ev.get('ref')} — {ev.get('summary')}"
                  for ev in p["evidence"]]
    if p.get("acceptance_criteria"):
        parts += ["", "Acceptance criteria:"]
        parts += [f"- {c}" for c in p["acceptance_criteria"]]
    return "\n".join(parts)


@router.post("/run")
async def run(payload: dict) -> dict:
    """Enqueue one proposal as an ordinary change_request (origin=user — a human clicked).
    Blocks until the task finishes, like the syscall itself; the UI's primary flow instead
    prefills the Self-Modify prompt box so the user can steer before submitting."""
    pid = str((payload or {}).get("id") or "").strip()
    now = time.time()
    with _LOCK:
        store = _read_store()
        target = next((p for p in store["proposals"]
                       if p.get("id") == pid and p.get("status") == "open"), None)
        if target is None:
            return {"ok": False, "reason": "no open proposal with that id"}
        target["status"] = "run"
        target["run_at"] = now
        _write_store(store)

    res = await _syscall_post("/change_request", {"prompt": run_prompt(target)},
                              timeout=None)
    with _LOCK:
        store = _read_store()
        for p in store["proposals"]:
            if p.get("id") == pid:
                if res.get("task"):
                    p["run_task"] = res["task"]
                if not res.get("ok") and not res.get("task"):
                    p["status"] = "open"  # rejected before it ever queued — keep it usable
                    p.pop("run_at", None)
                _write_store(store)
                break
    return {"ok": bool(res.get("ok")), "id": pid, "result": res}


@router.post("/schedule")
async def schedule(payload: dict) -> dict:
    """Schedule one proposal to run at HH:MM UTC — by creating a ONE-SHOT kernel schedule
    trigger, so the unattended run rides every ring-0 rail (master switch, daily cap,
    hold-unless-verified). The Advisor never flips those switches itself; the response
    reports triggers_enabled so the UI can tell the operator to."""
    p = payload or {}
    pid = str(p.get("id") or "").strip()
    daily_at = str(p.get("daily_at") or "").strip()
    if not _HHMM_RE.match(daily_at):
        return {"ok": False, "reason": 'daily_at must be "HH:MM" (24h, UTC)'}
    with _LOCK:
        store = _read_store()
        target = next((x for x in store["proposals"]
                       if x.get("id") == pid and x.get("status") == "open"), None)
    if target is None:
        return {"ok": False, "reason": "no open proposal with that id"}
    template = run_prompt(target)
    if len(template) > _TRIGGER_TEMPLATE_MAX:
        return {"ok": False, "reason": "proposal too long to schedule — run it directly"}
    resp = await _syscall_post("/triggers", {
        "name": f"advisor {pid}", "kind": "schedule", "prompt_template": template,
        "config": {"daily_at": daily_at, "once": True}})
    if not resp.get("ok"):
        return {"ok": False,
                "reason": f"trigger create failed: {resp.get('reason') or resp.get('error')}"}
    trigger = resp.get("trigger") or {}
    with _LOCK:
        store = _read_store()
        for x in store["proposals"]:
            if x.get("id") == pid and x.get("status") == "open":
                x["status"] = "scheduled"
                x["trigger_id"] = trigger.get("id")
                x["scheduled_for"] = daily_at
                _write_store(store)
                break
    listing = await _syscall_get("/triggers")
    enabled = (bool((listing.get("config") or {}).get("enabled"))
               if "error" not in listing else None)
    return {"ok": True, "id": pid, "trigger": trigger.get("id"),
            "scheduled_for": daily_at, "triggers_enabled": enabled}


@router.post("/unschedule")
async def unschedule(payload: dict) -> dict:
    """Cancel a scheduled proposal: delete its one-shot trigger and re-open it. If the
    trigger already fired, the proposal is marked run instead (the change is in flight)."""
    pid = str((payload or {}).get("id") or "").strip()
    with _LOCK:
        store = _read_store()
        target = next((x for x in store["proposals"]
                       if x.get("id") == pid and x.get("status") == "scheduled"), None)
    if target is None:
        return {"ok": False, "reason": "no scheduled proposal with that id"}
    await _sync_scheduled()  # a fire or an operator delete may have already resolved it
    with _LOCK:
        store = _read_store()
        target = next((x for x in store["proposals"] if x.get("id") == pid), None)
    if target is None or target.get("status") != "scheduled":
        return {"ok": False,
                "reason": f"no longer scheduled (now: {(target or {}).get('status', 'gone')})"}
    res = await _syscall_post("/triggers/delete", {"id": target.get("trigger_id")})
    if not res.get("ok"):
        # The trigger is still armed (e.g. operator_auth denies the app's delete, or the
        # kernel hiccuped) — do NOT re-open the proposal, or it would look cancelled while
        # the one-shot still fires. A trigger the operator deleted out from under us is
        # already handled above by _sync_scheduled.
        return {"ok": False, "id": pid, "reason": "couldn't delete the trigger — the "
                f"schedule is still armed: {res.get('reason') or res.get('error') or '?'}"}
    with _LOCK:
        store = _read_store()
        for x in store["proposals"]:
            if x.get("id") == pid and x.get("status") == "scheduled":
                x["status"] = "open"
                x.pop("trigger_id", None)
                x.pop("scheduled_for", None)
                _write_store(store)
                break
    return {"ok": True, "id": pid}


# ── Run-agent tools (read-only over code: listing + a metered analysis pass) ───────────
async def _tool_proposals(args: dict, ctx) -> str:
    with _LOCK:
        store = _read_store()
    rows = [{"id": p["id"], "title": p["title"], "status": p["status"],
             "effort": p.get("effort"), "rationale": _trim(p.get("rationale"), 300),
             "evidence": [f"{ev.get('kind')}:{ev.get('ref')}" for ev in p.get("evidence") or []]}
            for p in reversed(store["proposals"])
            if p.get("status") in ("open", "scheduled")]
    if not rows:
        return ("no open advisor proposals — run advisor_analyze to mine the current "
                "telemetry for improvement suggestions")
    return json.dumps(rows, indent=1)


async def _tool_analyze(args: dict, ctx) -> str:
    res = await run_analysis()
    return json.dumps({"ok": res.get("ok"), "new_proposals": res.get("new", 0),
                       "reason": res.get("reason") or ""})


TOOLS = {
    "advisor_proposals": {
        "schema": {
            "type": "function",
            "function": {
                "name": "advisor_proposals",
                "description": "List the Advisor's current improvement proposals (mined from "
                               "the system's own telemetry: errors, failed versions, spend). "
                               "Read-only; the user runs/schedules them in the Self-Modify tab.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        "handler": _tool_proposals,
    },
    "advisor_analyze": {
        "schema": {
            "type": "function",
            "function": {
                "name": "advisor_analyze",
                "description": "Run one Advisor analysis pass now: mine the system's current "
                               "telemetry into new improvement proposals (uses one metered "
                               "model call; produces text proposals only, never code changes).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        "handler": _tool_analyze,
    },
}
