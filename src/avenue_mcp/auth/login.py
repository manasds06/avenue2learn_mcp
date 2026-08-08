"""Interactive login via a real browser.

University SSO means a password plus 2FA. Fully headless credential-stuffing is
not a design goal: it is fragile and it means storing a password. So we open a
real browser, let the user sign in, and keep the resulting session.

The browser is used ONLY to acquire a session. All real work afterwards goes
through httpx -- Playwright is a login mechanism, not a scraping mechanism.

Critically, this waits for a *post-login success signal* (an authenticated
Brightspace URL) rather than for specific form selectors. Selector-based waits
break every time a school restyles its SSO page -- and there is no way to write
selectors that cover every institution anyway.

That last point is what makes this file institution-agnostic: it never touches
the IdP's DOM, so it works unchanged across Microsoft Entra (McMaster), ADFS
(Carleton), Shibboleth, or anything else a school puts in front of Brightspace.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from avenue_mcp.auth.filelock import restrict_to_owner
from avenue_mcp.errors import LoginTimeoutError

log = logging.getLogger(__name__)

# Paths that only exist once Brightspace has issued a session.
_SUCCESS_PATH = re.compile(r"/d2l/(home|le/|lp/|api/)", re.IGNORECASE)
_SESSION_COOKIES = ("d2lSessionVal", "d2lSecureSessionVal")


def _looks_authenticated(url: str, host: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.netloc and parsed.netloc.lower() != host.lower():
        return False
    return bool(_SUCCESS_PATH.search(parsed.path or ""))


def interactive_login(
    base_url: str,
    session_path: Path,
    timeout_seconds: int = 300,
    headless: bool = False,
    login_url: str | None = None,
    credential_brand: str | None = None,
    lms_name: str = "Brightspace",
) -> dict[str, object]:
    """Open a browser, wait for the user to sign in, persist storage_state.

    `login_url` is where we start and `base_url` is where success is detected.
    These are not always the same host -- McMaster's flow spans three, confirmed
    by tracing it live:

        avenue.mcmaster.ca/login.php   (static landing page, 302)
          -> login.microsoftonline.com/<tenant>/saml2   (MacID + 2FA)
          -> back via RelayState
          -> avenue.cllmcmaster.ca/d2l/...   (Brightspace; cookies land here)

    Carleton stays on one host throughout, going out to ADFS at cufed.carleton.ca
    and back to brightspace.carleton.ca. Either way, waiting on the wrong host is
    why a naive flow appears to hang forever after a successful sign-in.

    `credential_brand` and `lms_name` only affect what the user is told, so a
    wrong value is cosmetic. Passed as plain strings rather than a Settings or
    Institution object to keep this module free of config imports.

    Returns the storage_state dict. Raises LoginTimeoutError if the user does
    not finish in time.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc

    host = urlparse(base_url).netloc
    entry = login_url or base_url
    deadline = time.monotonic() + timeout_seconds
    state: dict[str, object] | None = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        print(f"\nOpening {entry}")
        if credential_brand:
            print(f"Sign in with your {credential_brand} (including 2FA).")
        else:
            print("Sign in with your university account (including 2FA).")
        print(f"Waiting for a {lms_name} session on {host} ...")
        print("This window closes automatically once you're signed in.\n")
        page.goto(entry, wait_until="domcontentloaded")

        while time.monotonic() < deadline:
            # Success = session cookies scoped to the Brightspace host. Checked
            # against cookie domain rather than the current page URL, because
            # the SAML round trip can leave the tab on an intermediate host even
            # after Brightspace has issued the session.
            cookies = context.cookies()
            have_cookies = any(
                c.get("name") in _SESSION_COOKIES
                and host.endswith(str(c.get("domain", "")).lstrip("."))
                for c in cookies
            )
            if not have_cookies:
                # Fall back to a name-only check: some deployments scope cookies
                # to a parent domain.
                have_cookies = any(c.get("name") in _SESSION_COOKIES for c in cookies)

            if have_cookies:
                # Confirm by actually landing on an authenticated Brightspace
                # page, rather than trusting cookie presence alone.
                try:
                    page.goto(f"{base_url}/d2l/home", wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001 -- navigation races are fine
                    pass
                if _looks_authenticated(page.url, host):
                    state = context.storage_state()
                    break
            try:
                page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001 -- window closed by the user
                break

        try:
            context.close()
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    if state is None:
        raise LoginTimeoutError(
            "Login did not complete before the window closed or timed out."
        )

    _persist(state, session_path)
    n = len(state.get("cookies", []) or [])
    print(f"Signed in. Session saved to {session_path} ({n} cookies).")
    return state


def _persist(state: dict[str, object], path: Path) -> None:
    """Write storage_state with owner-only permissions.

    This file is equivalent to a logged-in Brightspace session. Anyone with it can
    act as you until it expires.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")

    # Create with 0600 ALREADY SET, rather than writing at the process umask
    # (typically 0644) and chmod'ing afterwards. The old order left the cookies
    # world-readable for the duration of the write.
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except (OSError, AttributeError):
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)

    try:
        os.chmod(tmp, 0o600)
    except (OSError, NotImplementedError):
        log.debug("could not chmod session file (non-POSIX filesystem?)")

    # If the rename fails, the temp file still holds the complete cookie jar.
    # Leaving it behind would strand a live credential at a path the caller
    # never hears about -- and `with_suffix` makes that path `session.tmp`, not
    # `session.json.tmp`, so a .gitignore rule written for the real name does
    # not cover it. Delete it on any failure rather than relying on that.
    try:
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass

    # Everything above is POSIX-only. On Windows chmod does not touch the ACL,
    # so without this the file keeps inheriting SYSTEM/Administrators/user
    # full control while the code above claims 0600. Inert off Windows.
    if not restrict_to_owner(path):
        if sys.platform == "win32":
            log.warning(
                "Could not restrict %s to your account. It is equivalent to a "
                "logged-in Brightspace session — check its permissions manually.",
                path,
            )
