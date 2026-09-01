from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RequestPacer:
    interval_seconds: float
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _last_completion: float | None = field(default=None, init=False, repr=False)

    async def request(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            now = self.clock()
            if self._last_completion is not None:
                remaining = self.interval_seconds - (now - self._last_completion)
                if remaining > 0:
                    await self.sleep(remaining)
            try:
                return await operation()
            finally:
                self._last_completion = self.clock()
