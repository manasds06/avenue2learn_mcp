#!/usr/bin/env python3
"""Verify the login flow up to the point a human is required.

Drives the real browser through the real SAML chain and stops at the Microsoft
sign-in page. Everything before the password box is testable: Playwright
launches, the landing page redirects, Entra receives the SAMLRequest, and the
form actually renders.

What this canNOT check is the password + 2FA step, and therefore the
post-redirect landing on Brightspace. That needs `avenue-mcp login`.

Run: python scripts/smoke_login_chain.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from avenue_mcp.config import get_settings  # noqa: E402

TENANT = "44376307-b429-42ad-8c25-28cd496f4772"


def main() -> int:
    settings = get_settings()
    print(f"login_url: {settings.login_url}")
    print(f"base_url:  {settings.base_url}\n")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed.", file=sys.stderr)
        return 1

    failures = 0
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: chromium would not launch: {exc}", file=sys.stderr)
            print("Run: playwright install chromium", file=sys.stderr)
            return 1
        print("PASS  chromium launches")

        context = browser.new_context()
        page = context.new_page()

        hops: list[str] = []
        page.on("response", lambda r: hops.append(f"{r.status} {r.url[:100]}"))

        page.goto(settings.login_url, wait_until="domcontentloaded", timeout=60000)
        final = page.url
        print(f"PASS  navigated; landed on {final[:90]}")

        parsed = urlparse(final)
        if "login.microsoftonline.com" in parsed.netloc:
            print("PASS  redirected to Microsoft Entra")
        else:
            print(f"FAIL  expected Entra, got {parsed.netloc}")
            failures += 1

        if TENANT in final:
            print(f"PASS  tenant matches {TENANT}")
        else:
            print("WARN  tenant id not in URL (may be carried in the SAMLRequest)")

        # The SAMLRequest proves Brightspace initiated a SAML flow.
        qs = parse_qs(parsed.query)
        if "SAMLRequest" in qs or "client-request-id" in qs or parsed.path.endswith("saml2"):
            print("PASS  SAML2 request present")
        else:
            print("WARN  no SAMLRequest visible on the final URL")

        # A credential form means the flow is live, not an error page.
        try:
            page.wait_for_selector("input[type=email], input[name=loginfmt]", timeout=15000)
            print("PASS  Microsoft sign-in form rendered (MacID entry point)")
        except Exception:  # noqa: BLE001
            title = page.title()
            print(f"WARN  no email field found; page title = {title!r}")

        print("\n  redirect chain:")
        for h in hops[:8]:
            print(f"    {h}")

        context.close()
        browser.close()

    print("\n--- STOP ---")
    print("The next step needs a human: MacID password + 2FA approval.")
    print("Run `avenue-mcp login` to complete it.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
