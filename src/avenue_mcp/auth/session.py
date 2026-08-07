"""SessionManager and CookieSessionAuth (docs/01-authentication.md).

Loads the Playwright storage_state written by `avenue-mcp login`, builds an
httpx client from its cookie jar, and resolves which of the three auth
strategies this instance actually accepts.

`require_session()` never silently launches a browser. An MCP server runs
headless under a client; popping a browser window mid-tool-call is hostile and
can hang the call. Tools fail with an actionable message instead, and the user
re-runs login deliberately.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from avenue_mcp.auth.base import AuthProvider, Strategy
from avenue_mcp.auth.bearer import (
    WHOAMI_PROBE,
    BearerTokenAuth,
    CapturedBearerAuth,
    fetch_xsrf_token,
)
from avenue_mcp.auth.filelock import write_private
from avenue_mcp.config import Settings, get_settings
from avenue_mcp.errors import NoSessionError, SessionExpiredError

log = logging.getLogger(__name__)


class CookieSessionAuth(AuthProvider):
    """Cookies only — the simplest possible client, if Avenue accepts it.

    Nothing to attach: the cookie jar lives on the httpx client. This class
    exists to make "cookies alone" a first-class, testable strategy rather
    than an implicit default.
    """

    strategy = Strategy.COOKIE

    @property
    def needs_browser_to_refresh(self) -> bool:
        return False

    async def authorize(self, request: httpx.Request) -> httpx.Request:
        return request

    async def is_alive(self, client: httpx.AsyncClient) -> bool:
        try:
            response = await client.get(WHOAMI_PROBE)
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        # A 200 of text/html is the login page, not a live session.
        return "json" in response.headers.get("content-type", "").lower()


class SessionManager:
    """Owns the persisted session and the resolved auth strategy."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self._provider: AuthProvider | None = None
        self._xsrf: str | None = None

    # --- Persistence -----------------------------------------------------

    @property
    def session_path(self) -> Path:
        return self.settings.session_path

    def has_session(self) -> bool:
        return self.session_path.exists()

    def load_state(self) -> dict[str, Any]:
        if not self.has_session():
            raise NoSessionError()
        try:
            return json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NoSessionError(
                f"Session file at {self.session_path} is unreadable or corrupt ({exc})."
            ) from exc

    def save_state(
        self,
        storage_state: dict[str, Any],
        *,
        captured_bearer: str | None = None,
    ) -> None:
        """Persist storage_state plus any bearer captured during login.

        Written with owner-only permissions — via icacls on Windows, where
        chmod would silently do nothing. See auth/filelock.py.
        """
        payload = dict(storage_state)
        if captured_bearer:
            payload["_avenue_mcp"] = {
                "captured_bearer": captured_bearer,
                "captured_at": time.time(),
            }
        write_private(self.session_path, json.dumps(payload, indent=2))

    # --- HTTP client -----------------------------------------------------

    def _cookies_from_state(self, state: dict[str, Any]) -> httpx.Cookies:
        """Carry the whole jar, not just the documented pair.

        Load-balancer affinity cookies can matter for routing, and cherry-
        picking d2lSessionVal/d2lSecureSessionVal is a good way to produce a
        403 that looks like a permissions problem but isn't.
        """
        jar = httpx.Cookies()
        for cookie in state.get("cookies", []):
            name, value = cookie.get("name"), cookie.get("value")
            if not name or value is None:
                continue
            jar.set(name, value, domain=cookie.get("domain", ""), path=cookie.get("path", "/"))
        return jar

    def build_client(self) -> httpx.AsyncClient:
        """An httpx client carrying the session cookies. No network yet."""
        state = self.load_state()
        return httpx.AsyncClient(
            base_url=self.settings.base_url,
            cookies=self._cookies_from_state(state),
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "application/json, text/plain, */*",
            },
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=False,  # a redirect to SSO means expiry, not success
        )

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self.build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- Strategy resolution ---------------------------------------------

    async def resolve_provider(self, *, force: bool = False) -> AuthProvider:
        """Pick the working strategy: cookies -> minted bearer -> captured.

        Cheapest and most durable first. The first two keep the browser a
        login-only step; only the third makes it a runtime dependency.
        """
        if self._provider is not None and not force:
            return self._provider

        client = await self.client()

        cookie_auth = CookieSessionAuth()
        if await cookie_auth.is_alive(client):
            log.info("Auth strategy: cookies")
            self._provider = cookie_auth
            return cookie_auth

        minted = BearerTokenAuth()
        if await minted.is_alive(client):
            log.info("Auth strategy: bearer minted from session cookies")
            self._provider = minted
            return minted

        captured = self._load_captured_bearer()
        if captured is not None and await captured.is_alive(client):
            log.warning(
                "Auth strategy: captured bearer. This token expires roughly hourly "
                "and can only be refreshed by re-running `avenue-mcp login`."
            )
            self._provider = captured
            return captured

        raise SessionExpiredError(
            "No working authentication strategy — cookies, minted bearer, and "
            "captured bearer were all rejected by Avenue."
        )

    def _load_captured_bearer(self) -> CapturedBearerAuth | None:
        try:
            state = self.load_state()
        except NoSessionError:
            return None
        extra = state.get("_avenue_mcp") or {}
        token = extra.get("captured_bearer")
        if not isinstance(token, str) or not token:
            return None
        return CapturedBearerAuth(token, captured_at=extra.get("captured_at"))

    # --- Liveness --------------------------------------------------------

    async def xsrf_token(self, *, refresh: bool = False) -> str | None:
        if self._xsrf is None or refresh:
            self._xsrf = await fetch_xsrf_token(await self.client())
        return self._xsrf

    async def is_alive(self) -> bool:
        provider = self._provider or CookieSessionAuth()
        return await provider.is_alive(await self.client())

    async def require_session(self) -> tuple[httpx.AsyncClient, AuthProvider]:
        """The entry point every network tool goes through.

        Raises NoSessionError or SessionExpiredError with an actionable
        message. Never launches a browser.
        """
        if not self.has_session():
            raise NoSessionError()
        client = await self.client()
        provider = await self.resolve_provider()
        return client, provider
