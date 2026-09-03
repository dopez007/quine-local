"""Error tracker — the harness's "Sentry": a persistent, version-stamped record of
runtime errors that agents (and the user) can query and act on.

Records live under the data partition (DATA_DIR/errors/) so they survive reboots and
version switches, and are readable by the self-mod worker via the same QUINE_DATA_DIR.
`errors.jsonl` is append-only; resolution state lives beside it in `resolved.json` so
records are never rewritten. Occurrences are grouped Sentry-style by a stable
fingerprint (exception type + route + innermost app frame), so a repeating bug is one
group with a count, not a wall of duplicates.

Capture from anywhere — including code the self-mod agent writes later — with:

    from errorlog import capture
    try:
        ...
    except Exception as exc:
        capture(exc, source="my-feature", context={"detail": ...})

Capture must never raise: every write path here is defensive.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
import time
import traceback as tb_mod
import uuid

_HERE = pathlib.Path(__file__).resolve().parent
DATA_DIR = pathlib.Path(os.environ.get("QUINE_DATA_DIR") or (_HERE / ".data"))
ERRORS_DIR = DATA_DIR / "errors"
ERRORS_FILE = ERRORS_DIR / "errors.jsonl"
RESOLVED_FILE = ERRORS_DIR / "resolved.json"

APP_VERSION = os.environ.get("QUINE_APP_VERSION", "")

_COMPACT_AT = 2000   # compact errors.jsonl when it exceeds this many records…
_COMPACT_TO = 1000   # …keeping only the newest this many
_TRACEBACK_CAP = 20000
_LOCK = threading.Lock()


# ── capture ─────────────────────────────────────────────────────────────────────────
def _innermost_app_frame(exc: BaseException) -> str:
    """`file:function` of the deepest frame inside this app tree (fallback: the deepest
    frame anywhere). File basename + function — no line number — so the fingerprint
    survives unrelated edits shifting lines."""
    frames = tb_mod.extract_tb(exc.__traceback__)
    if not frames:
        return ""
    app_root = str(_HERE)
    chosen = frames[-1]
    for fr in reversed(frames):
        if fr.filename and fr.filename.startswith(app_root):
            chosen = fr
            break
    return f"{pathlib.Path(chosen.filename).name}:{chosen.name}"


def _fingerprint(exc_type: str, route: str, frame_or_msg: str) -> str:
    raw = f"{exc_type}|{route or ''}|{frame_or_msg or ''}"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:12]


def record(exc: BaseException | None, *, source: str, exc_type: str = "",
           message: str = "", traceback: str = "", route: str | None = None,
           method: str | None = None, context: dict | None = None) -> dict | None:
    """Append one error occurrence. Pass a live exception (preferred — type, message,
    traceback, and fingerprint frame are derived from it) or explicit fields (manual
    reports, e.g. frontend JS errors). Returns the record, or None if writing failed."""
    try:
        if exc is not None:
            exc_type = type(exc).__name__
            message = str(exc)
            traceback = "".join(tb_mod.format_exception(type(exc), exc, exc.__traceback__))
            frame = _innermost_app_frame(exc)
        else:
            exc_type = (exc_type or "Error").strip()[:200]
            message = (message or "").strip()
            frame = message[:80]
        entry = {
            "id": "e" + uuid.uuid4().hex[:10],
            "ts": time.time(),
            "version": APP_VERSION,
            "source": source,
            "fingerprint": _fingerprint(exc_type, route or "", frame),
            "exc_type": exc_type,
            "message": message[:4000],
            "traceback": (traceback or "")[:_TRACEBACK_CAP],
            "route": route,
            "method": method,
            "context": context or {},
        }
        with _LOCK:
            ERRORS_DIR.mkdir(parents=True, exist_ok=True)
            with ERRORS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _compact_locked()
        return entry
    except Exception:
        return None  # error logging must never raise


def capture(exc: BaseException, *, source: str = "app", route: str | None = None,
            method: str | None = None, context: dict | None = None) -> dict | None:
    """The one-liner for feature code (incl. agent-written features): record an
    exception with full traceback. Never raises."""
    return record(exc, source=source, route=route, method=method, context=context)


def _compact_locked() -> None:
    """Bound the store: keep only the newest _COMPACT_TO records once it exceeds
    _COMPACT_AT. Caller holds _LOCK."""
    try:
        lines = ERRORS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= _COMPACT_AT:
        return
    keep = [ln for ln in lines if ln.strip()][-_COMPACT_TO:]
    tmp = ERRORS_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
    os.replace(tmp, ERRORS_FILE)


# ── read / query ────────────────────────────────────────────────────────────────────
def _read_records() -> list[dict]:
    if not ERRORS_FILE.exists():
        return []
    out: list[dict] = []
    try:
        for ln in ERRORS_FILE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
                if isinstance(rec, dict):
                    out.append(rec)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _read_resolved() -> dict:
    if not RESOLVED_FILE.exists():
        return {}
    try:
        d = json.loads(RESOLVED_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_resolved(d: dict) -> None:
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    RESOLVED_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def list_groups(since: float | None = None, version: str | None = None,
                include_resolved: bool = False) -> list[dict]:
    """Occurrences grouped by fingerprint, newest-last-seen first. Filters: `since`
    (epoch lower bound on last_ts), `version` (exact or sha prefix), resolution state."""
    resolved = _read_resolved()
    groups: dict[str, dict] = {}
    for rec in _read_records():
        fp = rec.get("fingerprint") or ""
        g = groups.get(fp)
        if g is None:
            g = groups[fp] = {
                "fingerprint": fp, "count": 0,
                "first_ts": rec.get("ts"), "last_ts": rec.get("ts"),
                "exc_type": rec.get("exc_type"), "message": rec.get("message"),
                "source": rec.get("source"), "route": rec.get("route"),
                "versions": [],
                "last_traceback": "",
            }
        g["count"] += 1
        g["last_ts"] = rec.get("ts")
        g["exc_type"] = rec.get("exc_type")
        g["message"] = rec.get("message")
        g["source"] = rec.get("source")
        g["route"] = rec.get("route")
        # Keep the TAIL: a traceback's end holds the innermost frame + the actual error
        # (the head can be pages of framework/ExceptionGroup preamble).
        g["last_traceback"] = (rec.get("traceback") or "")[-4000:]
        v = rec.get("version") or ""
        if v and v not in g["versions"]:
            g["versions"] = (g["versions"] + [v])[-10:]
    out = []
    for fp, g in groups.items():
        res = resolved.get(fp)
        if not isinstance(res, dict):
            res = {}
        try:
            resolved_after_latest = bool(
                res and float(res.get("resolved_at") or 0) >= float(g["last_ts"] or 0)
            )
        except (TypeError, ValueError):
            resolved_after_latest = False
        g["resolved"] = resolved_after_latest
        if resolved_after_latest:
            g["resolved_at"] = res.get("resolved_at")
            g["resolved_note"] = res.get("note", "")
        if not include_resolved and g["resolved"]:
            continue
        if since is not None and (g["last_ts"] or 0) < since:
            continue
        if version and not any(v.startswith(version) for v in g["versions"]):
            continue
        out.append(g)
    out.sort(key=lambda g: g.get("last_ts") or 0, reverse=True)
    return out


def get_group(fingerprint: str, limit: int = 20) -> list[dict]:
    """The newest `limit` full occurrences (incl. tracebacks) of one group."""
    recs = [r for r in _read_records() if r.get("fingerprint") == fingerprint]
    return recs[-limit:][::-1]


def resolve(fingerprint: str, note: str = "") -> bool:
    """Mark a group resolved. Returns False if no such group exists."""
    if not any(r.get("fingerprint") == fingerprint for r in _read_records()):
        return False
    with _LOCK:
        d = _read_resolved()
        d[fingerprint] = {"resolved_at": time.time(), "note": (note or "")[:500]}
        _write_resolved(d)
    return True


def unresolve(fingerprint: str) -> bool:
    with _LOCK:
        d = _read_resolved()
        if fingerprint not in d:
            return False
        del d[fingerprint]
        _write_resolved(d)
    return True


def clear(boot_fingerprints: list[str] | None = None) -> None:
    """Drop app records and dismiss the boot failures currently visible in the UI."""
    with _LOCK:
        for p in (ERRORS_FILE, RESOLVED_FILE):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        if boot_fingerprints:
            now = time.time()
            _write_resolved({
                fp: {
                    "resolved_at": now,
                    "note": "cleared from Errors tab",
                    "dismissed": True,
                }
                for fp in dict.fromkeys(boot_fingerprints)
            })


def unresolved_summary(version: str | None = None) -> dict:
    """Compact counts for the agent's task-start notice and the UI badge:
    {"groups": N, "in_version": M} (M counts groups seen in `version`)."""
    groups = list_groups(include_resolved=False)
    in_version = 0
    if version:
        in_version = sum(1 for g in groups
                         if any(v.startswith(version) for v in g["versions"]))
    return {"groups": len(groups), "in_version": in_version}


# ── boot-failure merge (kernel-side records, surfaced via /api/syscall/versions) ─────
def boot_groups(versions: list[dict], limit: int = 10,
                include_resolved: bool = False) -> list[dict]:
    """Convert `health_failed` version entries (as returned by the /versions syscall)
    into pseudo-groups so boot crashes appear in the same unified error view. The
    kernel's registry is their source of truth; local resolution state only controls
    whether an existing boot failure is dismissed from the Errors tab."""
    resolved = _read_resolved()
    out = []
    for v in versions or []:
        if v.get("status") != "health_failed":
            continue
        health = v.get("health") or {}
        sha = v.get("version") or v.get("sha") or ""
        fingerprint = f"boot-{sha[:12]}"
        resolution = resolved.get(fingerprint)
        if not isinstance(resolution, dict):
            resolution = None
        if resolution and resolution.get("dismissed"):
            continue
        group = {
            "fingerprint": fingerprint,
            "count": 1,
            "first_ts": None, "last_ts": None,
            "exc_type": "BootFailure",
            "message": health.get("reason") or "version failed its boot health check",
            "source": "boot",
            "route": None,
            "versions": [sha] if sha else [],
            "seq": v.get("seq"),
            "last_traceback": (health.get("log_tail") or "")[-4000:],
            "resolved": bool(resolution),
        }
        if resolution:
            group["resolved_at"] = resolution.get("resolved_at")
            group["resolved_note"] = resolution.get("note", "")
        if group["resolved"] and not include_resolved:
            continue
        out.append(group)
        if len(out) >= limit:
            break
    return out
