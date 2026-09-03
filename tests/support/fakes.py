"""Small deterministic fakes shared by local and control-plane contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeClock:
    now_value: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.now_value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now_value += seconds


@dataclass
class CallRecorder:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    result: Any = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.result
