"""Autonomous triggers: the kernel's scheduler + event source (ring 0).

Makes the agent proactive. A *trigger* dispatches a self-modification when a condition
fires — a schedule elapses, an HMAC'd webhook arrives, the app's error tracker spikes, or
the app's Advisor has an open improvement proposal (`advisor` kind) — by enqueuing a
normal `change_request` onto the durable FIFO. So every autonomous change still flows
through the full validate → derive → verify pipeline, and (by default) HOLDS for human
approval; nothing here bypasses a single safety gate.

Why ring 0, not an app feature:
  • the healer must survive a broken patient — a self-mod that breaks the app can't run its
    own cron to fix itself; this loop lives in the kernel and polls the app from outside;
  • trigger definitions live in protected `state/triggers.json`, so the mutable app / the
    self-mod agent can never grant themselves autonomy the operator didn't configure;
  • the master kill switch, the daily rate cap, and the "hold unless verified" posture are
    enforced below the mutable layer.

The firing conditions are pure, clock-injectable functions (`schedule_due`, `spike_hits`,
`render_template`, `verify_webhook`) so the whole thing is deterministically testable
offline; `TriggerManager.tick(now)` orchestrates them and the async error poll.

`config.once` makes any trigger one-shot: its first fire self-disables it (kept, audited,
re-enable to re-arm) — how the Advisor schedules a proposal to run once at a set time.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import re
import secrets
import time
import uuid
from typing import Any, Awaitable, Callable

from kernel import state_store

KINDS = ("schedule", "webhook", "error_spike", "advisor")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,48}$")
_TEMPLATE_MAX = 4000
_TRACEBACK_TAIL = 1500

# enqueue(prompt, *, trigger_id, trigger_name) -> task_id (fire-and-forget onto the FIFO)
Enqueue = Callable[..., str]
# fetch_error_groups() -> unresolved error groups from the ACTIVE app (async), or None
FetchErrors = Callable[[], Awaitable[list[dict] | None]]
# fetch_proposals() -> the ACTIVE app's Advisor auto-queue (open proposals), or None
FetchProposals = Callable[[], Awaitable[list[dict] | None]]


# ── pure helpers (clock-injectable; unit-tested directly) ──────────────────────────────
def new_secret() -> str:
    return secrets.token_hex(16)


def sign(secret: str, body: bytes) -> str:
    """The `sha256=<hex>` signature a webhook sender must send in X-Quine-Signature."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_webhook(secret: str, body: bytes, presented: str | None) -> bool:
    """Constant-time HMAC check of a webhook body. No secret configured ⇒ never accept
    (an unsigned webhook trigger would be an open self-mod trigger to the whole internet)."""
    if not secret or not presented:
        return False
    # Bytes compare: compare_digest raises TypeError on a non-ASCII str, and this endpoint is
    # edge-auth-exempt (internet-facing) — a crafted header must yield 401, never a 500.
    return hmac.compare_digest(sign(secret, body).encode(),
                               presented.strip().encode("utf-8"))


def _day_key(now: float) -> str:
    return _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%d")


def schedule_due(trigger: dict, now: float) -> bool:
    """Whether a schedule trigger should fire at `now`. Supports `interval_minutes`
    (fixed cadence since last fire) and `daily_at` "HH:MM" UTC (once per day, on/after the
    time). Deterministic given last_fired + now."""
    cfg = trigger.get("config") or {}
    last = trigger.get("last_fired")
    interval = cfg.get("interval_minutes")
    if interval:
        if last is None:
            return True  # never fired → fire on the first evaluation
        return now - last >= float(interval) * 60
    daily = cfg.get("daily_at")
    if daily and re.match(r"^\d{1,2}:\d{2}$", str(daily)):
        hh, mm = (int(x) for x in str(daily).split(":"))
        now_dt = _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
        target = now_dt.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
        if now_dt < target:
            return False  # not yet time today
        # Fire once per day: only if we haven't already fired since today's target time.
        return (last or 0) < target.timestamp()
    return False


def spike_hits(trigger: dict, groups: list[dict], now: float) -> list[dict]:
    """Error groups that should trip an error_spike trigger right now. A group trips when
    it is unresolved, recently active (last_ts within `window_minutes`), and either has
    reached `threshold` occurrences or — with `trip_on_new` — is simply present; UNLESS it
    already fired within `cooldown_hours` (per-fingerprint dedupe). Cumulative `count` is a
    fine severity proxy because a fingerprint only ever fires once per cooldown."""
    cfg = trigger.get("config") or {}
    threshold = int(cfg.get("threshold", 3))
    window = float(cfg.get("window_minutes", 60)) * 60
    trip_on_new = bool(cfg.get("trip_on_new", False))
    cooldown = float(cfg.get("cooldown_hours", 24)) * 3600
    fired = trigger.get("fired_fingerprints") or {}
    hits = []
    for g in groups:
        # Groups come from the ring-3 app's /api/errors — a malformed entry (a self-mod may
        # reshape that endpoint) must skip, not crash the whole trigger tick.
        if not isinstance(g, dict) or g.get("resolved"):
            continue
        fp = g.get("fingerprint") or ""
        if not fp:
            continue
        try:
            last_ts = float(g.get("last_ts") or 0)
            count = int(g.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if window > 0 and now - last_ts > window:
            continue  # not recently active
        if not (count >= threshold or (trip_on_new and count >= 1)):
            continue
        last = fired.get(fp)  # never-fired must not look like an epoch-0 recent fire
        if last is not None and now - float(last) < cooldown:
            continue  # already fired for this fingerprint within the cooldown
        hits.append(g)
    return hits


def _cooldown_hours(trigger: dict) -> float:
    """Per-fingerprint re-fire cooldown. Advisor triggers default much longer (a month):
    an auto-filed proposal that failed shouldn't be silently retried the next day —
    mirrors the Advisor's own dismissal cooldown."""
    default = 720.0 if trigger.get("kind") == "advisor" else 24.0
    return float((trigger.get("config") or {}).get("cooldown_hours", default))


def advisor_hits(trigger: dict, proposals: list[dict], now: float) -> list[dict]:
    """Open Advisor proposals this trigger should auto-file right now: at most
    config.max_per_tick per pass (default 1 — one unattended change at a time), and a
    proposal id never re-fires within the cooldown."""
    cfg = trigger.get("config") or {}
    cap = int(cfg.get("max_per_tick", 1))
    cooldown = _cooldown_hours(trigger) * 3600
    fired = trigger.get("fired_fingerprints") or {}
    hits: list[dict] = []
    for p in proposals:
        pid = str(p.get("id") or "")
        if not pid or not str(p.get("prompt") or "").strip():
            continue
        last = fired.get(pid)  # never-fired must not look like an epoch-0 recent fire
        if last is not None and now - float(last) < cooldown:
            continue
        hits.append(p)
        if len(hits) >= cap:
            break
    return hits


def render_template(template: str, *, trigger: dict, error: dict | None = None,
                    payload: str | None = None, proposal: dict | None = None) -> str:
    """Render a prompt template's `{{placeholders}}`. Unknown placeholders are left intact
    (visible, not silently dropped). Available: trigger.name; for error_spike error.*
    (fingerprint/message/exc_type/route/count/traceback_tail); for webhook payload; for
    advisor proposal.* (id/title/prompt — prompt is the app-rendered change request)."""
    err = error or {}
    prop = proposal or {}
    values = {
        "trigger.name": str(trigger.get("name") or ""),
        "error.fingerprint": str(err.get("fingerprint") or ""),
        "error.message": str(err.get("message") or ""),
        "error.exc_type": str(err.get("exc_type") or ""),
        "error.route": str(err.get("route") or ""),
        "error.count": str(err.get("count") or ""),
        "error.traceback_tail": str(err.get("last_traceback") or "")[-_TRACEBACK_TAIL:],
        "payload": (payload or "")[:_TEMPLATE_MAX],
        "proposal.id": str(prop.get("id") or ""),
        "proposal.title": str(prop.get("title") or ""),
        "proposal.prompt": str(prop.get("prompt") or "")[:_TEMPLATE_MAX],
    }
    return re.sub(r"\{\{\s*([a-z_.]+)\s*\}\}",
                  lambda m: values.get(m.group(1), m.group(0)), template)


def validate_trigger(spec: Any) -> tuple[bool, str]:
    """Structural validation of a create/update spec (before it enters the store)."""
    if not isinstance(spec, dict):
        return False, "trigger must be an object"
    name = spec.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
        return False, "name must be 1-48 chars (letters, digits, space . _ -)"
    kind = spec.get("kind")
    if kind not in KINDS:
        return False, f"kind must be one of {list(KINDS)}"
    template = spec.get("prompt_template")
    if not isinstance(template, str) or not template.strip() or len(template) > _TEMPLATE_MAX:
        return False, f"prompt_template must be a non-empty string (max {_TEMPLATE_MAX} chars)"
    cfg = spec.get("config") or {}
    if not isinstance(cfg, dict):
        return False, "config must be an object"
    if "once" in cfg and not isinstance(cfg.get("once"), bool):
        return False, "config.once must be a boolean"
    if kind == "schedule":
        interval, daily = cfg.get("interval_minutes"), cfg.get("daily_at")
        if interval is not None:
            if not isinstance(interval, (int, float)) or not 1 <= interval <= 100000:
                return False, "config.interval_minutes must be 1..100000"
        elif daily is not None:
            if not (isinstance(daily, str) and re.match(r"^\d{1,2}:\d{2}$", daily)):
                return False, 'config.daily_at must be "HH:MM"'
        else:
            return False, "schedule needs config.interval_minutes or config.daily_at"
    elif kind == "error_spike":
        thr = cfg.get("threshold", 3)
        if not isinstance(thr, int) or not 1 <= thr <= 10000:
            return False, "config.threshold must be an int 1..10000"
    elif kind == "advisor":
        cap = cfg.get("max_per_tick", 1)
        if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 5:
            return False, "config.max_per_tick must be an int 1..5"
    return True, ""


# ── the manager ────────────────────────────────────────────────────────────────────────
class TriggerManager:
    def __init__(self, get_config: Callable[[], dict], enqueue: Enqueue,
                 fetch_errors: FetchErrors,
                 fetch_proposals: FetchProposals | None = None) -> None:
        self._get_config = get_config
        self._enqueue = enqueue
        self._fetch_errors = fetch_errors
        self._fetch_proposals = fetch_proposals

    def _cfg(self) -> dict:
        return self._get_config().get("triggers", {}) or {}

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", False))

    def _fires_today(self, triggers: list[dict], now: float) -> int:
        day = _day_key(now)
        return sum(int(t.get("fires_today") or 0)
                   for t in triggers if t.get("fires_day") == day)

    def _rate_limited(self, triggers: list[dict], now: float) -> bool:
        cap = int(self._cfg().get("max_per_day", 5))
        return self._fires_today(triggers, now) >= cap

    # ── firing ──────────────────────────────────────────────────────────────────────
    def _fire(self, trigger: dict, now: float, *, error: dict | None = None,
              payload: str | None = None, proposal: dict | None = None) -> str | None:
        """Render the trigger's prompt and enqueue an autonomous self-mod. Records the
        fire in the trigger's bookkeeping (last_fired, per-day count, per-fp cooldown).
        Returns the task_id, or None when rate-limited (caller already checked, but this is
        the authoritative gate). Skips a self-heal (or an auto-filed proposal) whose
        fingerprint is already in flight."""
        fp = (error or {}).get("fingerprint") or (proposal or {}).get("id") or ""
        if fp and _fingerprint_in_flight(fp):
            state_store.audit("trigger_skipped", trigger=trigger.get("id"),
                              reason="fingerprint already has an open task/pending fix",
                              fingerprint=fp)
            return None
        trigger_id = str(trigger.get("id") or "")
        prompt = render_template(trigger.get("prompt_template") or "", trigger=trigger,
                                 error=error, payload=payload, proposal=proposal)
        prompt = f"[auto:{trigger.get('name')}] {prompt}".strip()
        task_id = self._enqueue(prompt, trigger_id=trigger_id,
                                trigger_name=trigger.get("name"))
        day = _day_key(now)

        def _mut(entry: dict) -> None:
            # Capture BEFORE assigning fires_day, or the day-rollover reset is dead code and
            # yesterday's count carries into today (wrongly exhausting the daily cap).
            same_day = entry.get("fires_day") == day
            entry["last_fired"] = now
            entry["fires_day"] = day
            entry["fires_today"] = (int(entry.get("fires_today") or 0) if same_day else 0) + 1
            entry["last_result"] = {"task": task_id, "ts": now}
            # One-shot triggers (config.once) are consumed by their fire: self-disable, so
            # e.g. a scheduled proposal runs exactly once instead of recurring daily.
            if (entry.get("config") or {}).get("once"):
                entry["enabled"] = False
            if fp:
                ff = dict(entry.get("fired_fingerprints") or {})
                ff[fp] = now
                # prune stale cooldown entries so the map can't grow without bound
                cutoff = now - _cooldown_hours(entry) * 3600
                entry["fired_fingerprints"] = {k: v for k, v in ff.items() if v >= cutoff}

        state_store.update_trigger(trigger_id, _mut)
        state_store.audit("trigger_fired", trigger=trigger_id,
                          name=trigger.get("name"), kind=trigger.get("kind"),
                          task=task_id, fingerprint=fp or None)
        return task_id

    async def tick(self, now: float | None = None) -> list[str]:
        """One evaluation pass: fire every due schedule trigger and every tripped
        error_spike trigger, honoring the master switch and the daily cap. Returns the
        task_ids fired (for tests/telemetry). Called on a timer AND directly in tests."""
        now = time.time() if now is None else now
        if not self.enabled():
            return []
        triggers = state_store.read_triggers()
        fired: list[str] = []

        # Schedule triggers.
        for t in triggers:
            if not (t.get("enabled") and t.get("kind") == "schedule"):
                continue
            if self._rate_limited(state_store.read_triggers(), now):
                state_store.audit("trigger_skipped", trigger=t.get("id"), reason="daily cap reached")
                break
            if schedule_due(t, now):
                tid = self._fire(t, now)
                if tid:
                    fired.append(tid)

        # Error-spike triggers: one shared fetch of the active app's error groups.
        spike = [t for t in triggers if t.get("enabled") and t.get("kind") == "error_spike"]
        if spike:
            groups = None
            try:
                groups = await self._fetch_errors()
            except Exception:
                groups = None
            if groups is not None:
                for t in spike:
                    # re-read for fresh per-fp bookkeeping / rate accounting after each fire
                    live = next((x for x in state_store.read_triggers() if x.get("id") == t.get("id")), t)
                    for g in spike_hits(live, groups, now):
                        if self._rate_limited(state_store.read_triggers(), now):
                            state_store.audit("trigger_skipped", trigger=t.get("id"),
                                              reason="daily cap reached")
                            break
                        tid = self._fire(live, now, error=g)
                        if tid:
                            fired.append(tid)
                        live = next((x for x in state_store.read_triggers()
                                     if x.get("id") == t.get("id")), live)

        # Advisor triggers: auto-file the app's open improvement proposals. One shared
        # fetch of the active app's auto-queue; ring 3 proposes, THIS decides when filing
        # is allowed (rails: daily cap, per-proposal cooldown, in-flight dedupe).
        adv = [t for t in triggers if t.get("enabled") and t.get("kind") == "advisor"]
        if adv and self._fetch_proposals is not None:
            try:
                proposals = await self._fetch_proposals()
            except Exception:
                proposals = None
            if proposals is not None:
                for t in adv:
                    live = next((x for x in state_store.read_triggers()
                                 if x.get("id") == t.get("id")), t)
                    for p in advisor_hits(live, proposals, now):
                        if self._rate_limited(state_store.read_triggers(), now):
                            state_store.audit("trigger_skipped", trigger=t.get("id"),
                                              reason="daily cap reached")
                            break
                        tid = self._fire(live, now, proposal=p)
                        if tid:
                            fired.append(tid)
                        live = next((x for x in state_store.read_triggers()
                                     if x.get("id") == t.get("id")), live)
        return fired

    def handle_webhook(self, trigger_id: str, body: bytes,
                       signature: str | None) -> tuple[bool, int, dict]:
        """Verify + fire a webhook trigger. Returns (ok, http_status, body). Fails closed on
        a bad/absent signature, a disabled trigger, the master switch, or the daily cap."""
        now = time.time()
        trigger = next((t for t in state_store.read_triggers()
                        if t.get("id") == trigger_id and t.get("kind") == "webhook"), None)
        if trigger is None:
            return False, 404, {"ok": False, "error": "unknown webhook"}
        if not verify_webhook(trigger.get("secret") or "", body, signature):
            state_store.audit("webhook_denied", trigger=trigger_id, reason="bad signature")
            return False, 401, {"ok": False, "error": "invalid signature"}
        if not self.enabled():
            return False, 403, {"ok": False, "error": "triggers are disabled (master switch off)"}
        if not trigger.get("enabled"):
            return False, 403, {"ok": False, "error": "this trigger is disabled"}
        if self._rate_limited(state_store.read_triggers(), now):
            state_store.audit("trigger_skipped", trigger=trigger_id, reason="daily cap reached")
            return False, 429, {"ok": False, "error": "daily trigger cap reached"}
        payload = body.decode("utf-8", "replace")[:_TEMPLATE_MAX]
        task_id = self._fire(trigger, now, payload=payload)
        return True, 200, {"ok": True, "task": task_id}


def _fingerprint_in_flight(fp: str) -> bool:
    """True if a queued task, or a pending version, already references this error fingerprint
    — so a self-heal trigger doesn't file a duplicate fix while one is in progress."""
    for item in state_store.read_queue():
        if fp in (item.get("prompt") or ""):
            return True
    for p in state_store.read_pending():
        if fp in (p.get("prompt") or ""):
            return True
    return False


# ── CRUD (used by the syscalls) ────────────────────────────────────────────────────────
def list_triggers(reveal_secrets: bool = False) -> list[dict]:
    """Triggers for display. Webhook secrets are redacted unless explicitly revealed (they
    are shown exactly once, at creation)."""
    out = []
    for t in state_store.read_triggers():
        row = dict(t)
        if not reveal_secrets and row.get("secret"):
            row["secret"] = None
            row["has_secret"] = True
        out.append(row)
    return out


def upsert_trigger(spec: dict) -> tuple[bool, str, dict | None]:
    """Create (no id) or update (existing id) a trigger. Returns (ok, error, entry). A newly
    created webhook trigger gets a fresh HMAC secret, returned ONCE in the entry."""
    ok, err = validate_trigger(spec)
    if not ok:
        return False, err, None
    triggers = state_store.read_triggers()
    tid = (spec.get("id") or "").strip()
    now = time.time()
    fields = {
        "name": spec["name"].strip(),
        "kind": spec["kind"],
        "enabled": bool(spec.get("enabled", True)),
        "config": spec.get("config") or {},
        "prompt_template": spec["prompt_template"],
    }
    if tid:
        entry = next((t for t in triggers if t.get("id") == tid), None)
        if entry is None:
            return False, f"unknown trigger {tid}", None
        if entry["kind"] != fields["kind"]:
            return False, "cannot change a trigger's kind — delete and recreate", None
        entry.update(fields)
    else:
        entry = {
            "id": "trg_" + uuid.uuid4().hex[:10], **fields,
            "created_at": now, "last_fired": None, "fires_today": 0, "fires_day": None,
            "last_result": None, "fired_fingerprints": {},
        }
        if fields["kind"] == "webhook":
            entry["secret"] = new_secret()  # returned once, in this response
        triggers.append(entry)
    state_store.write_triggers(triggers)
    state_store.audit("trigger_saved", trigger=entry["id"], name=entry["name"],
                      kind=entry["kind"], enabled=entry["enabled"])
    return True, "", entry


def delete_trigger(trigger_id: str) -> bool:
    triggers = state_store.read_triggers()
    kept = [t for t in triggers if t.get("id") != trigger_id]
    if len(kept) == len(triggers):
        return False
    state_store.write_triggers(kept)
    state_store.audit("trigger_deleted", trigger=trigger_id)
    return True


def set_trigger_enabled(trigger_id: str, enabled: bool) -> bool:
    entry = state_store.update_trigger(trigger_id, lambda e: e.update(enabled=bool(enabled)))
    if entry is not None:
        state_store.audit("trigger_toggled", trigger=trigger_id, enabled=bool(enabled))
    return entry is not None
