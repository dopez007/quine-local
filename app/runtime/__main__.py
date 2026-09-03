"""Worker entrypoint: `python -u -m runtime`. The kernel spawns this (keyless) to edit
the staging clone, then validates/commits/reboots what it proposes."""

from __future__ import annotations

from . import sdk
from .agent import run

if __name__ == "__main__":
    sdk.ready()  # tell the kernel we started OK (else it falls back to recovery)
    try:
        run()
    except Exception as exc:  # never crash silently — surface it in the live log
        sdk.step("worker_error", summary=str(exc))
