"""The ONLY module allowed to touch `state/`.

Everything the agent must not reach lives here: config, the A/B slot pointers, the
append-only audit log, and provider secrets. Nothing in `app/` (and none of the
agent's tools) is ever handed these paths — that capability boundary is the core of
the "protect only what matters" isolation model.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import uuid
from typing import Any

import yaml

from kernel import keycrypt

# ── Filesystem layout (anchored at the project root) ──────────────────────────────
ROOT = pathlib.Path(__file__).resolve().parents[1]

# The state/slots/data partitions normally live at the repo root. Setting QUINE_STATE_HOME
# relocates just those three (never APP_SEED — that's the source tree, not relocatable
# state), which lets several harnesses or isolated serial test runs use independent homes
# without sharing a versions.git / slots dir / config.yaml. Resolved once at import, so it
# must be set before this module is first imported (the test conftest does exactly that).
_STATE_HOME = pathlib.Path(os.environ["QUINE_STATE_HOME"]).resolve() if os.environ.get("QUINE_STATE_HOME") else ROOT

STATE_DIR = _STATE_HOME / "state"
SLOTS_DIR = _STATE_HOME / "slots"
DATA_DIR = _STATE_HOME / "data"   # user-data / home partition (app-owned, persistent)
# The v1 app source, imported into git on first boot. Normally ROOT/app; QUINE_APP_SEED
# overrides it so a candidate KERNEL imported from a staging tree (which has no app/) can
# still seed the app during kernel-update validation — see kernel/kernelmod.py.
APP_SEED = (pathlib.Path(os.environ["QUINE_APP_SEED"]).resolve()
            if os.environ.get("QUINE_APP_SEED") else ROOT / "app")

VERSIONS_GIT = STATE_DIR / "versions.git"
SLOTS_JSON = STATE_DIR / "slots.json"
AUDIT_LOG = STATE_DIR / "audit.log"
SECRETS_ENV = STATE_DIR / "secrets.env"
CONFIG_YAML = STATE_DIR / "config.yaml"
TASKS_DIR = STATE_DIR / "tasks"
LOGS_DIR = STATE_DIR / "logs"     # per-slot app child stdout/stderr (crash forensics)

# Gated Kernel Self-Update (feature #4): a SEPARATE kernel version store (the app pipeline's
# versions.git only holds `app/`). The firmware — not the kernel — is the sole writer of the
# on-disk kernel and the promote/rollback authority; these files are the protected handshake
# between the kernel (proposes/approves) and the firmware (verifies/swaps/health-gates).
KERNEL_VERSIONS_GIT = STATE_DIR / "kernel.git"        # bare repo of kernel-tree versions
KERNEL_VERSIONS_JSON = STATE_DIR / "kernel_versions.json"  # per-version metadata/status
ACTIVE_KERNEL_JSON = STATE_DIR / "active_kernel.json"      # the kernel version that SHOULD run
ACTIVE_KERNEL_PREV_JSON = STATE_DIR / "active_kernel_prev.json"  # firmware/operator rollback target
PENDING_KERNEL_JSON = STATE_DIR / "pending_kernel.json"   # committed candidate awaiting approval
KERNEL_BOOT_RESULT_JSON = STATE_DIR / "kernel_boot_result.json"  # firmware→kernel swap-outcome breadcrumb

DEFAULT_CONFIG: dict[str, Any] = {
    "kernel": {"host": "127.0.0.1", "port": 8000},
    "app": {"host": "127.0.0.1", "health_path": "/health"},
    "agent": {
        # Model id is a LiteLLM string; swap providers freely. The default is a real
        # provider (DeepSeek) so a fresh install can chat / self-modify out of the box —
        # just set DEEPSEEK_API_KEY in state/secrets.env. The keyless offline "scripted"
        # engine is still a preset below (and is what the test suite pins explicitly).
        "engine": "litellm",
        "model": "deepseek/deepseek-v4-flash",
        "max_steps": 40,
        "temperature": 0.0,
        # Optional tools the self-mod agent may use; read_file/write_file/
        # propose_commit are always on. Toggle these via the config syscall.
        "tools_enabled": ["list_dir", "run_shell", "run_tests"],
        # Governance: when true, a self-mod commits but is HELD for approval instead of
        # promoting — review its diff first, then approve/reject (see kernel.core).
        "require_approval": False,
        # Optional review agent: when true, after the agent's edits validate, a reviewer LLM
        # inspects the staged diff and feeds any problems back for the agent to fix before the
        # commit. review_model "" falls back to the model above. No effect on the scripted engine.
        "review_enabled": False,
        "review_model": "",
    },
    # Selectable agent presets (edit this list freely). The UI offers them as a picker for
    # BOTH the Run-tab chat and the self-mod agent; `agent` above is the active self-mod
    # selection. Each: name (label), model (LiteLLM id), engine (litellm|scripted).
    "agents": [
        {"name": "DeepSeek Flash", "model": "deepseek/deepseek-v4-flash", "engine": "litellm"},
        {"name": "Claude Opus 4.8", "model": "anthropic/claude-opus-4-8", "engine": "litellm"},
        {"name": "Claude Sonnet 4.6", "model": "anthropic/claude-sonnet-4-6", "engine": "litellm"},
        {"name": "GPT-4o", "model": "gpt-4o", "engine": "litellm"},
        {"name": "Scripted (offline)", "model": "scripted", "engine": "scripted"},
    ],
    "watchdog": {
        "health_timeout_seconds": 30,
        "health_poll_interval": 0.5,
        # Continuous liveness monitor of the LIVE app (opt-in): after monitor_failures
        # consecutive failed polls (a dead process trips instantly), the kernel
        # auto-rolls-back to the previous promoted version through the health gate.
        "monitor_enabled": False,
        "monitor_interval": 10,
        "monitor_failures": 3,
    },
    # The Verification Gate (opt-in): a ring-0 verifier derives executable acceptance
    # checks from each change request; a candidate must pass them — plus every frozen
    # regression check in state/checks.json — before promotion, not just boot healthy.
    # `strict` fail-closes check DERIVATION too (default: a derivation error stamps the
    # version "unverified" and proceeds; a failing CHECK always blocks promotion).
    "verifier": {
        "enabled": False,
        "model": "",             # "" => use agent.model
        "strict": False,
        "max_checks": 3,         # cap on derived acceptance checks per change
        "timeout_seconds": 120,  # overall verification deadline per candidate
    },
    # Preview environments: extra running copies of any version (slots/p_*), addressable
    # through the gateway (/preview/<name>, cookie-routed). Bounded + idle-reaped so
    # forgotten experiments can't pile up into resource exhaustion.
    "previews": {
        "max": 3,                # concurrent preview processes
        "idle_minutes": 120,     # stop a preview nobody has touched for this long
    },
    # Operator authorization (opt-in, OFF): require a kernel-held credential — the
    # operator key (state/operator_key) as an X-Operator-Key header, or an HttpOnly
    # session unlocked at the kernel-served /operator page — on promotion-authority and
    # autonomy-config syscalls (approve/reject, rollback/revert, config writes, trigger/
    # check/eval mutation). The app process never holds the key, so ring 3 can no longer
    # act as the operator; unattended change requests hold for approval (core._should_hold).
    "operator_auth": {
        "enabled": False,
        "session_hours": 12,   # browser-session lifetime after an /operator unlock
    },
    # Agent evals (opt-in, master-switch OFF): held-out benchmark tasks (state/evals.json)
    # that gate changes touching the agent's own brain. When a candidate's diff touches
    # `paths`, the CANDIDATE's runtime must run every enabled eval task on a throwaway
    # staging and pass the authoritative validation gate — the paper's "promote only if
    # the harness still performs on held-out tasks". A failed task always blocks
    # (status eval_failed); infra failures fail open unless strict.
    "evals": {
        "enabled": False,
        "strict": False,
        "paths": ["runtime/"],     # diff prefixes that trigger evals; [] = every change
        "timeout_seconds": 600,    # per-task worker deadline
    },
    # Autonomous triggers (opt-in, master-switch OFF): a kernel scheduler/event source that
    # dispatches self-mod tasks on a schedule, an HMAC'd webhook, or an error spike — the
    # flagship being self-healing (an errorlog spike auto-files a fix task). Every autonomous
    # change still flows through the full validate → verify pipeline, and by default HOLDS
    # for approval (see kernel.triggers / core._should_hold) regardless of agent.require_approval.
    "triggers": {
        "enabled": False,        # master kill switch — nothing fires while off
        "max_per_day": 5,        # cap on autonomous fires across all triggers per calendar day
        "auto_promote": False,   # full-auto: promote without a human — engages ONLY with verifier.enabled
    },
    # Gated Kernel Self-Update (feature #4, opt-in, master-switch OFF): let the agent author
    # changes to the KERNEL itself. Heavily gated — a candidate is validated stricter than an
    # app change, ALWAYS held for operator approval (never auto-promoted), and applied by the
    # immutable firmware which health-gates it and auto-rolls-back a kernel that won't boot.
    # In signed mode (KERNEL_INTEGRITY_PUBKEY set) promotion also needs an operator signature.
    "kernel_update": {
        "enabled": False,           # master switch — no kernel change can even be submitted while off
        "boot_health_seconds": 60,  # firmware health-gate window for a freshly-swapped kernel
    },
}

# Self-mod agent tool surface. Mandatory tools are always available; optional tools
# are toggleable via `agent.tools_enabled` (validated in update_config).
MANDATORY_TOOLS: tuple[str, ...] = ("read_file", "write_file", "edit_file", "propose_commit")
OPTIONAL_TOOLS: tuple[str, ...] = ("list_dir", "run_shell", "run_tests")

DEFAULT_SLOTS: dict[str, Any] = {
    "active_slot": None,
    "active_version": None,
    "previous_version": None,    # mirror of promotion_history[-1] (backward compat)
    "last_known_good": None,     # last version that booted healthy (auto-recovery)
    "promotion_history": [],     # versions departed by successive promotions, oldest→newest —
                                 # the undo stack `rollback()` walks backward through
}

PROMOTION_HISTORY_MAX = 50  # bound the undo stack (slots.json stays small)


def ensure_dirs() -> None:
    for path in (STATE_DIR, SLOTS_DIR, DATA_DIR, TASKS_DIR, LOGS_DIR,
                 SLOTS_DIR / "a", SLOTS_DIR / "b"):
        path.mkdir(parents=True, exist_ok=True)


def app_log_path(slot: str) -> pathlib.Path:
    """The captured stdout/stderr of the app child running in `slot` (one file per slot,
    truncated at each launch — it always holds the CURRENT/most recent run's output)."""
    return LOGS_DIR / f"slot-{slot}.log"


def read_log_tail(slot: str, max_bytes: int = 8192) -> str:
    """Tail of a slot's captured app output — the crash traceback when a candidate died
    before/while health-checking. Best-effort: missing/unreadable log ⇒ empty string."""
    try:
        path = app_log_path(slot)
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def prune_logs(max_total_bytes: int | None = None) -> int:
    """Bound state/logs/ growth by keeping the NEWEST log files within a total byte budget,
    deleting the oldest first. Preview-slot logs (slot-p_<name>-a|b.log) accumulate and are never
    otherwise reclaimed, and a single long run can grow its slot log without limit — this caps both.

    Called on the launch path (bootloader.start), so the ACTIVE logs are the newest and stay within
    budget untouched; only stale/preview logs are candidates, and a file another process still holds
    open just fails to unlink (suppressed) — never corrupts a live capture. Budget from
    QUINE_MAX_LOG_BYTES (default 50 MiB); <=0 disables. Returns bytes reclaimed."""
    if max_total_bytes is None:
        try:
            max_total_bytes = int(os.environ.get("QUINE_MAX_LOG_BYTES") or 50 * 1024 * 1024)
        except (TypeError, ValueError):
            max_total_bytes = 50 * 1024 * 1024
    if max_total_bytes <= 0 or not LOGS_DIR.exists():
        return 0
    files: list[tuple[pathlib.Path, int, float]] = []
    for p in LOGS_DIR.glob("*.log"):
        with contextlib.suppress(OSError):
            st = p.stat()
            files.append((p, st.st_size, st.st_mtime))
    total = sum(sz for _, sz, _ in files)
    freed = 0
    for p, sz, _ in sorted(files, key=lambda t: t[2]):  # oldest first
        if total <= max_total_bytes:
            break
        with contextlib.suppress(OSError):
            p.unlink()
            total -= sz
            freed += sz
    return freed


def atomic_write_text(path: pathlib.Path, text: str, encoding: str = "utf-8") -> None:
    """Crash-safe replacement for `path.write_text()`: write to a temp file in the SAME
    directory, flush + fsync, then `os.replace()` over the target (atomic on POSIX and Windows).

    A bare `write_text` truncates-then-writes, so a crash / full disk / `kill -9` mid-write can
    leave a partial file — fatal for boot-critical state like slots.json (the A/B pointer) or the
    durable queue, which are then unparseable JSON. This makes a torn write impossible: readers
    see either the old file or the complete new one, never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config() -> dict[str, Any]:
    """Load config, seeding a default file on first run. Always merged over defaults."""
    ensure_dirs()
    if not CONFIG_YAML.exists():
        atomic_write_text(CONFIG_YAML, yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False))
        return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    loaded = yaml.safe_load(CONFIG_YAML.read_text(encoding="utf-8")) or {}
    return _deep_merge(DEFAULT_CONFIG, loaded)


# ── Config mutation (the bounded, allow-listed surface behind the config syscall) ──
# This is the whole answer to "the agent can't edit kernel/, so how does it change
# temperature/tools/model?" — those are CONFIG the kernel reads, changed only through
# update_config(), which enforces an allowlist + value bounds. No kernel write needed.
def _enum(*allowed: Any):
    def check(v: Any):
        return (v in allowed, v, None if v in allowed else f"must be one of {list(allowed)}")
    return check


def _num(lo: float, hi: float, cast: Any):
    def check(v: Any):
        try:
            n = cast(v)
        except (TypeError, ValueError):
            return (False, None, "must be a number")
        if n < lo or n > hi:
            return (False, None, f"must be between {lo} and {hi}")
        return (True, n, None)
    return check


def _nonempty_str(v: Any):
    ok = isinstance(v, str) and bool(v.strip())
    return (ok, v, None if ok else "must be a non-empty string")


def _str(v: Any):
    """Any string, INCLUDING empty. Used where '' is a meaningful 'unset/fall back' value
    (e.g. agent.review_model empty => use agent.model)."""
    ok = isinstance(v, str)
    return (ok, v.strip() if ok else v, None if ok else "must be a string")


def _bool(v: Any):
    if isinstance(v, bool):
        return (True, v, None)
    if v in (0, 1, "true", "false", "True", "False"):
        return (True, v in (1, "true", "True"), None)
    return (False, None, "must be true or false")


def _tool_list(v: Any):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        return (False, None, "must be a list of tool names")
    unknown = [x for x in v if x not in OPTIONAL_TOOLS]
    if unknown:
        return (False, None, f"unknown/locked tools {unknown}; settable: {list(OPTIONAL_TOOLS)}")
    return (True, v, None)


def _agents_list(v: Any):
    """The selectable agent presets: a list of {name, model, engine}. Non-secret display config
    (just labels + LiteLLM model ids), so it's safe to edit from the Settings UI. Coerced to a
    clean, bounded shape; provider keys still live only in secrets.env, never here."""
    if not isinstance(v, list):
        return (False, None, "must be a list of agents")
    if len(v) > 50:
        return (False, None, "too many agents (max 50)")
    cleaned: list[dict] = []
    for item in v:
        if not isinstance(item, dict):
            return (False, None, "each agent must be an object with name/model/engine")
        name = str(item.get("name", "")).strip()
        model = str(item.get("model", "")).strip()
        engine = str(item.get("engine", "") or "litellm").strip()
        if not name or not model:
            return (False, None, "each agent needs a name and a model")
        if engine not in ("litellm", "scripted"):
            return (False, None, "agent engine must be 'litellm' or 'scripted'")
        cleaned.append({"name": name, "model": model, "engine": engine})
    return (True, cleaned, None)


# Dotted key -> validator. ONLY these are settable; anything else (kernel.*, secrets)
# is rejected. Keep this in lock-step with what the Settings UI exposes. A non-dotted key
# (e.g. "agents") sets a whole top-level section.
CONFIG_ALLOWLIST = {
    "agent.engine": _enum("scripted", "litellm"),
    "agent.model": _nonempty_str,
    "agent.temperature": _num(0.0, 2.0, float),
    "agent.max_steps": _num(1, 200, int),
    "agent.tools_enabled": _tool_list,
    "agent.require_approval": _bool,
    # Optional second-pass review of the staged self-mod diff before commit. When on, a
    # reviewer LLM inspects the diff; any findings are fed back to the same agent to fix.
    "agent.review_enabled": _bool,
    "agent.review_model": _str,  # "" => use agent.model
    "agents": _agents_list,
    "watchdog.health_timeout_seconds": _num(5, 600, int),
    "watchdog.monitor_enabled": _bool,
    "watchdog.monitor_interval": _num(2, 300, float),
    "watchdog.monitor_failures": _num(1, 20, int),
    # Verification Gate: behavioral acceptance + regression checks before promotion.
    "verifier.enabled": _bool,
    "verifier.model": _str,  # "" => use agent.model
    "verifier.strict": _bool,
    "verifier.max_checks": _num(1, 10, int),
    "verifier.timeout_seconds": _num(10, 600, int),
    # Operator authorization: enabling is open (raising security); DISABLING goes through
    # the gated POST /config itself, so only an operator can turn the gate back off.
    "operator_auth.enabled": _bool,
    "operator_auth.session_hours": _num(1, 168, int),
    # Agent evals: master switch + failure policy (task CRUD is its own syscall surface).
    "evals.enabled": _bool,
    "evals.strict": _bool,
    "evals.timeout_seconds": _num(30, 1800, int),
    # Preview environments: bounded concurrent previews + idle reaping.
    "previews.max": _num(1, 10, int),
    "previews.idle_minutes": _num(5, 1440, int),
    # Autonomous triggers: master switch, daily cap, full-auto opt-in.
    "triggers.enabled": _bool,
    "triggers.max_per_day": _num(1, 100, int),
    "triggers.auto_promote": _bool,
    # Gated Kernel Self-Update: master switch + firmware health-gate window.
    "kernel_update.enabled": _bool,
    "kernel_update.boot_health_seconds": _num(10, 600, int),
}


def public_config() -> dict[str, Any]:
    """Config for display/editing. Provider secrets never live here (they are in
    secrets.env / the process env), so the whole config is safe to return."""
    return load_config()


def update_config(patch: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    """Validate a {dotted_key: value} patch against CONFIG_ALLOWLIST and persist it.

    Returns (ok, errors, config). Nothing is written unless EVERY key validates, so a
    bad request can never leave config half-updated.
    """
    errors: list[str] = []
    coerced: dict[str, Any] = {}
    for key, val in (patch or {}).items():
        validator = CONFIG_ALLOWLIST.get(key)
        if validator is None:
            errors.append(f"{key}: not settable")
            continue
        ok, cval, err = validator(val)
        if ok:
            coerced[key] = cval
        else:
            errors.append(f"{key}: {err}")
    if errors:
        return (False, errors, public_config())
    cfg = load_config()
    for key, cval in coerced.items():
        section, sep, leaf = key.partition(".")
        if sep:  # dotted "section.leaf" → set the leaf within the section
            cfg.setdefault(section, {})[leaf] = cval
        else:    # bare top-level key (e.g. "agents") → set the whole section
            cfg[section] = cval
    atomic_write_text(CONFIG_YAML, yaml.safe_dump(cfg, sort_keys=False))
    return (True, [], cfg)


def read_slots() -> dict[str, Any]:
    if not SLOTS_JSON.exists():
        return dict(DEFAULT_SLOTS)
    return {**DEFAULT_SLOTS, **json.loads(SLOTS_JSON.read_text(encoding="utf-8"))}


def write_slots(slots: dict[str, Any]) -> None:
    # Atomic: slots.json is the A/B boot pointer — a torn write here can brick boot.
    atomic_write_text(SLOTS_JSON, json.dumps(slots, indent=2))


# The audit log is hash-chained: every record carries `prev` (the previous record's hash,
# or "genesis") and `h` = sha256 of its own canonical JSON (which includes `prev`). Editing,
# deleting, or inserting any line breaks every hash after it — verify_audit() pinpoints the
# first bad line. Records written before the chain existed are tolerated as a contiguous
# legacy prefix. This is tamper-EVIDENT, not tamper-proof (no signatures — same trust model
# as the rest of state/: protected from ring-3, not from the operator).
_AUDIT_LOCK = threading.Lock()
_audit_tail: str | None = None  # hash of the last written record; lazy-loaded from disk
_audit_size: int | None = None  # file size after our last write — detects the log being
                                # wiped/rotated/replaced underneath us so we re-anchor to
                                # the real tail instead of chaining to a phantom record


def _audit_record_hash(entry: dict[str, Any]) -> str:
    import hashlib
    canon = json.dumps({k: v for k, v in entry.items() if k != "h"},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _audit_tail_from_disk() -> str:
    if not AUDIT_LOG.exists():
        return "genesis"
    tail = "genesis"
    with AUDIT_LOG.open("r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                h = json.loads(ln).get("h")
            except ValueError:
                continue
            if h:
                tail = h
    return tail


def _audit_file_size() -> int:
    try:
        return AUDIT_LOG.stat().st_size
    except OSError:
        return 0


def audit(event: str, **fields: Any) -> dict[str, Any]:
    """Append one tamper-evident (hash-chained) JSONL record. Returns the written entry."""
    global _audit_tail, _audit_size
    ensure_dirs()
    with _AUDIT_LOCK:
        # Re-anchor to the on-disk tail on first write AND whenever the file changed size
        # behind our back (wiped by a fresh test session, rotated, another writer) — a
        # cached tail pointing at a record that is no longer in the file would otherwise
        # chain every subsequent record to a phantom parent.
        if _audit_tail is None or _audit_file_size() != _audit_size:
            _audit_tail = _audit_tail_from_disk()
        entry = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), "event": event,
                 **fields, "prev": _audit_tail}
        entry["h"] = _audit_record_hash(entry)
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        _audit_size = _audit_file_size()
        _audit_tail = entry["h"]
    return entry


def read_audit(limit: int = 200, offset: int = 0, event: str | None = None,
               since: str | None = None) -> list[dict[str, Any]]:
    """The audit tail, oldest→newest, with optional server-side filters: `event` (exact
    name), `since` (ISO-8601 lower bound — records are UTC ISO, so string compare works),
    and `offset` (skip the N NEWEST matches — the "load older" pagination cursor)."""
    if not AUDIT_LOG.exists():
        return []
    records: list[dict[str, Any]] = []
    for ln in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue  # a torn/corrupt line must not take the whole endpoint down
        if event and rec.get("event") != event:
            continue
        if since and str(rec.get("ts", "")) < since:
            continue
        records.append(rec)
    end = max(0, len(records) - max(0, offset))
    return records[max(0, end - max(1, limit)):end]


def verify_audit() -> dict[str, Any]:
    """Re-compute the whole hash chain. Legacy (pre-chain) records are allowed only as a
    contiguous prefix; the first record that breaks the chain is reported by line number."""
    if not AUDIT_LOG.exists():
        return {"ok": True, "checked": 0, "legacy_prefix": 0, "first_bad_line": None}
    checked = legacy = 0
    prev = "genesis"
    in_legacy_prefix = True
    with AUDIT_LOG.open("r", encoding="utf-8") as fh:
        for i, ln in enumerate(fh, start=1):
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except ValueError:
                return {"ok": False, "checked": checked, "legacy_prefix": legacy,
                        "first_bad_line": i, "error": "unparseable record"}
            if "h" not in rec:
                if in_legacy_prefix:
                    legacy += 1
                    continue
                return {"ok": False, "checked": checked, "legacy_prefix": legacy,
                        "first_bad_line": i, "error": "unhashed record after the chain started"}
            in_legacy_prefix = False
            if rec.get("prev") != prev or _audit_record_hash(rec) != rec["h"]:
                return {"ok": False, "checked": checked, "legacy_prefix": legacy,
                        "first_bad_line": i, "error": "hash chain broken"}
            prev = rec["h"]
            checked += 1
    return {"ok": True, "checked": checked, "legacy_prefix": legacy, "first_bad_line": None}


# ── Self-mod task persistence (durable progress for recovery after tab/app close) ──
# A self-mod task records, under state/tasks/<id>/, a status.json (lifecycle + result)
# and an events.jsonl (append-only progress). A tiny _current.json points at the latest
# task. Because this lives in protected state/, the UI can fully reconstruct a run after
# a reload — the in-memory event bus alone cannot (it has no history).
CURRENT_TASK_JSON = TASKS_DIR / "_current.json"


def _task_dir(task_id: str) -> pathlib.Path:
    d = TASKS_DIR / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_task_status(task_id: str, status: dict[str, Any]) -> None:
    atomic_write_text(_task_dir(task_id) / "status.json", json.dumps(status, indent=2))


def read_task_status(task_id: str) -> dict[str, Any] | None:
    p = TASKS_DIR / task_id / "status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def append_task_event(task_id: str, event: dict[str, Any]) -> None:
    with (_task_dir(task_id) / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def read_task_events(task_id: str, limit: int = 2000) -> list[dict[str, Any]]:
    p = TASKS_DIR / task_id / "events.jsonl"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").splitlines()
    events: list[dict[str, Any]] = []
    for ln in lines[-limit:]:
        if not ln.strip():
            continue
        try:  # appends aren't atomic — one torn line must not 500 the /task endpoint forever
            events.append(json.loads(ln))
        except ValueError:
            continue
    return events


# ── Durable self-mod queue (FIFO backlog that survives a kernel restart) ───────────
# A small on-disk FIFO so concurrent self-mod requests are not lost: the kernel enqueues
# here, a single drainer runs them one at a time, and the drainer resumes the backlog on
# boot. It lives UNDER TASKS_DIR so the test fixture (which wipes TASKS_DIR) keeps a clean
# slate per session.
QUEUE_JSON = TASKS_DIR / "_queue.json"

# The queue is a read-modify-write on one JSON file. Serialize enqueue/remove under a lock so two
# concurrent change_request handlers can't both read the old list and clobber each other's append
# (which would silently drop a queued task). Pair with atomic_write_text for crash safety.
_QUEUE_LOCK = threading.Lock()


def read_queue() -> list[dict[str, Any]]:
    if not QUEUE_JSON.exists():
        return []
    try:
        data = json.loads(QUEUE_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def _write_queue(items: list[dict[str, Any]]) -> None:
    atomic_write_text(QUEUE_JSON, json.dumps(items, indent=2))


def queue_enqueue(task_id: str, prompt: str, kind: str = "change",
                  payload: dict[str, Any] | None = None) -> None:
    """Append one task. `kind` distinguishes agent self-mods ("change") from operator
    git ops ("revert"/"reapply"); readers default a missing kind to "change" so an
    in-flight queue written by an older kernel still drains correctly."""
    with _QUEUE_LOCK:
        items = read_queue()
        items.append({
            "task_id": task_id, "prompt": prompt, "kind": kind,
            "payload": payload or {},
            "enqueued_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })
        _write_queue(items)


def queue_peek() -> dict[str, Any] | None:
    items = read_queue()
    return items[0] if items else None


def queue_remove(task_id: str) -> None:
    with _QUEUE_LOCK:
        _write_queue([it for it in read_queue() if it.get("task_id") != task_id])


# ── version registry storage (see kernel/registry.py for the logic) ───────────────────
# One JSON index of per-version metadata. Git remains the source of truth for trees;
# a lost/corrupt registry is rebuilt from git at boot, so readers must never crash on it.
REGISTRY_JSON = STATE_DIR / "versions_meta.json"


def read_registry() -> dict[str, Any]:
    empty: dict[str, Any] = {"next_seq": 1, "versions": {}}
    if not REGISTRY_JSON.exists():
        return empty
    try:
        data = json.loads(REGISTRY_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("versions"), dict):
        return empty
    data.setdefault("next_seq", 1)
    return data


def write_registry(reg: dict[str, Any]) -> None:
    atomic_write_text(REGISTRY_JSON, json.dumps(reg, indent=2))


# ── verification check store (see kernel/checks.py for the logic) ─────────────────────
# The frozen regression suite of the Verification Gate: acceptance checks that passed at
# promotion, which every FUTURE candidate must also pass. Lives in protected state/ so the
# self-modifying agent can never weaken its own grader (same trust model as the registry).
CHECKS_JSON = STATE_DIR / "checks.json"


def read_check_store() -> list[dict[str, Any]]:
    if not CHECKS_JSON.exists():
        return []
    try:
        data = json.loads(CHECKS_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    checks = data.get("checks") if isinstance(data, dict) else None
    return checks if isinstance(checks, list) else []


def write_check_store(checks: list[dict[str, Any]]) -> None:
    atomic_write_text(CHECKS_JSON, json.dumps({"checks": checks}, indent=2))


# ── the operator key (state/operator_key — protected state; NEVER in the app's env, so
# ring 3 can't present it; see kernel/opauth.py for the verification half) ─────────────
OPERATOR_KEY_FILE = STATE_DIR / "operator_key"


def ensure_operator_key() -> str:
    """Read the operator key, minting it on first use. The file lives in protected
    state/ — the path-policy/capability model already keeps it out of the agent's reach."""
    try:
        existing = OPERATOR_KEY_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    key = secrets.token_hex(32)
    OPERATOR_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(OPERATOR_KEY_FILE, key)
    audit("operator_key_created")
    return key


# ── the agent-eval benchmark store (state/evals.json — protected: the agent must never
# be able to water down the very benchmark that gates changes to its own runtime) ─────
EVALS_JSON = STATE_DIR / "evals.json"


def read_eval_store() -> list[dict[str, Any]]:
    if not EVALS_JSON.exists():
        return []
    try:
        data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return tasks if isinstance(tasks, list) else []


def write_eval_store(tasks: list[dict[str, Any]]) -> None:
    atomic_write_text(EVALS_JSON, json.dumps({"tasks": tasks}, indent=2))


# ── named line metadata (see the line machinery in kernel/core.py) ────────────────────
# A line's TRUTH is its git ref (refs/heads/line_<name> in versions.git); this file only
# carries the human metadata git can't (creation provenance, description). Losing it is
# cosmetic — the refs, and therefore the lines, survive.
LINES_JSON = STATE_DIR / "lines.json"


def read_lines() -> list[dict[str, Any]]:
    if not LINES_JSON.exists():
        return []
    try:
        data = json.loads(LINES_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    lines = data.get("lines") if isinstance(data, dict) else None
    return lines if isinstance(lines, list) else []


def write_lines(lines: list[dict[str, Any]]) -> None:
    atomic_write_text(LINES_JSON, json.dumps({"lines": lines}, indent=2))


# ── kernel version store (Gated Kernel Self-Update — see kernel/kernelmod.py) ─────────
# The metadata index + the firmware handshake files. Git (kernel.git) is the source of
# truth for kernel trees; these small JSON files carry status + the active/pending pointers.
def read_kernel_versions() -> dict[str, Any]:
    empty: dict[str, Any] = {"next_seq": 1, "versions": {}}
    if not KERNEL_VERSIONS_JSON.exists():
        return empty
    try:
        data = json.loads(KERNEL_VERSIONS_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("versions"), dict):
        return empty
    data.setdefault("next_seq", 1)
    return data


def write_kernel_versions(reg: dict[str, Any]) -> None:
    atomic_write_text(KERNEL_VERSIONS_JSON, json.dumps(reg, indent=2))


def read_active_kernel() -> dict[str, Any] | None:
    try:
        return json.loads(ACTIVE_KERNEL_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def write_active_kernel(entry: dict[str, Any] | None) -> None:
    """Set (or clear, with None) the kernel version the firmware should run. Setting it
    stashes the current value as the rollback target first."""
    cur = read_active_kernel()
    if cur is not None:
        atomic_write_text(ACTIVE_KERNEL_PREV_JSON, json.dumps(cur, indent=2))
    if entry is None:
        with contextlib.suppress(OSError):
            ACTIVE_KERNEL_JSON.unlink()
    else:
        atomic_write_text(ACTIVE_KERNEL_JSON, json.dumps(entry, indent=2))


def read_prev_active_kernel() -> dict[str, Any] | None:
    try:
        return json.loads(ACTIVE_KERNEL_PREV_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def read_pending_kernel() -> dict[str, Any] | None:
    try:
        return json.loads(PENDING_KERNEL_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def write_pending_kernel(entry: dict[str, Any] | None) -> None:
    if entry is None:
        with contextlib.suppress(OSError):
            PENDING_KERNEL_JSON.unlink()
    else:
        atomic_write_text(PENDING_KERNEL_JSON, json.dumps(entry, indent=2))


def take_kernel_boot_result() -> dict[str, Any] | None:
    """Read + CLEAR the firmware's swap-outcome breadcrumb (kernel reconciles it into audit
    + kernel_versions status at boot). One-shot: returns None once consumed."""
    try:
        data = json.loads(KERNEL_BOOT_RESULT_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    with contextlib.suppress(OSError):
        KERNEL_BOOT_RESULT_JSON.unlink()
    return data if isinstance(data, dict) else None


# ── autonomous trigger definitions (see kernel/triggers.py for the logic) ─────────────
# The trigger registry: schedule/webhook/error_spike definitions + their firing bookkeeping
# (last_fired, per-day counts, per-fingerprint cooldowns). Protected state/ — the mutable
# app and the self-mod agent can never grant themselves autonomy the operator didn't set up.
# Serialized like the registry: ticks and CRUD both read-modify-write this one file.
TRIGGERS_JSON = STATE_DIR / "triggers.json"
_TRIGGERS_LOCK = threading.Lock()


def read_triggers() -> list[dict[str, Any]]:
    if not TRIGGERS_JSON.exists():
        return []
    try:
        data = json.loads(TRIGGERS_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    trigs = data.get("triggers") if isinstance(data, dict) else None
    return trigs if isinstance(trigs, list) else []


def write_triggers(triggers: list[dict[str, Any]]) -> None:
    atomic_write_text(TRIGGERS_JSON, json.dumps({"triggers": triggers}, indent=2))


def update_trigger(trigger_id: str, mutate: Any) -> dict[str, Any] | None:
    """Locked read-modify-write of ONE trigger: `mutate(entry)` edits it in place. Returns
    the updated entry (or None if unknown). Keeps tick bookkeeping race-free against CRUD."""
    with _TRIGGERS_LOCK:
        triggers = read_triggers()
        entry = next((t for t in triggers if t.get("id") == trigger_id), None)
        if entry is None:
            return None
        mutate(entry)
        write_triggers(triggers)
        return entry


# ── pending promotions (governance: committed but awaiting approval) ──────────────────
PENDING_JSON = STATE_DIR / "pending_promotions.json"


def read_pending() -> list[dict[str, Any]]:
    if not PENDING_JSON.exists():
        return []
    try:
        data = json.loads(PENDING_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def _write_pending(items: list[dict[str, Any]]) -> None:
    atomic_write_text(PENDING_JSON, json.dumps(items, indent=2))


def add_pending(entry: dict[str, Any]) -> None:
    items = [p for p in read_pending() if p.get("sha") != entry.get("sha")]
    items.append(entry)
    _write_pending(items)


def remove_pending(sha: str) -> bool:
    items = read_pending()
    kept = [p for p in items if p.get("sha") != sha]
    if len(kept) == len(items):
        return False
    _write_pending(kept)
    return True


def set_current_task(task_id: str) -> None:
    atomic_write_text(CURRENT_TASK_JSON, json.dumps({"task_id": task_id}))


def current_task_id() -> str | None:
    if not CURRENT_TASK_JSON.exists():
        return None
    try:
        return json.loads(CURRENT_TASK_JSON.read_text(encoding="utf-8")).get("task_id")
    except (ValueError, OSError):
        return None


# ── Hardened mode + secret-at-rest encryption ──────────────────────────────────────────
# Hardened mode (set QUINE_KERNEL_HARDENED=1 in the container/VPS) turns on two extra
# protections beyond env-stripping, both aimed at an agent that shares the kernel's OS user:
#   1. secrets.env is encrypted at rest (Fernet, key = QUINE_SECRET_KEY) — a disk read yields
#      ciphertext (see kernel.keycrypt);
#   2. the kernel marks itself non-dumpable so a same-UID child can't read its /proc environ.
# A fail-closed boot self-check (enforce_secret_hardening) proves both before the kernel serves.
HARDENED_ENV = "QUINE_KERNEL_HARDENED"

# secrets.env, when encrypted, is a marker line followed by one Fernet token. The marker lets
# load_secrets/secret_keys transparently handle BOTH forms, so plaintext files (local dev, and
# any deployment without QUINE_SECRET_KEY) keep working unchanged.
_ENC_MARKER = "#QUINE-SECRETS-ENC-V1"


def hardened() -> bool:
    """True when QUINE_KERNEL_HARDENED enables at-rest encryption + the fail-closed self-check."""
    return os.environ.get(HARDENED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _is_encrypted_blob(text: str) -> bool:
    return text.lstrip().startswith(_ENC_MARKER)


def _read_secrets_text() -> str:
    """The plaintext KEY=VALUE body of secrets.env, decrypting it if stored encrypted.

    Returns "" when the file is absent, or when an encrypted blob can't be decrypted (wrong /
    missing QUINE_SECRET_KEY) — the latter is surfaced loudly and, in hardened mode, caught by
    the boot self-check rather than silently serving keyless."""
    if not SECRETS_ENV.exists():
        return ""
    raw = SECRETS_ENV.read_text(encoding="utf-8")
    if not _is_encrypted_blob(raw):
        return raw  # plaintext (legacy / unhardened) — behavior unchanged
    token = raw.split("\n", 1)[1].strip() if "\n" in raw else ""
    body = keycrypt.decrypt(token) if token else None
    if body is None:
        print("[kernel] ERROR: secrets.env is encrypted but could not be decrypted "
              "(QUINE_SECRET_KEY missing or wrong)", flush=True)
        return ""
    return body


def _write_secrets_text(body: str) -> None:
    """Persist the KEY=VALUE body, encrypting it when a QUINE_SECRET_KEY is configured. Without
    a key it stays plaintext, so nothing changes for local dev / the test suite."""
    if keycrypt.configured():
        atomic_write_text(SECRETS_ENV, f"{_ENC_MARKER}\n{keycrypt.encrypt(body)}\n")
    else:
        atomic_write_text(SECRETS_ENV, body)


def load_secrets() -> None:
    """Parse `state/secrets.env` (KEY=VALUE) into the kernel process environment.

    Only the kernel calls this; the keys are used by the agent runtime and the
    `llm_call` primitive. They are never passed down to app subprocesses. The file may be
    encrypted at rest (hardened mode) — it is transparently decrypted here.
    """
    for raw in _read_secrets_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def secret_keys() -> set[str]:
    """The env-var names defined in secrets.env. The bootloader strips these from
    app subprocess environments so the mutable app never inherits provider keys."""
    keys: set[str] = set()
    for raw in _read_secrets_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


# Provider / cloud credentials that must NEVER reach ring-3 (the mutable app, the keyless
# self-mod worker, or any subprocess that runs agent-authored code), even when they are NOT
# listed in secrets.env. Defense in depth: an operator can inject a key straight into the
# kernel's own environment (LiteLLM and most provider SDKs read these by convention), which
# `secret_keys()` — being file-derived — would not know to strip. Without this denylist such
# a key would silently be inherited by the app/worker, defeating the whole isolation model.
WELL_KNOWN_SECRET_ENV: frozenset[str] = frozenset({
    # LLM / inference providers (LiteLLM conventions).
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_ORGANIZATION", "OPENAI_ORG_ID",
    "AZURE_API_KEY", "AZURE_OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
    "TOGETHER_API_KEY", "TOGETHERAI_API_KEY", "COHERE_API_KEY", "CO_API_KEY",
    "ANYSCALE_API_KEY", "PERPLEXITY_API_KEY", "PERPLEXITYAI_API_KEY", "XAI_API_KEY",
    "FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY", "REPLICATE_API_KEY", "REPLICATE_API_TOKEN",
    "HUGGINGFACE_API_KEY", "HUGGINGFACE_TOKEN", "HF_TOKEN", "VOYAGE_API_KEY",
    "NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY", "AI21_API_KEY", "ALEPHALPHA_API_KEY",
    "BASETEN_API_KEY", "CEREBRAS_API_KEY", "DATABRICKS_API_KEY", "DATABRICKS_TOKEN",
    "WATSONX_API_KEY", "CLOUDFLARE_API_KEY", "PALM_API_KEY", "TAVILY_API_KEY",
    # Generic cloud / infrastructure credentials that have no business in ring-3.
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY",
    "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
    # The kernel's own at-rest master key: if ring-3 held it, the agent could decrypt
    # secrets.env itself, defeating the at-rest encryption. Never hand it down.
    keycrypt.SECRET_KEY_ENV,
})


def secret_env_names() -> set[str]:
    """Every env-var name to strip from an agent-facing subprocess: the names declared in
    secrets.env PLUS the built-in WELL_KNOWN_SECRET_ENV denylist (so a credential injected
    directly into the kernel env — not through secrets.env — still never leaks to ring-3)."""
    return secret_keys() | set(WELL_KNOWN_SECRET_ENV)


def stripped_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of `base` (default: the kernel's own os.environ) with every secret env var
    removed. This is the environment handed to ANY subprocess that runs agent-authored code
    — the app slot, the keyless self-mod worker, the pre-commit validation/build steps, and
    the kernel's own git invocations — so provider keys are not inherited by any of them.

    Note: env-stripping is necessary but not sufficient on its own — agent code runs as the
    same OS user as the kernel. On its own this leaves two disk/introspection paths to the keys
    (reading state/secrets.env, or reading the kernel's /proc environ). Hardened mode closes both
    (at-rest encryption + a non-dumpable kernel, verified fail-closed at boot — see hardened() /
    enforce_secret_hardening); the deployment container remains the outer boundary."""
    env = dict(os.environ if base is None else base)
    for key in secret_env_names():
        env.pop(key, None)
    return env


# ── First-boot self-provisioning ───────────────────────────────────────────────────
# Optional deployment environment variables let the kernel configure an empty state volume on first
# boot. Provider keys are written into state/secrets.env, where they remain kernel-only and are
# stripped from the app subprocess (see bootloader._child_env / secret_keys()). Unset means no
# change, so local development and the test suite are unaffected.
PROVISION_SECRETS_ENV = "KERNEL_PROVISION_SECRETS"  # "K=V" pairs, newline- or ;-separated
PROVISION_ENGINE_ENV = "KERNEL_PROVISION_ENGINE"    # "scripted" | "litellm"
PROVISION_MODEL_ENV = "KERNEL_PROVISION_MODEL"      # a LiteLLM model id
PROVISION_FORCE_ENV = "KERNEL_PROVISION_FORCE"      # "1" → overwrite existing secrets/config
#   (useful when an operator rotates a provider key without wiping the data volume; default unset
#    preserves the idempotent first-boot behavior).


def _parse_env_blob(blob: str) -> dict[str, str]:
    """Parse a KEY=VALUE blob (pairs separated by newlines or semicolons) into a dict."""
    pairs: dict[str, str] = {}
    for chunk in re.split(r"[\n;]+", blob):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#") or "=" not in chunk:
            continue
        key, _, val = chunk.partition("=")
        key = key.strip()
        if key:
            pairs[key] = val.strip().strip('"').strip("'")
    return pairs


def provision_from_env() -> None:
    """Idempotent, opt-in self-configuration from the environment on first boot.

    Must run BEFORE the first load_config() (which seeds config.yaml) — call it at the very
    top of the kernel entry point. Two effects:
      1. If KERNEL_PROVISION_SECRETS is set and state/secrets.env does NOT yet exist, write
         the parsed keys there (so they become managed secrets: kernel-only + stripped from
         the app). The raw blob is then POPPED from os.environ so it can never reach the app
         child either (it would otherwise be an unmanaged, un-stripped env var).
      2. If config.yaml does NOT yet exist, seed agent.engine / agent.model from
         KERNEL_PROVISION_ENGINE / _MODEL so the instance can use a configured model instead of
         the offline scripted default.
    Existing secrets.env / config.yaml are never overwritten (operator/agent edits win).
    """
    ensure_dirs()
    force = (os.environ.pop(PROVISION_FORCE_ENV, "") or "").strip().lower() in ("1", "true", "yes")
    blob = os.environ.pop(PROVISION_SECRETS_ENV, None)
    if blob and (force or not SECRETS_ENV.exists()):
        pairs = _parse_env_blob(blob)
        if pairs:
            _write_secrets_text("".join(f"{k}={v}\n" for k, v in pairs.items()))
            audit("provisioned_secrets", keys=sorted(pairs),  # names only — never values
                  encrypted=keycrypt.configured())

    engine = os.environ.get(PROVISION_ENGINE_ENV)
    model = (os.environ.get(PROVISION_MODEL_ENV) or "").strip()
    patch: dict[str, Any] = {}
    if engine in ("scripted", "litellm"):
        patch["engine"] = engine
    if model:
        patch["model"] = model
    if patch and not CONFIG_YAML.exists():
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        cfg["agent"].update(patch)
        atomic_write_text(CONFIG_YAML, yaml.safe_dump(cfg, sort_keys=False))
        audit("provisioned_config", **patch)
    elif patch and force:
        # Forced set/rotate: update agent.engine/model in the EXISTING config, preserving
        # everything else the operator/agent may have changed.
        try:
            cfg = yaml.safe_load(CONFIG_YAML.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg.setdefault("agent", {}).update(patch)
        atomic_write_text(CONFIG_YAML, yaml.safe_dump(cfg, sort_keys=False))
        audit("provisioned_config", **patch)


# ── Process hardening + fail-closed secret-isolation self-check ─────────────────────────
# These run at kernel start (kernel/__main__.py). Together with at-rest encryption they close
# the two ways a same-UID agent could still reach provider keys — reading secrets.env off disk,
# or reading the kernel's /proc environ — and PROVE it before serving. All gated on hardened();
# outside hardened mode they are opportunistic no-ops, so local dev and the test suite are
# unchanged. Real isolation ultimately rests on the deployment container, but this strengthens the
# in-container boundary (defense in depth) and self-verifies it at every boot.


def harden_process() -> None:
    """Best-effort: mark THIS (kernel) process non-dumpable on Linux, so a same-UID child cannot
    read its /proc/<pid>/{environ,mem} or ptrace it — closing the in-memory path to provider keys
    and QUINE_SECRET_KEY. Needs no privileges; a no-op on non-Linux. Call as early as possible."""
    if sys.platform != "linux":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_DUMPABLE = 4
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            print(f"[kernel] warning: prctl(PR_SET_DUMPABLE, 0) failed (errno {ctypes.get_errno()})",
                  flush=True)
    except Exception as exc:  # ctypes/libc unavailable — degrade, never crash boot on this
        print(f"[kernel] warning: could not set process non-dumpable: {exc}", flush=True)


def _is_undumpable() -> bool | None:
    """Whether THIS process is non-dumpable (Linux; via prctl PR_GET_DUMPABLE) — audit detail only.
    None off Linux or if the query fails."""
    if sys.platform != "linux":
        return None
    try:
        import ctypes

        PR_GET_DUMPABLE = 3
        return ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except Exception:
        return None


def migrate_secrets_at_rest() -> None:
    """If secrets.env is plaintext on disk and a QUINE_SECRET_KEY is configured, rewrite it
    encrypted in place. No-op if the file is absent, already encrypted, or no key is set."""
    if not (SECRETS_ENV.exists() and keycrypt.configured()):
        return
    if _is_encrypted_blob(SECRETS_ENV.read_text(encoding="utf-8")):
        return
    _write_secrets_text(SECRETS_ENV.read_text(encoding="utf-8"))
    audit("secrets_encrypted_at_rest")


def _agent_can_read_secret_env() -> str | None:
    """Spawn a stripped-env child (as an agent subprocess runs) and check whether it can read a
    provider key or QUINE_SECRET_KEY out of ANY same-UID process's /proc environ — the kernel, its
    firmware parent (pid 1), or its immediate parent. Non-dumpable makes those files root-owned, so
    a same-UID child must get EACCES for every secret-bearing one (the firmware carries the same env
    the container was launched with, so pid 1 must be checked too, not just the kernel).

    Returns a short "pid VARNAME" description of the FIRST leak found (boundary NOT holding), or None
    when clean. Linux-only; None elsewhere (no /proc → this vector doesn't exist). The child prints
    only pid + variable NAME, never a value, so nothing leaks through the probe itself."""
    if sys.platform != "linux":
        return None
    pids = sorted({1, os.getppid(), os.getpid()})
    names = sorted(secret_env_names() | {PROVISION_SECRETS_ENV})  # names only — never values
    probe = (
        "import sys\n"
        "pids = sys.argv[1].split(',')\n"
        "names = set(sys.argv[2].split(','))\n"
        "for pid in pids:\n"
        "    try:\n"
        "        data = open('/proc/%s/environ' % pid, 'rb').read()\n"
        "    except Exception:\n"
        "        continue\n"
        "    for entry in data.split(b'\\0'):\n"
        "        k, _, v = entry.partition(b'=')\n"
        "        if v and k.decode('latin1', 'replace') in names:\n"
        "            print('LEAK %s %s' % (pid, k.decode('latin1', 'replace')))\n"
        "            sys.exit(0)\n"
        "print('CLEAN')\n"
    )
    try:
        res = subprocess.run(
            [sys.executable, "-c", probe, ",".join(str(p) for p in pids), ",".join(names)],
            env=stripped_env(), capture_output=True, text=True, timeout=20,
        )
    except Exception as exc:  # probe couldn't run — inconclusive, don't fail closed on infra error
        print(f"[kernel] warning: /proc isolation probe could not run: {exc}", flush=True)
        return None
    out = res.stdout.strip()
    return out[len("LEAK"):].strip() if out.startswith("LEAK") else None


def enforce_secret_hardening() -> None:
    """Boot-time gate for hardened mode: encrypt secrets.env at rest and PROVE, fail-closed, that
    the agent's two remaining paths to the keys are shut (disk read of secrets.env; /proc read of
    the kernel's env). Call once at kernel start, AFTER harden_process() + provision_from_env().

    Outside hardened mode this only opportunistically encrypts (when a key is set) and never fails,
    so local dev and the offline test suite are unaffected."""
    if not hardened():
        if keycrypt.configured():  # operator set a key without hardened mode — honor it, never block
            migrate_secrets_at_rest()
        return

    problems: list[str] = []

    # Invariant 1 — provider keys are not readable in plaintext from disk.
    if SECRETS_ENV.exists():
        if not keycrypt.configured():
            problems.append(
                f"state/secrets.env exists but {keycrypt.SECRET_KEY_ENV} is unset — cannot "
                "encrypt provider keys at rest")
        else:
            migrate_secrets_at_rest()
            if not _is_encrypted_blob(SECRETS_ENV.read_text(encoding="utf-8")):
                problems.append("state/secrets.env is still plaintext after at-rest migration")

    # Invariant 2 — no same-UID process (kernel, firmware parent, pid 1) exposes provider keys or
    # the master key through its /proc environ to a stripped agent child.
    leak = _agent_can_read_secret_env()
    if leak is not None:
        problems.append(
            f"a same-UID agent child can read a secret from /proc environ ({leak}) — a process "
            "holding provider keys is not effectively non-dumpable")

    if problems:
        for p in problems:
            print(f"[kernel] SECURITY: hardened-mode self-check failed — {p}", flush=True)
        audit("secret_hardening_failed", problems=problems)
        # Fail closed: refuse to serve rather than run with keys reachable by the agent.
        raise SystemExit(1)

    audit("secret_hardening_ok",
          secrets_encrypted=SECRETS_ENV.exists(), undumpable=_is_undumpable())
