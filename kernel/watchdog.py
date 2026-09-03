"""Watchdog: POST (power-on self-test) for a freshly started app version, plus an
opt-in continuous monitor of the LIVE app.

After a reboot into a candidate, `wait_healthy` gives it a bounded window to become
healthy; if it crashes or never reports healthy, the kernel rolls back. That gate only
runs at boot — a version that turns unhealthy LATER is covered by `monitor` (config
`watchdog.monitor_enabled`, default off), which polls the active app and asks the kernel
to auto-roll-back after N consecutive failures.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx

from kernel.bootloader import AppHandle


async def wait_healthy(
    handle: AppHandle,
    health_path: str,
    timeout: float,
    interval: float,
) -> tuple[bool, str]:
    """Poll the candidate's /health until 200, the process dies, or we time out.

    Returns (ok, reason).
    """
    url = f"http://127.0.0.1:{handle.port}{health_path}"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout

    async with httpx.AsyncClient(timeout=5.0) as client:
        while loop.time() < deadline:
            if not handle.alive():
                return False, f"process exited with code {handle.proc.returncode}"
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return True, "healthy"
            except httpx.HTTPError:
                pass  # not up yet
            await asyncio.sleep(interval)

    if not handle.alive():
        return False, f"process exited with code {handle.proc.returncode}"
    return False, f"health check timed out after {timeout:.0f}s"


async def monitor(
    get_handle: Callable[[], AppHandle | None],
    health_path: str,
    interval: float,
    fail_threshold: int,
    on_unhealthy: Callable[[str], Awaitable[Any]],
) -> None:
    """Continuously poll the ACTIVE app; after `fail_threshold` consecutive failures
    (a dead process counts as an instant threshold hit), await `on_unhealthy(reason)`.

    The callback owns the response (the kernel rolls back through the normal
    health-gated reboot); the loop keeps running afterwards, re-armed at zero failures,
    and simply idles while there is no live handle (e.g. mid-reboot). Cancelled by the
    kernel on shutdown.
    """
    failures = 0
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            await asyncio.sleep(interval)
            handle = get_handle()
            if handle is None:
                failures = 0
                continue
            if not handle.alive():
                reason = f"process exited with code {handle.proc.returncode}"
                failures = 0
                await on_unhealthy(reason)
                continue
            try:
                resp = await client.get(f"http://127.0.0.1:{handle.port}{health_path}")
                ok = resp.status_code == 200
                reason = f"health returned {resp.status_code}"
            except httpx.HTTPError as exc:
                ok = False
                reason = f"health unreachable: {type(exc).__name__}"
            if ok:
                failures = 0
                continue
            failures += 1
            if failures >= fail_threshold:
                failures = 0
                await on_unhealthy(f"{reason} ({fail_threshold} consecutive failures)")
