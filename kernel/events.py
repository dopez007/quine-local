"""A tiny in-memory pub/sub bus for live progress events.

The gateway exposes this over SSE so the UI can watch a self-modification happen in
real time. Because the bus lives in the kernel (not the app), the stream survives app
reboots — the browser's EventSource stays connected to the stable gateway throughout.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def register(self) -> asyncio.Queue:
        """Register a subscriber queue synchronously (so it's active before the SSE
        response sends its first byte — no early events are missed)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[dict]:
        queue = self.register()
        try:
            while True:
                yield await queue.get()
        finally:
            self.unregister(queue)


bus = EventBus()
