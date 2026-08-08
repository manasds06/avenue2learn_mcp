"""Keepalive off must mean genuinely zero background traffic.

docs/06 Phase 4 exit criterion: "With `AVENUE_MCP_KEEPALIVE_MINUTES=0`, **zero**
background requests are made". docs/07 states this as a property of the software
-- no polling for data, ever -- and a documented promise nobody verified is just
a hope. The default is `0`, so this is the path essentially every user runs.

`Keepalive` is the only timer in the codebase, which is what makes it worth
pinning: it is the single place a background request could come from.

The counting transport deliberately serves the liveness route for real rather
than stubbing `is_alive`, so that a keepalive ping would actually reach the
counter -- a stub would make it invisible and the tests would pass regardless of
what the timer did.

**What carries the weight here, stated precisely.** An idle-window counter check
cannot outlast a real 60-second interval, so by itself it would pass with
keepalive *enabled*; it is a smoke test, not a proof. The load-bearing
assertions are that the off switch keeps `start()` from creating a task at all,
plus `test_a_ping_would_be_visible_to_this_counter`, which shortens the interval
so a ping lands inside the window and proves the counter is not simply blind.

Known limit: `_keepalive_tasks()` matches the literal task name, and the counter
is attached to the auth client, so a *new* background source -- an unnamed task,
or the one-off embedding-model download in rag/embed.py -- would not be caught
here. `Keepalive` being the only `create_task` in src/ is what makes that
acceptable today; it is not guaranteed by these tests.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from avenue_mcp.context import AppContext
from avenue_mcp.util.throttle import Keepalive

TASK_NAME = "avenue-keepalive"


class _Counter:
    """Counts every request that reaches the transport."""

    def __init__(self) -> None:
        self.count = 0
        self.paths: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.count += 1
            self.paths.append(request.url.path)
            return httpx.Response(
                200,
                json={"Identifier": "42"},
                headers={"Content-Type": "application/json"},
            )

        return httpx.MockTransport(handler)


def _keepalive_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name() == TASK_NAME]


@pytest.fixture
def ctx_with_counter(tmp_path, monkeypatch):
    """AppContext whose every HTTP call is counted. Keepalive left at its default 0."""
    monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AVENUE_MCP_KEEPALIVE_MINUTES", "0")
    from avenue_mcp.config import get_settings

    get_settings.cache_clear()

    ctx = AppContext()
    ctx.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.settings.session_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "d2lSessionVal",
                        "value": "x",
                        "domain": httpx.URL(ctx.settings.base_url).host,
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )

    counter = _Counter()
    ctx.auth._client = httpx.AsyncClient(
        base_url=ctx.settings.base_url, transport=counter.transport()
    )
    try:
        yield ctx, counter
    finally:
        get_settings.cache_clear()


class TestNoBackgroundRequestsWhenOff:
    """The criterion, measured against a request counter."""

    async def test_settings_default_is_off(self, ctx_with_counter):
        ctx, _ = ctx_with_counter
        assert ctx.settings.keepalive_minutes == 0
        assert ctx.keepalive.enabled is False
        assert ctx.keepalive.running is False

    async def test_tool_gate_does_not_start_it(self, ctx_with_counter):
        """`require_session` is what starts keepalive when it is enabled."""
        ctx, _ = ctx_with_counter
        await ctx.require_session()

        assert ctx.keepalive.running is False
        assert _keepalive_tasks() == []

    async def test_counter_stays_still_while_the_loop_runs(self, ctx_with_counter):
        """No request arrives during an idle period.

        Honest about its own limits: a short idle window cannot outlast a
        real 60-second keepalive interval, so on its own this would pass with
        keepalive *enabled* too. It is a smoke test for "nothing is firing right
        now", not proof of the off switch. The discriminating version is
        `test_a_ping_would_be_visible_to_this_counter` below, which shortens the
        interval so a ping actually lands inside the window.
        """
        ctx, counter = ctx_with_counter
        await ctx.require_session()

        assert counter.count > 0, "counter never saw the foreground request"
        settled = counter.count

        for _ in range(20):
            await asyncio.sleep(0.01)

        assert counter.count == settled, (
            f"background request(s) after idling: {counter.paths[settled:]}"
        )

    async def test_a_ping_would_be_visible_to_this_counter(self, ctx_with_counter):
        """Proves the counter can see a keepalive ping at all.

        Without this, every zero-request assertion here could be passing because
        the counter is blind rather than because nothing fired. So: force the
        keepalive on, shorten its interval below the idle window, and require the
        counter to move. If this fails, the other assertions mean nothing.
        """
        ctx, counter = ctx_with_counter
        await ctx.require_session()
        before = counter.count

        ctx.keepalive._interval = 0.01
        ctx.keepalive._task = asyncio.get_running_loop().create_task(
            ctx.keepalive._loop(), name=TASK_NAME
        )
        try:
            for _ in range(50):
                await asyncio.sleep(0.01)
                if counter.count > before:
                    break
            assert counter.count > before, (
                "a keepalive ping did not reach the request counter, so the "
                "zero-request assertions in this file prove nothing"
            )
            assert counter.paths[-1].endswith("/whoami")
        finally:
            await ctx.keepalive.stop()

    async def test_start_is_a_no_op_when_off(self, ctx_with_counter):
        """Even called directly, bypassing the `enabled` check at the call site."""
        ctx, counter = ctx_with_counter
        before = counter.count

        ctx.keepalive.start()
        for _ in range(10):
            await asyncio.sleep(0.01)

        assert ctx.keepalive.running is False
        assert _keepalive_tasks() == []
        assert counter.count == before

    async def test_status_reports_it_off(self, ctx_with_counter):
        """get_status must not claim a keepalive that is not running."""
        from avenue_mcp.tools.status import get_status

        ctx, _ = ctx_with_counter
        out = await get_status(ctx, check_session=False)

        assert out["session"]["keepalive_enabled"] is False
        assert out["session"]["keepalive_running"] is False


class TestNegativeAndZeroAreBothOff:
    @pytest.mark.parametrize("minutes", [0, -1, -60])
    def test_non_positive_intervals_are_disabled(self, minutes):
        ka = Keepalive(ping=_never_called, interval_minutes=minutes)
        assert ka.enabled is False
        ka.start()
        assert ka.running is False


class TestTheSwitchIsRealNotDead:
    """Proves the tests above measure a switch rather than a broken feature.

    If keepalive could never ping under any configuration, every zero-request
    assertion would pass while proving nothing. So: enable it, make it fire, and
    watch a ping actually happen.
    """

    async def test_enabled_keepalive_does_ping(self):
        pings = 0

        async def ping() -> bool:
            nonlocal pings
            pings += 1
            return True

        ka = Keepalive(ping=ping, interval_minutes=1)
        assert ka.enabled is True
        # A one-minute interval cannot be waited out in a test. Shorten the
        # already-computed seconds directly -- the public API takes minutes.
        ka._interval = 0.01
        ka.start()
        try:
            assert ka.running is True
            assert [t.get_name() for t in _keepalive_tasks()] == [TASK_NAME]
            for _ in range(50):
                await asyncio.sleep(0.01)
                if pings:
                    break
            assert pings > 0, "enabled keepalive never pinged"
        finally:
            await ka.stop()

        assert ka.running is False
        assert _keepalive_tasks() == []


async def _never_called() -> bool:  # pragma: no cover - must never run
    raise AssertionError("keepalive pinged while disabled")
