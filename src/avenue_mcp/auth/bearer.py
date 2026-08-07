"""Bearer-token strategies (docs/01-authentication.md).

Brightspace's frontend attaches `Authorization: Bearer <JWT>` to its own
/d2l/api/ calls. Two ways for us to hold such a token:

    BearerTokenAuth      mint one from the session cookies, no browser
    CapturedBearerAuth   replay one lifted from the frontend during login

The first is strongly preferred: it keeps the browser a one-time login step.
The second is the floor — it's what the inspectable prior art
(joshuasoup/d2l-mcp) actually ships, so it is known to work — but its token
can only be refreshed by re-running the browser flow, roughly hourly.
"""

from __future__ import annotations

import logging
import time

import httpx

from avenue_mcp.auth.base import AuthProvider, Strategy

log = logging.getLogger(__name__)

XSRF_PATH = "/d2l/lp/auth/xsrf-tokens"
TOKEN_PATH = "/d2l/lp/auth/oauth2/token"
WHOAMI_PROBE = "/d2l/api/lp/1.0/users/whoami"

# Re-mint this many seconds before nominal expiry, so a token doesn't die
# mid-flight on a slow call.
EXPIRY_MARGIN_SECONDS = 300
DEFAULT_LIFETIME_SECONDS = 3600


async def fetch_xsrf_token(client: httpx.AsyncClient) -> str | None:
    """GET the XSRF token. Doubles as the session liveness probe.

    Returns None rather than raising — callers distinguish "no session" from
    "session fine but this route is forbidden", and an exception here would
    collapse that distinction.
    """
    try:
        response = await client.get(XSRF_PATH)
    except httpx.HTTPError as exc:
        log.debug("XSRF fetch failed: %s", exc)
        return None

    if response.status_code != 200:
        return None

    # An expired session commonly returns the login page with a 200, so a
    # status check alone is not enough. See docs/01 "Detecting expiry".
    if "json" not in response.headers.get("content-type", "").lower():
        return None

    try:
        payload = response.json()
    except ValueError:
        return None

    if isinstance(payload, dict):
        for key in ("referrerToken", "ReferrerToken", "token"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


class BearerTokenAuth(AuthProvider):
    """Mints a short-lived bearer from the session cookies. No browser."""

    strategy = Strategy.MINTED_BEARER

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def needs_browser_to_refresh(self) -> bool:
        return False

    def _is_fresh(self) -> bool:
        return bool(self._token) and time.time() < self._expires_at - EXPIRY_MARGIN_SECONDS

    async def mint(self, client: httpx.AsyncClient) -> str | None:
        """POST the session cookies + XSRF token, get back a bearer.

        This is why XSRF is v1-critical rather than v2 groundwork: minting is
        a POST, so the token is on the critical path from the first API call.
        """
        if self._is_fresh():
            return self._token

        xsrf = await fetch_xsrf_token(client)
        if xsrf is None:
            return None

        try:
            response = await client.post(
                TOKEN_PATH,
                headers={
                    "X-Csrf-Token": xsrf,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content="scope=*:*:*",
            )
        except httpx.HTTPError as exc:
            log.debug("Token mint failed: %s", exc)
            return None

        if response.status_code != 200:
            log.debug("Token mint returned %d", response.status_code)
            return None

        try:
            payload = response.json()
        except ValueError:
            return None

        if not isinstance(payload, dict):
            return None

        token = payload.get("access_token") or payload.get("accessToken")
        if not isinstance(token, str) or not token:
            return None

        lifetime = payload.get("expires_in", DEFAULT_LIFETIME_SECONDS)
        try:
            lifetime = float(lifetime)
        except (TypeError, ValueError):
            lifetime = DEFAULT_LIFETIME_SECONDS

        self._token = token
        self._expires_at = time.time() + lifetime
        log.debug("Minted bearer token, valid %.0fs", lifetime)
        return token

    async def authorize(self, request: httpx.Request) -> httpx.Request:
        if self._token:
            request.headers["Authorization"] = f"Bearer {self._token}"
        return request

    async def is_alive(self, client: httpx.AsyncClient) -> bool:
        return await self.mint(client) is not None


class CapturedBearerAuth(AuthProvider):
    """Replays a bearer captured from frontend traffic during Playwright login.

    The floor strategy. Known to work, but the token dies in about an hour and
    the only way to get another is to re-run the browser flow — so if this is
    the strategy that wins, that fact belongs in the README rather than buried.
    """

    strategy = Strategy.CAPTURED_BEARER

    def __init__(self, token: str, captured_at: float | None = None) -> None:
        self._token = token
        self._captured_at = captured_at or time.time()

    @property
    def needs_browser_to_refresh(self) -> bool:
        return True

    @property
    def age_seconds(self) -> float:
        return time.time() - self._captured_at

    @property
    def likely_expired(self) -> bool:
        return self.age_seconds > DEFAULT_LIFETIME_SECONDS - EXPIRY_MARGIN_SECONDS

    async def authorize(self, request: httpx.Request) -> httpx.Request:
        request.headers["Authorization"] = f"Bearer {self._token}"
        return request

    async def is_alive(self, client: httpx.AsyncClient) -> bool:
        try:
            response = await client.get(
                WHOAMI_PROBE,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and "json" in response.headers.get(
            "content-type", ""
        ).lower()
