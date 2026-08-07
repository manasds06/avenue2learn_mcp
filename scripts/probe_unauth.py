#!/usr/bin/env python3
"""Phase 0, the part that needs no credentials.

Several probe questions are about how Avenue behaves toward an *unauthenticated*
client, and those can be answered without a MacID:

  A1  Is /d2l/api/versions/ readable without a session? What versions?
  B3  What does an API route return with no session -- 401, 302, or HTML 200?
      (This is the load-bearing question for expiry detection.)
  B5  Does the SSO chain start at Avenue or bounce elsewhere?
  B6  Does the server care about the User-Agent?

Answering B3 here is worth real risk reduction: the client's expiry detection
assumes a 302-to-SSO or an HTML-200 rather than a clean 401, and if that guess is
wrong every expired session surfaces as a JSON parse error instead of
"run avenue-mcp login".

Run: python scripts/probe_unauth.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from avenue_mcp.auth.session import USER_AGENT  # noqa: E402
from avenue_mcp.config import get_settings  # noqa: E402

# Read from config rather than hardcoding. This script originally pointed at
# avenue.mcmaster.ca, which is the static landing page -- every /d2l/* path 404s
# there. That is exactly the finding this probe exists to have caught.
BASE = get_settings().base_url

API_ROUTES = [
    ("/d2l/api/versions/", "A1 version discovery"),
    ("/d2l/api/lp/1.0/users/whoami", "B3 API route, no session"),
    ("/d2l/api/lp/1.0/enrollments/myenrollments/", "B3 paged route, no session"),
    ("/d2l/lp/auth/xsrf-tokens", "B2 the liveness probe route"),
]


async def show(client: httpx.AsyncClient, path: str, label: str) -> None:
    try:
        r = await client.get(path)
    except httpx.HTTPError as exc:
        print(f"  {label}\n    ERROR {type(exc).__name__}: {exc}")
        return

    ctype = r.headers.get("content-type", "(none)").split(";")[0]
    loc = r.headers.get("location", "")
    print(f"  {label}")
    print(f"    {path}")
    print(f"    status={r.status_code}  content-type={ctype}")
    if loc:
        print(f"    location={loc[:120]}")

    body = r.text[:180].replace("\n", " ").strip()
    if body:
        print(f"    body[:180]={body!r}")

    # What would our client conclude?
    verdict = classify(r.status_code, ctype, loc)
    print(f"    -> client would raise: {verdict}")


def classify(status: int, ctype: str, location: str) -> str:
    """Mirror D2LClient._raise_for_session / _raise_for_status.

    Kept deliberately in step with the client: the point of this probe is to
    predict what the real code does, so a stale copy here is worse than none.
    """
    if 300 <= status < 400:
        return "SessionExpiredError (redirect)"
    if status == 200 and "text/html" in ctype:
        return "SessionExpiredError (HTML on an API route)"
    if status == 401:
        return "SessionExpiredError (401)"
    if status == 403:
        # The split that anonymous probing established: an HTML 403 is
        # Brightspace's sign-in wall, a JSON 403 is a real permission denial.
        if "html" in ctype:
            return "SessionExpiredError (403 + HTML = sign-in wall)"
        return "PermissionDeniedError (403 + JSON = genuinely not allowed)"
    if status == 404:
        return "NotFoundError"
    if status >= 500:
        return "UpstreamError"
    if status == 200:
        return "nothing -- treated as a successful response"
    return f"(unmapped status {status})"


async def main() -> int:
    print(f"Probing {BASE} with NO session (Phase 0, credential-free part)\n")

    print("=== Browser-like User-Agent, redirects NOT followed ===")
    async with httpx.AsyncClient(
        base_url=BASE,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"},
        follow_redirects=False,
        timeout=30.0,
    ) as client:
        for path, label in API_ROUTES:
            await show(client, path, label)
            print()

    print("=== B5: where does the landing page send an anonymous visitor? ===")
    async with httpx.AsyncClient(
        base_url=BASE,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        r = await client.get("/d2l/home")  # on the Brightspace host
        print(f"  /d2l/home -> final URL: {r.url}")
        print(f"  status={r.status_code}")
        print(f"  redirect chain ({len(r.history)} hop(s)):")
        for h in r.history:
            print(f"    {h.status_code} {h.url}")
        host = r.url.host
        print(f"  final host: {host}")
        if "microsoft" in host or "login" in host:
            print("  -> SSO is external (Microsoft Entra), as expected")
        elif host.endswith("mcmaster.ca"):
            print("  -> stays on a McMaster host")

    print("\n=== B6: does a default (non-browser) User-Agent behave differently? ===")
    async with httpx.AsyncClient(
        base_url=BASE, follow_redirects=False, timeout=30.0
    ) as client:
        r = await client.get("/d2l/api/versions/")
        print(f"  /d2l/api/versions/ with default httpx UA: status={r.status_code}")
        print(f"  content-type={r.headers.get('content-type', '?').split(';')[0]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
