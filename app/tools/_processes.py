"""Process helpers for app-layer tools.

These helpers only manage subprocesses started by this app process. They deliberately do not
inspect or kill arbitrary system processes.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys


def creation_flags() -> int:
    """Flags that make a subprocess tree separately terminable on Windows."""
    if sys.platform == "win32":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def popen_kwargs() -> dict:
    """Cross-platform kwargs for starting an owned process group/tree."""
    if sys.platform == "win32":
        return {"creationflags": creation_flags()}
    return {"start_new_session": True}


async def terminate_tree(pid: int, *, grace_seconds: float = 2.0) -> None:
    """Terminate an owned subprocess tree by root pid.

    Windows shell commands often spawn children (`npm`, `python`, dev servers). Killing only the
    shell leaves those children running, so use `taskkill /T` there. On POSIX, start_new_session
    gives us a process group to signal.
    """
    if pid <= 0:
        return
    if sys.platform == "win32":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    await asyncio.sleep(grace_seconds)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return

