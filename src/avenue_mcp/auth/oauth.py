"""OAuth slot — documented, not implemented (docs/01-authentication.md).

D2L's supported path is OAuth 2.0, and it requires a Brightspace administrator
to register an application via Manage Extensibility. A student account cannot
do this, so this class cannot be written against a real credential today.

It exists as a stub because the shape of the eventual change matters: if
McMaster UTS ever issues a client ID and secret, this class gets a body, the
config selects it, and *nothing in the tool layer changes*. That is the whole
point of AuthProvider.

Asking UTS about Valence API access remains the clean long-term fix — see
docs/07-risks-and-policy.md.
"""

from __future__ import annotations

import httpx

from avenue_mcp.auth.base import AuthProvider, Strategy
from avenue_mcp.errors import ConfigError

AUTH_URL = "https://auth.brightspace.com/oauth2/auth"
TOKEN_URL = "https://auth.brightspace.com/core/connect/token"


class OAuthAuth(AuthProvider):
    """Not implemented. Requires admin-registered client credentials."""

    strategy = Strategy.OAUTH

    def __init__(self, client_id: str | None = None, client_secret: str | None = None) -> None:
        raise ConfigError(
            "OAuth is not available: registering a Valence application requires a "
            "Brightspace administrator, which a student account cannot do. The "
            "server uses browser-session authentication instead — run "
            "`avenue-mcp login`."
        )

    @property
    def needs_browser_to_refresh(self) -> bool:
        return False

    async def authorize(self, request: httpx.Request) -> httpx.Request:  # pragma: no cover
        raise NotImplementedError

    async def is_alive(self, client: httpx.AsyncClient) -> bool:  # pragma: no cover
        return False
