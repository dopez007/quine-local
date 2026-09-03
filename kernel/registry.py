"""Version registry: the per-version metadata index (ring 0).

Git (`state/versions.git`) stays the source of truth for *trees*; this registry is the
queryable index of everything git can't record — monotonic version numbers (`v1`, `v2`, …),
task/prompt provenance, human labels, status history (committed → promoted/pending/rejected/
abandoned/…), and the revert/re-apply edges between versions. It is reconciled against git
facts at boot (`versioning.reconcile_registry`), so a lost or corrupt file is never fatal —
the index rebuilds itself.

Deliberately imports ONLY state_store: git facts are passed in by callers, so
versioning → registry never becomes an import cycle.
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
import time
from typing import Any

from kernel import state_store

# All mutations are read-modify-write on one JSON file; serialize them so two concurrent
# writers can't clobber each other's update (writes themselves are atomic via state_store).
_LOCK = threading.Lock()

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_SEQ_REF_RE = re.compile(r"^[vV](\d+)$")
_SHA_PREFIX_RE = re.compile(r"^[0-9a-f]{7,40}$")
_PROMPT_MAX = 300

# Status vocabulary (current `status` is always the LAST entry of `history`):
#   committed     recorded in git, never promoted (yet)
#   pending       held for approval (agent.require_approval)
#   promoted      passed the health gate; is/was on the active line
#   health_failed candidate rejected by the boot health gate
#   verify_failed healthy candidate rejected by the Verification Gate (see `verification`)
#   rejected      pending version discarded by the operator
#   rolled_back   was active, left by a rollback (tip of an abandoned range)
#   abandoned     left the active line when main moved past/behind it
#   restored      abandoned version re-legitimized by a roll-forward
#   reverted      a promoted revert of this version exists (see `reverted_by`)
#   reapplied     a promoted re-apply of this version exists (see `reapplied_by`)


def _now() -> float:
    return round(time.time(), 3)


def record_commit(
    sha: str,
    *,
    parent: str | None,
    message: str,
    task_id: str | None = None,
    prompt: str | None = None,
    origin: str = "self-mod",
    reverts: str | None = None,
    reapplies: str | None = None,
    created_at: float | None = None,
    status: str = "committed",
) -> dict[str, Any]:
    """Register a new version, assigning its monotonic seq number. Idempotent per sha
    (a re-record returns the existing entry untouched)."""
    with _LOCK:
        reg = state_store.read_registry()
        existing = reg["versions"].get(sha)
        if existing:
            return existing
        entry: dict[str, Any] = {
            "sha": sha,
            "short": sha[:8],
            "seq": int(reg.get("next_seq", 1)),
            "parent": parent,
            "task": task_id,
            "origin": origin,
            "prompt": (prompt or "")[:_PROMPT_MAX] or None,
            "message": message,
            "created_at": created_at if created_at is not None else _now(),
            "label": None,
            "reverts": reverts,
            "reapplies": reapplies,
            "reverted_by": None,
            "reapplied_by": None,
            "status": status,
            "history": [{"status": status, "t": _now()}],
            "health": None,
        }
        reg["next_seq"] = entry["seq"] + 1
        reg["versions"][sha] = entry
        state_store.write_registry(reg)
        return entry


def set_status(sha: str, status: str, **fields: Any) -> None:
    """Append a status transition to a version's history (no-op for unknown shas —
    git remains authoritative; the registry only indexes what it knows about)."""
    with _LOCK:
        reg = state_store.read_registry()
        entry = reg["versions"].get(sha)
        if entry is None:
            return
        entry["history"].append({"status": status, "t": _now(), **fields})
        entry["status"] = status
        by = fields.get("by")
        if status == "reverted" and by:
            entry["reverted_by"] = by
        if status == "reapplied" and by:
            entry["reapplied_by"] = by
        if "health" in fields:
            entry["health"] = fields["health"]
        # The Verification Gate's report (acceptance + regression checks, or an
        # "unverified" stamp) — mirrored to the entry like `health` so listings get it.
        if fields.get("verification") is not None:
            entry["verification"] = fields["verification"]
        # The agent-eval benchmark report (see kernel/evals.py) — same mirroring.
        if fields.get("evals") is not None:
            entry["evals"] = fields["evals"]
        state_store.write_registry(reg)


def set_label(sha: str, label: str | None) -> tuple[bool, str]:
    """Set (or clear, with None) a version's human-friendly label. Labels are resolvable
    identifiers, so they are validated and enforced unique."""
    with _LOCK:
        reg = state_store.read_registry()
        entry = reg["versions"].get(sha)
        if entry is None:
            return (False, f"unknown version {sha[:8]}")
        if label:
            if not _LABEL_RE.match(label):
                return (False, "label must be 1-64 chars (letters, digits, . _ - space), "
                               "starting with a letter or digit")
            clash = next((v for v in reg["versions"].values()
                          if v.get("label") == label and v["sha"] != sha), None)
            if clash:
                return (False, f"label already used by v{clash['seq']} ({clash['short']})")
            entry["label"] = label
        else:
            entry["label"] = None
        state_store.write_registry(reg)
        return (True, entry.get("label") or "")


def get(sha: str) -> dict[str, Any] | None:
    return state_store.read_registry()["versions"].get(sha)


def all_versions() -> dict[str, dict[str, Any]]:
    """sha → registry entry, for bulk enrichment (one read for a whole listing)."""
    return state_store.read_registry()["versions"]


def lookup(ref: str) -> str | None:
    """Resolve a human reference — full sha, unique sha prefix (≥7 chars), `v<seq>`,
    or a label — to a full sha. `v<seq>` takes precedence over a label spelled `v3`."""
    ref = (ref or "").strip()
    if not ref:
        return None
    versions = state_store.read_registry()["versions"]
    if ref in versions:
        return ref
    m = _SEQ_REF_RE.match(ref)
    if m:
        seq = int(m.group(1))
        hit = next((v for v in versions.values() if v.get("seq") == seq), None)
        if hit:
            return hit["sha"]
    lowered = ref.lower()
    if _SHA_PREFIX_RE.match(lowered):
        hits = [sha for sha in versions if sha.startswith(lowered)]
        if len(hits) == 1:
            return hits[0]
    hit = next((v for v in versions.values() if v.get("label") == ref), None)
    return hit["sha"] if hit else None


def _parse_iso_epoch(iso: str) -> float:
    try:
        return _dt.datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return _now()


def reconcile(commits: list[dict[str, Any]], main_set: set[str],
              pending: set[str]) -> dict[str, int]:
    """Repair the registry against git facts (the source of truth for what exists).

    `commits` must be OLDEST-FIRST so backfilled seq numbers follow history order.
    Three repairs: backfill entries git has but the registry lost; prune entries whose
    sha left git; and fix status drift a crash can leave (a commit that is an ancestor
    of main was necessarily promoted; a `promoted` one that is not, was abandoned).
    Idempotent — a second run is a no-op.
    """
    with _LOCK:
        reg = state_store.read_registry()
        versions: dict[str, dict[str, Any]] = reg["versions"]
        git_shas = {c["sha"] for c in commits}

        pruned = [sha for sha in list(versions) if sha not in git_shas]
        for sha in pruned:
            del versions[sha]

        added: list[str] = []
        for c in commits:
            sha = c["sha"]
            if sha in versions:
                continue
            status = ("promoted" if sha in main_set
                      else "pending" if sha in pending
                      else "abandoned")
            entry: dict[str, Any] = {
                "sha": sha,
                "short": sha[:8],
                "seq": int(reg.get("next_seq", 1)),
                "parent": c.get("parent"),
                "task": None,
                "origin": "backfill",
                "prompt": None,
                "message": c.get("message", ""),
                "created_at": _parse_iso_epoch(c.get("date", "")),
                "label": None,
                "reverts": None,
                "reapplies": None,
                "reverted_by": None,
                "reapplied_by": None,
                "status": status,
                "history": [{"status": status, "t": _now(), "note": "backfilled from git"}],
                "health": None,
            }
            reg["next_seq"] = entry["seq"] + 1
            versions[sha] = entry
            added.append(sha)

        updated: list[str] = []
        for sha, entry in versions.items():
            status = entry.get("status")
            if sha in main_set and status == "committed":
                fixed = "promoted"
            elif sha not in main_set and status in ("promoted", "restored"):
                fixed = "abandoned"
            else:
                continue
            entry["history"].append({"status": fixed, "t": _now(), "note": "reconciled from git"})
            entry["status"] = fixed
            updated.append(sha)

        if added or pruned or updated:
            state_store.write_registry(reg)
        return {"added": len(added), "pruned": len(pruned), "updated": len(updated)}
