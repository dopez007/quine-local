"""Policy gates: keep self-modification inside the app sandbox.

Path containment ensures the agent's tools cannot escape the staging workspace (so the
protected `state/` tree is unreachable), and structural checks ensure a proposed
version still looks like a runnable app before it is committed.
"""

from __future__ import annotations

import pathlib


def resolve_within(staging: pathlib.Path, rel: str) -> pathlib.Path | None:
    """Resolve `rel` under `staging`; return None if it escapes (defense in depth)."""
    try:
        full = (staging / rel).resolve()
        full.relative_to(staging.resolve())
        return full
    except (ValueError, OSError):
        return None


def check_staging(staging: pathlib.Path) -> tuple[bool, list[str]]:
    """Structural sanity checks for a proposed version."""
    errors: list[str] = []
    if not (staging / "main.py").exists():
        errors.append("main.py is missing (the app must keep an entry point)")
    if not (staging / "app_manifest.json").exists():
        errors.append("app_manifest.json is missing")
    return (not errors, errors)
