"""Politeness controls and the one permitted timer.

An unsanctioned integration that generates abnormal load is the one that gets
noticed and blocked. Throttling here is a requirement, not an optimization.

The throttle is global rather than per-tool: the constraint is "requests we
send to Brightspace", and a per-tool limiter lets a fan-out tool blow past it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class Throttle:
    """Bounded concurrency + a minimum interval between request starts."""

    def __init__(self, max_concurrency: int = 4, min_interval_ms: int = 100) -> None:
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self._min_interval = max(0.0, min_interval_ms / 1000.0)
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    async def __aenter__(self) -> None:
        await self._sem.acquire()
        # If anything after acquire() raises -- most importantly CancelledError
        # during the pacing sleep -- __aenter__ never returns, so `async with`
        # never calls __aexit__ and the permit is lost forever. After
        # max_concurrency cancellations the semaphore is exhausted and every
        # later request deadlocks silently.
        try:
            async with self._lock:
                wait = self._min_interval - (time.monotonic() - self._last_start)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_start = time.monotonic()
        except BaseException:
            self._sem.release()
            raise

    async def __aexit__(self, *exc: object) -> None:
        self._sem.release()


async def with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    should_retry: Callable[[BaseException], bool],
    retry_after: Callable[[BaseException], float | None] | None = None,
    base_delay: float = 0.75,
) -> T:
    """Retry with jittered exponential backoff, honoring Retry-After."""
    last: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            last = exc
            if attempt == max_attempts - 1 or not should_retry(exc):
                raise
            delay = None
            if retry_after is not None:
                delay = retry_after(exc)
            if delay is None:
                delay = base_delay * (2**attempt) + random.uniform(0, 0.3)
            log.debug("retry %d/%d after %.2fs: %s", attempt + 1, max_attempts, delay, exc)
            await asyncio.sleep(delay)
    assert last is not None
    raise last


class Keepalive:
    """The only timer in the codebase, bounded on every side.

    Sends one liveness probe every `interval_minutes` -- no queries, no course
    data. Starts only after the first real tool call, stops after
    `idle_stop_minutes` with no activity. `interval_minutes <= 0` disables it
    entirely and nothing is ever scheduled.

    See docs/07-risks-and-policy.md: no polling for data, ever; one
    session-extension request during an active working session.
    """

    def __init__(
        self,
        ping: Callable[[], Awaitable[bool]],
        interval_minutes: int,
        idle_stop_minutes: int = 120,
    ) -> None:
        self._ping = ping
        self._interval = max(0, interval_minutes) * 60
        self._idle_stop = max(1, idle_stop_minutes) * 60
        self._task: asyncio.Task[None] | None = None
        self._last_activity = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self._interval > 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def note_activity(self) -> None:
        """Called on every tool invocation."""
        self._last_activity = time.monotonic()

    def start(self) -> None:
        if not self.enabled or self.running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop(), name="avenue-keepalive")
        log.info("keepalive started (every %ds)", self._interval)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                if time.monotonic() - self._last_activity > self._idle_stop:
                    log.info("keepalive stopping: idle")
                    return
                try:
                    if not await self._ping():
                        log.info("keepalive stopping: session no longer alive")
                        return
                except Exception as exc:  # noqa: BLE001 -- never crash the server
                    log.debug("keepalive ping failed: %s", exc)
                    return
        except asyncio.CancelledError:
            raise


class TTLCache:
    """Tiny TTL cache for stable GET responses.

    Deliberately not used for grades, submission status, or computed digests
    -- a stale grade is a bad answer, and caching a watermark diff would
    report changes that were already consumed.
    """

    def __init__(self, ttl_seconds: int = 300, max_entries: int = 256) -> None:
        self._ttl = max(0, ttl_seconds)
        self._max = max_entries
        self._data: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> object | None:
        if self._ttl <= 0:
            return None
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if time.monotonic() > expires:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: object) -> None:
        if self._ttl <= 0:
            return
        if len(self._data) >= self._max:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._data.clear()
