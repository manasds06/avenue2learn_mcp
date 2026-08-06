"""Cookie-session authentication.

Brightspace's own frontend calls /d2l/api/{lp,le}/... authenticated by the
browser session. We reuse that: session cookies plus an XSRF token from
/d2l/lp/auth/xsrf-tokens (required on non-GET only).

Calls run as the logged-in user with their permissions. There is no privilege
escalation -- the API enforces the same role checks as the UI.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from avenue_mcp.errors import NoSessionError, SessionExpiredError

log = logging.getLogger(__name__)

XSRF_PATH = "/d2l/lp/auth/xsrf-tokens"

# The liveness probe must be an AUTHENTICATED route.
#
# The XSRF endpoint is unsuitable and this was verified against the live host:
# GET /d2l/lp/auth/xsrf-tokens returns 200 with application/json even with NO
# session at all. Using it as the probe reports a dead session as alive, so
# require_session() passes and every subsequent call fails confusingly -- the
# exact failure mode the error taxonomy exists to prevent.
#
# whoami is a real discriminator: anonymous -> 403 + text/html,
# authenticated -> 200 + application/json.
LIVENESS_PATH = "/d2l/api/lp/1.0/users/whoami"

_SESSION_COOKIES = ("d2lSessionVal", "d2lSecureSessionVal")

# A browser-like UA. Some Brightspace deployments behave differently for
# unrecognized clients; Phase 0 confirms whether this is actually required.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class CookieSessionAuth:
    """AuthProvider backed by a persisted Playwright storage_state."""

    def __init__(self, base_url: str, session_path: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_path = session_path
        self._state: dict[str, Any] | None = None
        self._xsrf: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._loaded_at: datetime | None = None
        self._last_ok: datetime | None = None

    # --- state ------------------------------------------------------------

    @property
    def session_present(self) -> bool:
        return self.session_path.is_file()

    def load_state(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        if not self.session_present:
            raise NoSessionError("No saved Avenue session.")
        try:
            raw = json.loads(self.session_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NoSessionError(f"Session file unreadable: {exc}") from exc
        if not isinstance(raw, dict) or not raw.get("cookies"):
            raise NoSessionError("Session file contains no cookies.")
        self._state = raw
        self._loaded_at = datetime.fromtimestamp(
            self.session_path.stat().st_mtime, tz=timezone.utc
        )
        return raw

    def cookie_jar(self) -> httpx.Cookies:
        """Carry the whole jar, not just the documented pair.

        Load-balancer affinity cookies can matter for routing, so cherry-picking
        is the more fragile choice.
        """
        jar = httpx.Cookies()
        for c in self.load_state().get("cookies", []):
            name, value = c.get("name"), c.get("value")
            if not name or value is None:
                continue
            jar.set(name, value, domain=c.get("domain", ""), path=c.get("path", "/"))
        return jar

    def has_session_cookies(self) -> bool:
        try:
            names = {c.get("name") for c in self.load_state().get("cookies", [])}
        except NoSessionError:
            return False
        return any(n in names for n in _SESSION_COOKIES)

    @property
    def session_age_minutes(self) -> float | None:
        if self._loaded_at is None and self.session_present:
            self.load_state()
        if self._loaded_at is None:
            return None
        delta = datetime.now(timezone.utc) - self._loaded_at
        return round(delta.total_seconds() / 60.0, 1)

    # --- httpx ------------------------------------------------------------

    def client(self, timeout: float = 60.0) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                cookies=self.cookie_jar(),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=timeout,
                follow_redirects=False,  # a redirect to SSO means expiry
            )
        return self._client

    async def apply(self, client: httpx.AsyncClient) -> None:
        client.cookies.update(self.cookie_jar())

    async def headers_for(self, method: str) -> dict[str, str]:
        """XSRF is required for non-GET only.

        v1 is entirely read-only, so this is not strictly needed yet. We manage
        it anyway: it is a reliable liveness probe, and the v2 write path needs
        it. Building it in now avoids a retrofit.
        """
        if method.upper() in ("GET", "HEAD", "OPTIONS"):
            return {}
        token = await self.xsrf_token()
        return {"X-Csrf-Token": token} if token else {}

    async def xsrf_token(self, force: bool = False) -> str | None:
        if self._xsrf and not force:
            return self._xsrf
        try:
            resp = await self.client().get(XSRF_PATH)
        except httpx.HTTPError as exc:
            log.debug("xsrf fetch failed: %s", exc)
            return None
        if resp.status_code != 200:
            return None
        self._xsrf = _parse_xsrf(resp)
        return self._xsrf

    # --- liveness ---------------------------------------------------------

    async def is_alive(self) -> bool:
        """Probe an authenticated route. Requires 200 AND a JSON body.

        Verified against the live host: anonymous gets 403 + text/html here,
        while a valid session gets 200 + application/json. Both halves of the
        check matter -- a 200 whose body is HTML is Brightspace's auth wall, not
        a live session.
        """
        if not self.session_present:
            return False
        try:
            resp = await self.client().get(LIVENESS_PATH)
        except httpx.HTTPError as exc:
            log.debug("liveness probe failed: %s", exc)
            return False

        # Do not trust the status code alone, in either direction.
        if resp.status_code in (401, 403):
            return False
        if 300 <= resp.status_code < 400:
            return False
        if resp.status_code != 200:
            return False
        ctype = resp.headers.get("content-type", "").lower()
        if "json" not in ctype:
            return False
        try:
            body = resp.json()
        except ValueError:
            return False
        if not isinstance(body, dict):
            return False

        self._last_ok = datetime.now(timezone.utc)
        return True

    async def require_alive(self) -> None:
        """Raise the *correct* error for the situation.

        Never silently launches a browser: an MCP server runs headless under a
        client, and popping a window mid-tool-call is hostile and can hang the
        call.
        """
        if not self.session_present:
            raise NoSessionError("Not logged in to Avenue.")
        if not await self.is_alive():
            raise SessionExpiredError("The Avenue session is no longer valid.")

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def status(self) -> dict[str, Any]:
        return {
            "present": self.session_present,
            "has_session_cookies": self.has_session_cookies(),
            "age_minutes": self.session_age_minutes,
            "last_verified_utc": (
                self._last_ok.strftime("%Y-%m-%dT%H:%M:%SZ") if self._last_ok else None
            ),
            "path": str(self.session_path),
        }


def _parse_xsrf(resp: httpx.Response) -> str | None:
    """The token is usually `referrerToken` in a JSON body."""
    try:
        data = resp.json()
    except ValueError:
        text = resp.text.strip()
        return text if text and len(text) < 512 and "<" not in text else None
    if isinstance(data, dict):
        for key in ("referrerToken", "ReferrerToken", "token", "Token"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    elif isinstance(data, str) and data:
        return data
    return None


# --- The OAuth slot -------------------------------------------------------


class OAuthAuth:
    """Documented, not implemented.

    If McMaster ever registers an application and issues a client ID + secret,
    this class gets written, config selects it, and the tool layer is untouched.

    Note what does NOT work: registering your own Microsoft Entra app and
    exchanging a MacID token for Avenue access. Entra tokens are scoped to the
    app that requested them; Brightspace has no trust relationship with your
    registration and no endpoint that exchanges a third-party IdP token for a
    Brightspace session. See docs/01-authentication.md.
    """

    def __init__(self, *_: object, **__: object) -> None:
        raise NotImplementedError(
            "OAuth requires a McMaster-registered application. See "
            "docs/01-authentication.md."
        )
