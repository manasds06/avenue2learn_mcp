"""A DEBUG log level must not turn into a credential dump.

docs/06 Phase 4 exit criterion: "Logs contain no cookies, tokens, or session
values -- grep-verified". This is the grep, automated.

**These tests need a real socket, and must keep it.** `httpx.MockTransport`
short-circuits above `httpcore`, and `httpcore` is the layer that handles raw
headers -- so a mocked version of this test passes no matter how badly the real
code leaks. That is exactly how the leak survived 352 tests. If you are tempted
to "simplify" this to MockTransport, the test stops testing anything.

The leak these guard against was real, not theoretical: with the root logger at
DEBUG, `httpcore` wrote the whole response header list into the log, so an
`X-Csrf-Token` -- and a rotated `Set-Cookie` session value -- went to disk in
plain text. See src/avenue_mcp/util/logging.py.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from avenue_mcp.auth.session import CookieSessionAuth
from avenue_mcp.util.logging import _NOISY, cap_third_party_loggers

# Distinctive so a substring search cannot match by accident.
SENT_COOKIE = "SENTCOOKIE_CANARY_AAAA1111"
ROTATED_COOKIE = "ROTATEDCOOKIE_CANARY_BBBB2222"
CSRF_TOKEN = "CSRFTOKEN_CANARY_CCCC3333"

ALL_CANARIES = (SENT_COOKIE, ROTATED_COOKIE, CSRF_TOKEN)


class _Handler(BaseHTTPRequestHandler):
    """Answers like Brightspace: a CSRF header, and a rotated session cookie."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's API
        body = json.dumps({"Identifier": "42"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Csrf-Token", CSRF_TOKEN)
        self.send_header(
            "Set-Cookie", f"d2lSessionVal={ROTATED_COOKIE}; Path=/; HttpOnly"
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Silence the stdlib access log; it writes to stderr, not our buffer."""


@pytest.fixture
def local_server(monkeypatch):
    # httpx honours proxy env vars and, unlike requests, has NO implicit
    # localhost bypass -- so on a machine behind a corporate/university proxy
    # every test here would fail trying to reach 127.0.0.1 through it.
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(var, raising=False)

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def captured_logs():
    """Root at DEBUG, every logger captured, and logging state restored after.

    Restoration matters: leaving httpcore at DEBUG would silently change the
    behaviour of any test that runs later in the same process.
    """
    root = logging.getLogger()
    saved_handlers, saved_root_level = list(root.handlers), root.level
    saved_levels = {name: logging.getLogger(name).level for name in _NOISY}

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.NOTSET)
    for existing in saved_handlers:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        for existing in saved_handlers:
            root.addHandler(existing)
        root.setLevel(saved_root_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


def _session_file(tmp_path: Path) -> Path:
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "d2lSessionVal",
                        "value": SENT_COOKIE,
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


async def _make_request(base_url: str, tmp_path: Path) -> str:
    auth = CookieSessionAuth(base_url, _session_file(tmp_path))
    client = auth.client()
    try:
        response = await client.get("/d2l/api/lp/1.0/users/whoami")
        assert response.status_code == 200
        # Guard the guard: if the cookie were never sent, "no cookie in the
        # logs" would be vacuously true.
        assert SENT_COOKIE in str(response.request.headers)
        return response.text
    finally:
        await client.aclose()


class TestNoCredentialsInLogs:
    """With everything at DEBUG, no canary may reach the log."""

    async def test_no_canary_appears_anywhere(
        self, local_server, captured_logs, tmp_path
    ):
        cap_third_party_loggers()
        await _make_request(local_server, tmp_path)

        logs = captured_logs.getvalue()
        leaked = [c for c in ALL_CANARIES if c in logs]
        assert not leaked, f"credential(s) written to the log: {leaked}"

    async def test_cookie_name_value_pair_never_appears(
        self, local_server, captured_logs, tmp_path
    ):
        """Catches a partial leak that a value-only search would miss."""
        cap_third_party_loggers()
        await _make_request(local_server, tmp_path)

        assert "d2lSessionVal=" not in captured_logs.getvalue()

    async def test_useful_logging_still_survives(
        self, local_server, captured_logs, tmp_path
    ):
        """The fix must be a muzzle, not a gag.

        httpx's one-line request record carries no credentials -- Valence puts no
        tokens in URLs -- and it is the main thing you want when debugging. If
        this assertion fails, someone silenced httpx wholesale and threw away
        real debuggability to fix a leak that lived in httpcore.
        """
        cap_third_party_loggers()
        await _make_request(local_server, tmp_path)

        logs = captured_logs.getvalue()
        assert "HTTP Request:" in logs
        assert "/d2l/api/lp/1.0/users/whoami" in logs


class TestTheLeakIsRealWithoutTheCap:
    """Proves the tests above have teeth.

    A passing test is worthless if it would also pass on the broken code. Rather
    than trusting that, this reproduces the unfixed configuration and asserts the
    leak is observable -- so if a future httpcore stops logging headers, this
    fails and tells us the cap is now redundant instead of silently load-bearing.
    """

    async def test_response_headers_leak_when_httpcore_is_left_at_debug(
        self, local_server, captured_logs, tmp_path
    ):
        # Deliberately do NOT call cap_third_party_loggers().
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
        await _make_request(local_server, tmp_path)

        logs = captured_logs.getvalue()
        assert CSRF_TOKEN in logs or ROTATED_COOKIE in logs, (
            "httpcore no longer logs response headers at DEBUG. The cap in "
            "util/logging.py may now be unnecessary -- verify before removing."
        )

    async def test_request_cookie_never_leaks_even_uncapped(
        self, local_server, captured_logs, tmp_path
    ):
        """The outbound direction was always safe, and this records why.

        httpcore logs `request=<Request [b'GET']>`, a repr with no headers. Worth
        pinning: if that repr ever grows to include headers, the cap stops being
        sufficient and this fails.
        """
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
        await _make_request(local_server, tmp_path)

        assert SENT_COOKIE not in captured_logs.getvalue()


class TestEveryEntryPointCaps:
    """The cap is worthless if a process start skips it.

    This class exists because it was missing. Every test above calls
    `cap_third_party_loggers()` by hand, so deleting the call from the real
    startup path left all of them green while `avenue-mcp serve` leaked -- a
    guard on one unprotected line, with nothing watching the line.

    There are exactly two process entry points, and both must cap. If a third is
    added, add it here.
    """

    @pytest.mark.parametrize("level", ["DEBUG", "debug", "INFO"])
    def test_cli_setup_caps(self, captured_logs, level):
        """`avenue-mcp <cmd>` goes through `main()` -> `_setup_logging`."""
        from avenue_mcp.__main__ import _setup_logging

        logging.getLogger("httpcore").setLevel(logging.NOTSET)
        _setup_logging(level)

        assert logging.getLogger("httpcore.http11").getEffectiveLevel() >= logging.INFO

    def test_server_run_uses_the_same_setup(self):
        """`server.run()` used to hold a second, uncapped `basicConfig`.

        It was safe only because importing the module installs a root handler,
        making its `basicConfig` a silent no-op -- safety by third-party accident.
        Asserting on the shared function rather than calling `run()` (which would
        block on stdio) keeps the two from drifting apart again.
        """
        import inspect

        from avenue_mcp import server

        # Comments are stripped: the body explains the old basicConfig, and
        # matching prose would fail for the wrong reason.
        code = "\n".join(
            line.split("#", 1)[0]
            for line in inspect.getsource(server.run).splitlines()
        )
        assert "configure_logging" in code
        assert "basicConfig" not in code, (
            "server.run() has its own logging setup again; it will not be capped"
        )

    def test_configure_logging_sends_records_to_stderr(self, capsys):
        """A log line on stdout corrupts the MCP protocol stream."""
        from avenue_mcp.util.logging import configure_logging

        configure_logging("DEBUG")
        logging.getLogger("avenue_mcp.test").warning("marker-not-for-stdout")

        captured = capsys.readouterr()
        assert "marker-not-for-stdout" not in captured.out


class TestCapBehaviour:
    def test_is_idempotent(self, captured_logs):
        cap_third_party_loggers()
        first = logging.getLogger("httpcore").level
        cap_third_party_loggers()
        assert logging.getLogger("httpcore").level == first == logging.INFO

    def test_does_not_make_a_quieter_logger_noisier(self, captured_logs):
        """A user who silenced httpcore entirely keeps their setting."""
        logging.getLogger("httpcore").setLevel(logging.CRITICAL)
        cap_third_party_loggers()
        assert logging.getLogger("httpcore").level == logging.CRITICAL

    def test_child_loggers_are_covered_via_the_parent(self, captured_logs):
        """httpcore.http11 is the one that logs headers, and it is a child."""
        cap_third_party_loggers()
        child = logging.getLogger("httpcore.http11")
        assert child.level == logging.NOTSET  # untouched directly...
        assert not child.isEnabledFor(logging.DEBUG)  # ...but still muzzled

    def test_a_child_with_its_own_level_is_capped_too(self, captured_logs):
        """The case parent-only capping misses.

        Once a child sets an explicit level, the parent's is ignored. No current
        version of httpcore does this, so it is latent -- but the whole point of
        the cap is that a library's logging choices are not ours to predict.
        """
        child = logging.getLogger("httpcore.http11")
        child.setLevel(logging.DEBUG)
        try:
            cap_third_party_loggers()
            assert not child.isEnabledFor(logging.DEBUG)
        finally:
            child.setLevel(logging.NOTSET)
