"""Regression tests for behaviors verified against the live Avenue host.

These pin down facts that were *guessed wrong* in the original design and only
corrected after probing avenue.cllmcmaster.ca without credentials. Each one is a
bug that silently produces a misleading message to the user.

Findings (2026-08-05, no login required):
  * avenue.mcmaster.ca is a static Apache landing page; /d2l/* 404s there.
    The Brightspace host is avenue.cllmcmaster.ca (lp 1.62, le 1.96).
  * GET /d2l/lp/auth/xsrf-tokens returns 200 + JSON with NO session.
  * An anonymous API request returns 403 + text/html.
  * SSO is Microsoft Entra SAML2 via avenue.mcmaster.ca/login.php.
"""

from __future__ import annotations

import httpx
import pytest

from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.errors import PermissionDeniedError, SessionExpiredError


def resp(status, *, ctype="application/json", text="", url="https://x/d2l/api/lp/1.0/x"):
    return httpx.Response(
        status,
        headers={"content-type": ctype},
        text=text,
        request=httpx.Request("GET", url),
    )


class TestBaseUrl:
    def test_default_points_at_the_brightspace_host(self):
        """avenue.mcmaster.ca is a landing page; /d2l/* 404s there."""
        from avenue_mcp.config import Settings

        s = Settings(_env_file=None)
        assert s.base_url == "https://avenue.cllmcmaster.ca"

    def test_login_url_is_the_landing_page(self):
        """The SAML flow starts on the landing page, not on Brightspace."""
        from avenue_mcp.config import Settings

        s = Settings(_env_file=None)
        assert s.login_url == "https://avenue.mcmaster.ca/login.php"

    def test_hosts_are_different(self):
        from avenue_mcp.config import Settings

        s = Settings(_env_file=None)
        assert s.base_url not in s.login_url


class TestAnonymous403:
    """The dangerous one.

    A logged-out user hitting an API route gets 403 + HTML. Mapping that to
    PermissionDeniedError tells them 'signing in again will not help' -- the
    exact opposite of the truth.
    """

    def test_403_with_html_is_session_expired(self):
        with pytest.raises(SessionExpiredError):
            D2LClient._raise_for_status(
                resp(403, ctype="text/html; charset=utf-8", text="<html>Sign in</html>"),
                "/d2l/api/lp/1.0/users/whoami",
            )

    def test_403_with_json_is_permission_denied(self):
        """An authenticated session genuinely lacking access answers in JSON."""
        with pytest.raises(PermissionDeniedError):
            D2LClient._raise_for_status(
                resp(403, ctype="application/json", text='{"Errors":[{"Message":"no"}]}'),
                "/d2l/api/lp/1.0/1234/classlist/",
            )

    def test_the_two_are_not_collapsed(self):
        """One is fixed by logging in; the other never will be."""
        html_err = json_err = None
        try:
            D2LClient._raise_for_status(resp(403, ctype="text/html"), "/x")
        except Exception as e:
            html_err = type(e)
        try:
            D2LClient._raise_for_status(resp(403, ctype="application/json"), "/x")
        except Exception as e:
            json_err = type(e)
        assert html_err is not json_err

    def test_permission_denied_hint_does_not_suggest_login(self):
        exc = PermissionDeniedError("nope")
        assert "login" not in exc.hint.lower() or "not a login problem" in exc.hint.lower()

    def test_session_expired_hint_does_suggest_login(self):
        assert "avenue-mcp login" in SessionExpiredError("x").hint


class TestLivenessProbe:
    """The XSRF endpoint answers 200 + JSON with no session, so it cannot be
    the liveness probe: it reports a dead session as alive."""

    def test_probe_path_is_an_authenticated_route(self):
        from avenue_mcp.auth.session import LIVENESS_PATH, XSRF_PATH

        assert LIVENESS_PATH != XSRF_PATH
        assert "users/whoami" in LIVENESS_PATH
        assert LIVENESS_PATH.startswith("/d2l/api/")

    async def test_html_200_is_not_alive(self, tmp_path):
        """Brightspace's auth wall can answer 200 with an HTML body."""
        auth = _fake_auth(tmp_path, 200, "text/html", "<html>login</html>")
        assert await auth.is_alive() is False

    async def test_403_is_not_alive(self, tmp_path):
        auth = _fake_auth(tmp_path, 403, "text/html", "<html>denied</html>")
        assert await auth.is_alive() is False

    async def test_json_200_dict_is_alive(self, tmp_path):
        auth = _fake_auth(tmp_path, 200, "application/json", '{"Identifier":"123"}')
        assert await auth.is_alive() is True

    async def test_json_200_non_dict_is_not_alive(self, tmp_path):
        auth = _fake_auth(tmp_path, 200, "application/json", "[]")
        assert await auth.is_alive() is False

    async def test_no_session_file_is_not_alive(self, tmp_path):
        from avenue_mcp.auth.session import CookieSessionAuth

        auth = CookieSessionAuth("https://x", tmp_path / "missing.json")
        assert await auth.is_alive() is False


def _fake_auth(tmp_path, status, ctype, body):
    """A CookieSessionAuth whose HTTP client returns a canned response."""
    import json

    from avenue_mcp.auth.session import CookieSessionAuth

    session = tmp_path / "session.json"
    session.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "d2lSessionVal",
                        "value": "x",
                        "domain": "avenue.cllmcmaster.ca",
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )

    auth = CookieSessionAuth("https://avenue.cllmcmaster.ca", session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": ctype}, text=body)

    auth._client = httpx.AsyncClient(
        base_url="https://avenue.cllmcmaster.ca",
        transport=httpx.MockTransport(handler),
    )
    return auth


class TestVersionNegotiation:
    def test_fallbacks_are_conservative(self):
        """Live host reports lp 1.62 / le 1.96, but 1.0 is universally
        supported, so the fallback cannot 404."""
        from avenue_mcp.client.d2l import _FALLBACK_VERSIONS

        assert _FALLBACK_VERSIONS == {"lp": "1.0", "le": "1.0"}

    def test_versions_parsed_from_product_codes(self):
        """Shape confirmed live: a list of {ProductCode, LatestVersion, ...}."""
        payload = [
            {"ProductCode": "lp", "LatestVersion": "1.62", "SupportedVersions": ["1.0"]},
            {"ProductCode": "le", "LatestVersion": "1.96", "SupportedVersions": ["1.0"]},
            {"ProductCode": "bas", "LatestVersion": "1.6", "SupportedVersions": ["1.0"]},
        ]
        found = {
            str(e["ProductCode"]).lower(): e["LatestVersion"]
            for e in payload
            if isinstance(e, dict)
        }
        assert found["lp"] == "1.62"
        assert found["le"] == "1.96"
