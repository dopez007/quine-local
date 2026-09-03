"""Bootloader: A/B slots + app process lifecycle (the "GRUB + init").

Launches a deployed app version as a child process on an ephemeral port, in a clean
environment that deliberately omits provider secrets. The kernel flips which child is
"active" to perform a blue-green reboot.

We use subprocess.Popen (not asyncio subprocesses) so process management does not
depend on the event-loop kind — important on Windows, where only the Proactor loop
supports asyncio subprocesses and uvicorn does not guarantee it.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import IO

from kernel import state_store, versioning
from kernel.util import CHILD_CREATIONFLAGS, free_port


@dataclass
class AppHandle:
    proc: subprocess.Popen
    port: int
    slot: str          # "a" or "b"
    version: str       # commit sha
    log_file: IO[bytes] | None = None  # captured child stdout/stderr (crash forensics)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close_log(self) -> None:
        """Release the slot-log handle (on Windows the next launch's truncating open of
        the same slot file needs it closed)."""
        if self.log_file is not None:
            with contextlib.suppress(Exception):
                self.log_file.close()
            self.log_file = None


def slot_dir(slot: str) -> pathlib.Path:
    return state_store.SLOTS_DIR / slot


def other_slot(slot: str | None) -> str:
    return "b" if slot == "a" else "a"


def _child_env(port: int, version: str = "") -> dict[str, str]:
    """Environment for the app child: data dir + syscall URL, but NO secrets.

    Strips provider/cloud credentials (those named in secrets.env AND a built-in denylist)
    so the mutable app can never inherit them — see state_store.stripped_env."""
    env = state_store.stripped_env()
    cfg = state_store.load_config()
    gw_host, gw_port = cfg["kernel"]["host"], cfg["kernel"]["port"]
    env["QUINE_DATA_DIR"] = str(state_store.DATA_DIR)
    env["QUINE_SYSCALL_URL"] = f"http://{gw_host}:{gw_port}/api/syscall"
    env["PORT"] = str(port)
    if version:
        # The app's exact version sha, so app-side records (e.g. the error tracker) can be
        # stamped correctly even while the candidate is still health-checking — the /status
        # syscall would still point at the OLD version during that window.
        env["QUINE_APP_VERSION"] = version
    # PYTHONPATH is set per-launch in start() once the slot dir is known.
    return env


async def start(version: str, slot: str, extra_env: dict[str, str] | None = None) -> AppHandle:
    """Deploy `version` into `slot` and launch it as a uvicorn child process.

    `extra_env` adds non-secret marker vars (e.g. QUINE_PREVIEW_NAME for preview slots) on
    top of the stripped child environment — never a way to smuggle secrets back in."""
    target = slot_dir(slot)
    versioning.deploy(version, target)

    port = free_port()
    env = _child_env(port, version)
    if extra_env:
        env.update({k: v for k, v in extra_env.items()
                    if k not in state_store.secret_env_names()})
    env["PYTHONPATH"] = str(target)

    cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
    ]
    # Capture the child's output per slot (truncated each launch): when a candidate dies
    # before its /health ever answers, this log is the only place its traceback exists —
    # the kernel tails it into the version's registry health record on failure.
    log_path = state_store.app_log_path(slot)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Bound state/logs/ growth (preview-slot logs accumulate; a long run can bloat one file).
    # Safe here: the active logs are newest so they stay within budget; stale ones are reclaimed.
    state_store.prune_logs()
    log_file: IO[bytes] | None = None
    try:
        log_file = log_path.open("wb")
    except OSError:
        log_file = None  # capture is forensics, never a launch blocker
    proc = subprocess.Popen(
        cmd, cwd=str(target), env=env,
        # DEVNULL fallback (never inherit): the child must not share the kernel's console.
        stdout=log_file if log_file is not None else subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        creationflags=CHILD_CREATIONFLAGS,
    )
    state_store.audit("app_started", slot=slot, version=version[:12], port=port, pid=proc.pid)
    return AppHandle(proc=proc, port=port, slot=slot, version=version, log_file=log_file)


async def stop(handle: AppHandle | None, timeout: float = 8.0) -> None:
    if handle is None:
        return
    if not handle.alive():
        handle.close_log()  # a crashed child still holds the slot log open on our side
        return

    def _terminate() -> None:
        handle.proc.terminate()
        try:
            handle.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
            handle.proc.wait()

    await asyncio.to_thread(_terminate)
    handle.close_log()
    state_store.audit("app_stopped", slot=handle.slot, version=handle.version[:12], pid=handle.pid)
