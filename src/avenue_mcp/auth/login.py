"""Interactive Playwright login (docs/01-authentication.md).

The browser acquires a session and nothing else. It is never used for
scraping, and never launched from a tool call.

Two things this flow does that are easy to get wrong:

1. **It waits on a post-login success signal, not on form selectors.** McMaster
   SSO redesigns would break selector-based waits; landing on an authenticated
   Avenue URL is stable across them.
2. **It captures any bearer token the frontend uses.** One request listener,
   and it guarantees a working credential even if cookie auth turns out not to
   be accepted by this instance.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from avenue_mcp.config import Settings, get_settings
from avenue_mcp.errors import LoginTimeoutError
from avenue_mcp.auth.session import SessionManager

log = logging.getLogger(__name__)

LOGIN_TIMEOUT_MS = 5 * 60 * 1000  # generous: MFA pushes get missed

# Hosts that mean "still authenticating". McMaster bounces through Microsoft
# Entra; the exact chain is a Phase 0 observation.
_LOGIN_HOST_MARKERS = ("login", "sso", "adfs", "microsoftonline", "duosecurity", "okta")

# Launching real Edge rather than Playwright's bundled Chromium: Entra ID
# risk-scores unfamiliar browsers, and the bundled build can trigger an MFA
# challenge on every single run. Falls back if the channel isn't installed.
_PREFERRED_CHANNELS = ("msedge", "chrome")


def _is_login_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in _LOGIN_HOST_MARKERS)


def _is_authenticated_url(url: str, base_url: str) -> bool:
    """True once we're on Avenue proper and no longer in the SSO chain."""
    host = base_url.split("://", 1)[-1].rstrip("/").lower()
    lowered = url.lower()
    return host in lowered and "/d2l/" in lowered and not _is_login_url(lowered)


async def _launch(playwright: Any) -> tuple[Any, str]:
    """Prefer a real installed browser; fall back to bundled Chromium."""
    last_error: Exception | None = None
    for channel in _PREFERRED_CHANNELS:
        try:
            browser = await playwright.chromium.launch(headless=False, channel=channel)
            log.info("Launched %s for login", channel)
            return browser, channel
        except Exception as exc:  # channel not installed on this machine
            last_error = exc
            log.debug("Channel %s unavailable: %s", channel, exc)

    log.info(
        "Falling back to Playwright's bundled Chromium. If Entra prompts for MFA "
        "on every login, installing Microsoft Edge avoids that."
    )
    try:
        return await playwright.chromium.launch(headless=False), "bundled-chromium"
    except Exception as exc:
        raise LoginTimeoutError(
            "Could not launch a browser for login. If Playwright's browsers "
            "aren't installed yet, run: python -m playwright install chromium"
        ) from exc


async def login(settings: Settings | None = None) -> dict[str, Any]:
    """Run the interactive login and persist the session.

    Returns a summary dict for the CLI to print. Raises LoginTimeoutError if
    the user doesn't finish in time.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise LoginTimeoutError(
            "Playwright isn't installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

    captured_bearer: str | None = None

    async with async_playwright() as playwright:
        browser, channel = await _launch(playwright)
        # A persistent-looking context keeps Entra's "remember this device"
        # state useful across runs.
        context = await browser.new_context(user_agent=settings.user_agent)
        page = await context.new_page()

        def _on_request(request: Any) -> None:
            nonlocal captured_bearer
            if captured_bearer or "/d2l/api/" not in request.url:
                return
            header = request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                captured_bearer = header[7:]
                log.debug("Captured a bearer token from frontend traffic")

        context.on("request", _on_request)

        print(f"\nOpening {settings.base_url} in {channel}.")
        print("Sign in with your MacID, password, and MFA.")
        print("This window closes by itself once you're through.\n")

        await page.goto(f"{settings.base_url}/d2l/home", wait_until="domcontentloaded")

        try:
            await _wait_for_login(page, settings.base_url)
        except asyncio.TimeoutError as exc:
            await context.close()
            await browser.close()
            raise LoginTimeoutError() from exc

        # Let the frontend settle so its API calls (and their bearer) happen.
        try:
            await page.wait_for_timeout(3000)
        except Exception:
            pass

        storage_state = await context.storage_state()
        await context.close()
        await browser.close()

    manager = SessionManager(settings)
    manager.save_state(storage_state, captured_bearer=captured_bearer)

    from avenue_mcp.auth.filelock import describe_permissions

    return {
        "session_path": str(manager.session_path),
        "cookies_captured": len(storage_state.get("cookies", [])),
        "bearer_captured": captured_bearer is not None,
        "permissions": describe_permissions(manager.session_path),
    }


async def _wait_for_login(page: Any, base_url: str) -> None:
    """Poll for an authenticated Avenue URL.

    Deliberately not `wait_for_url` with a fixed pattern and not a selector
    wait — the SSO chain varies and its markup changes. What doesn't change is
    where you end up.
    """
    deadline = asyncio.get_running_loop().time() + LOGIN_TIMEOUT_MS / 1000

    while asyncio.get_running_loop().time() < deadline:
        if page.is_closed():
            raise asyncio.TimeoutError("Login window was closed.")
        try:
            if _is_authenticated_url(page.url, base_url):
                log.info("Login complete: %s", page.url)
                return
        except Exception:
            pass  # navigation in flight
        await asyncio.sleep(1.0)

    raise asyncio.TimeoutError("Timed out waiting for sign-in.")


def login_sync(settings: Settings | None = None) -> dict[str, Any]:
    """Blocking wrapper for the CLI."""
    if sys.platform == "win32":
        # Playwright drives browsers via subprocesses, which the selector
        # event loop can't spawn on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(login(settings))
