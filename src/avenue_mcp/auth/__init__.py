"""Authentication. The only place Playwright appears — one import, one blast radius."""

from avenue_mcp.auth.base import AuthProvider, Strategy
from avenue_mcp.auth.bearer import BearerTokenAuth, CapturedBearerAuth
from avenue_mcp.auth.session import CookieSessionAuth, SessionManager

__all__ = [
    "AuthProvider",
    "BearerTokenAuth",
    "CapturedBearerAuth",
    "CookieSessionAuth",
    "SessionManager",
    "Strategy",
]
