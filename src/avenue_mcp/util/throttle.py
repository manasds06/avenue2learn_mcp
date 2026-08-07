"""One shared throttle wrapping every outbound request (docs/02, docs/05).

Global, not per-tool — the constraint is "requests we send to Avenue", and a
per-tool limiter lets a fan-out tool blow past it.

Valence does not publish a per-user rate limit for session-authenticated calls,
which is not the same as there being none. Politeness is a requirement here,
not an optimization.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class Throttle:
    """Concurrency cap + minimum inter-request interval + jittered backoff."""

    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        min_interval_ms: int = 100,
        max_attempts: int = 3,
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrency)
        self._min_interval = min_interval_ms / 1000.0
        self._max_attempts = max_attempts
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    async def _pace(self) -> None:
        """Hold the interval floor between request starts, across all callers."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._last_start + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._last_start = now

    async def run(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run one request under the concurrency cap and interval floor."""
        async with self._sem:
            await self._pace()
            return await fn()

    def backoff_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Jittered exponential backoff, honouring Retry-After when present.

        `attempt` is 1-based: the delay to wait *after* the attempt-th failure.
        """
        if retry_after is not None and retry_after >= 0:
            return retry_after
        return 2.0 ** (attempt - 1) + random.uniform(0, 0.5)

    @property
    def max_attempts(self) -> int:
        return self._max_attempts
