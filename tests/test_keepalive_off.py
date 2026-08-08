"""Keepalive off must mean genuinely zero background traffic.

docs/06 Phase 4 exit criterion: "With `AVENUE_MCP_KEEPALIVE_MINUTES=0`, **zero**
background requests are made". docs/07 states this as a property of the software
-- no polling for data, ever -- and a documented promise nobody verified is just
a hope. The default is `0`, so this is the path essentially every user runs.

`Keepalive` is the only timer in the codebase, which is what makes it worth
pinning: it is the single place a background request could come from.

The important test here is the app-level one. Asserting `enabled is False` only
restates the getter; asserting that a **real request counter stays still while
the event loop runs** is the property docs/07 actually claims. So the counting
transport deliberately serves the liveness route for real rather than stubbing
`is_alive` -- a stub would make the ping invisible to the counter and the test
would pass no matter what the timer did.
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
        """The assertion that actually matters.

        A real request goes out first, so the counter is proven to work. Then the
        event loop is handed plenty of turns, and nothing more may arrive.
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
