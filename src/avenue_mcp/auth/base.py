"""Auth provider interface.

Two implementations are contemplated:

* CookieSessionAuth -- the working one. Reuses a real browser session.
* OAuthAuth        -- documented, not implemented. If your institution ever
                      issues a client ID and secret, this drops in and
                      *nothing in the tool layer changes*.

That separation is why docs/01-authentication.md says OAuth is closed *today*
rather than irrelevant.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx


@runtime_checkable
class AuthProvider(Protocol):
    """Attaches credentials to outbound requests and reports liveness."""

    async def apply(self, client: httpx.AsyncClient) -> None:
        """Configure the client (cookies, default headers)."""
        ...

    async def headers_for(self, method: str) -> dict[str, str]:
        """Per-request headers. Non-GET may need a CSRF token."""
        ...

    async def is_alive(self) -> bool:
        """Cheap liveness check."""
        ...
