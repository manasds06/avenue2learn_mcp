"""The AuthProvider protocol (docs/01-authentication.md).

Three strategies implement this, tried cheapest-and-most-durable first:

    CookieSessionAuth   cookies only                      no browser at runtime
    BearerTokenAuth     mints a bearer from the cookies   no browser at runtime
    CapturedBearerAuth  replays a bearer lifted at login  browser needed ~hourly

Which one Avenue accepts is a Phase 0 measurement, not an assumption. The
interface exists so that the answer can be discovered rather than guessed
right in advance — and so that a future admin-granted OAuth credential is a
drop-in that changes nothing in the tool layer.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

import httpx


class Strategy(str, Enum):
    """Identifies the active strategy, for logging and probe output."""

    COOKIE = "cookie"
    MINTED_BEARER = "minted_bearer"
    CAPTURED_BEARER = "captured_bearer"
    OAUTH = "oauth"


@runtime_checkable
class AuthProvider(Protocol):
    """Applies credentials to an outgoing request."""

    strategy: Strategy

    async def authorize(self, request: httpx.Request) -> httpx.Request:
        """Attach whatever this strategy needs. Cookies ride on the client jar."""
        ...

    async def is_alive(self, client: httpx.AsyncClient) -> bool:
        """Cheap liveness probe. Must not raise on a dead session — return False."""
        ...

    @property
    def needs_browser_to_refresh(self) -> bool:
        """True if recovering from expiry requires a visible browser.

        Surfaced to the user rather than hidden: it's the difference between
        re-authenticating daily and hourly.
        """
        ...
