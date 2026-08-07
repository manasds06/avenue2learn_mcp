"""Phase 0 probe (docs/06-roadmap.md, docs/08-api-probe-results.md).

Throwaway diagnostic, not shipped and not polished. It answers two questions:

  B0. Which auth strategy does Avenue accept — cookies, a bearer minted from
      the session, or only a bearer captured from frontend traffic? This
      decides whether the browser is a one-time login or an hourly runtime
      dependency, so it runs first and everything else depends on it.

  C*. Which of the uncertain routes can a student account actually reach?

Direction matters: probe -> docs, never docs -> probe. If a route is blocked,
the tool description changes to match. Reinterpreting a 403 to preserve a
planned feature is the failure mode this exists to prevent.

Run:  python -m avenue_mcp probe        (after `python -m avenue_mcp login`)
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from avenue_mcp.auth.bearer import TOKEN_PATH, XSRF_PATH, BearerTokenAuth, fetch_xsrf_token
from avenue_mcp.auth.filelock import describe_permissions
from avenue_mcp.auth.session import CookieSessionAuth, SessionManager
from avenue_mcp.client.d2l import VERSIONS_PATH, D2LClient
from avenue_mcp.client.paging import extract_items, paging_info
from avenue_mcp.config import get_settings
from avenue_mcp.errors import AvenueMCPError, NoSessionError
from avenue_mcp.util.dates import to_utc_param, utcnow

FIXTURE_DIR = Path("tests/fixtures")

# Fields that must never reach a committed file. Raw bodies go to the
# gitignored fixture dir; only shapes and status codes get summarised.
_PII_KEYS = {
    "emailaddress", "email", "username", "orgdefinedid", "firstname",
    "lastname", "displayname", "profileidentifier", "userid", "identifier",
}


@dataclass
class Probe:
    """One route's result."""

    label: str
    method: str
    path: str
    status: int | None = None
    content_type: str | None = None
    ok: bool = False
    item_count: int | None = None
    keys: list[str] = field(default_factory=list)
    paged: bool | None = None
    note: str = ""
    error: str = ""

    def verdict(self) -> str:
        if self.ok:
            return "WORKS"
        if self.status == 403:
            return "BLOCKED (403)"
        if self.status == 404:
            return "NOT FOUND (404) — check the path before concluding 'blocked'"
        if self.status == 400:
            return "BAD REQUEST (400) — likely missing a required parameter"
        if self.status is None:
            return f"ERROR: {self.error}"
        return f"HTTP {self.status}"


def shape_of(payload: Any, depth: int = 0) -> Any:
    """Describe a response's structure without its values."""
    if depth > 3:
        return "..."
    if isinstance(payload, dict):
        return {k: shape_of(v, depth + 1) for k, v in list(payload.items())[:25]}
    if isinstance(payload, list):
        return [shape_of(payload[0], depth + 1)] if payload else []
    return type(payload).__name__


def redact(payload: Any) -> Any:
    """Strip obvious PII so a sample can be pasted into docs/08."""
    if isinstance(payload, dict):
        return {
            k: ("<REDACTED>" if k.lower() in _PII_KEYS else redact(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [redact(v) for v in payload[:2]]
    return payload


class Prober:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.session = SessionManager(self.settings)
        self.client = D2LClient(self.session, self.settings)
        self.results: list[Probe] = []
        self.strategy: str = "unresolved"
        self.versions: dict[str, str] = {}
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    def _save(self, label: str, payload: Any) -> None:
        """Raw response to the gitignored fixture dir — the Phase 1-3 test corpus."""
        safe = label.replace("/", "_").replace(" ", "_")
        (FIXTURE_DIR / f"{safe}.json").write_text(
            json.dumps(payload, indent=2)[:2_000_000], encoding="utf-8"
        )

    async def probe(
        self,
        label: str,
        component: str,
        route: str,
        *,
        params: dict[str, Any] | None = None,
        note: str = "",
    ) -> Probe:
        result = Probe(label=label, method="GET", path=f"/d2l/api/{component}/{{v}}/{route}", note=note)
        try:
            path = await self.client.path(component, route)
            result.path = path
            http, provider = await self.session.require_session()
            response = await self.client._send(http, "GET", path, provider=provider, params=params)
            result.status = response.status_code
            result.content_type = response.headers.get("content-type", "")

            if response.status_code == 200 and "json" in (result.content_type or ""):
                payload = response.json()
                result.ok = True
                items = extract_items(payload)
                result.item_count = len(items) if items or isinstance(payload, list) else None
                bookmark, has_more = paging_info(payload)
                result.paged = bookmark is not None or has_more
                sample = items[0] if items else payload
                if isinstance(sample, dict):
                    result.keys = sorted(sample.keys())
                self._save(label, redact(payload))
        except AvenueMCPError as exc:
            result.error = f"{type(exc).__name__}: {exc.message}"
        except httpx.HTTPError as exc:
            result.error = f"network: {exc}"
        except Exception as exc:  # a probe must never die on one bad route
            result.error = f"{type(exc).__name__}: {exc}"

        self.results.append(result)
        print(f"  {result.verdict():<24} {label}")
        return result

    # --- B0: the question that gates the design --------------------------

    async def probe_auth(self) -> None:
        print("\n=== B0. Auth strategy (run first — gates everything) ===\n")

        http = await self.session.client()

        # Test 1: cookies alone.
        cookie_auth = CookieSessionAuth()
        cookies_work = await cookie_auth.is_alive(http)
        print(f"  cookies alone -> whoami      : {'WORKS' if cookies_work else 'rejected'}")

        # Test 2: mint a bearer from the session.
        xsrf = await fetch_xsrf_token(http)
        print(f"  GET {XSRF_PATH:<28}: {'token received' if xsrf else 'no token'}")

        minted = BearerTokenAuth()
        token = await minted.mint(http)
        print(f"  POST {TOKEN_PATH:<27}: {'minted a bearer' if token else 'failed'}")

        # Test 3: a bearer captured during login.
        captured = self.session._load_captured_bearer()
        captured_works = bool(captured) and await captured.is_alive(http)
        print(
            "  captured bearer from login   : "
            + ("present and valid" if captured_works else "absent or expired")
        )

        if cookies_work:
            self.strategy = "CookieSessionAuth"
        elif token:
            self.strategy = "BearerTokenAuth"
        elif captured_works:
            self.strategy = "CapturedBearerAuth"
        else:
            self.strategy = "NONE — all three rejected"

        print(f"\n  VERDICT: {self.strategy}")
        if self.strategy == "CapturedBearerAuth":
            print(
                "  ** The browser is an ~hourly runtime dependency. Update the\n"
                "     README, docs/01's lifetime table, and Phase 1's 'no browser'\n"
                "     exit criterion to match."
            )
        elif self.strategy.startswith(("Cookie", "Bearer")):
            print("  ** The browser stays a one-time login step. Re-login roughly daily.")

    # --- Everything else --------------------------------------------------

    async def probe_bootstrap(self) -> None:
        print("\n=== A. Bootstrap ===\n")
        self.versions = await self.client.negotiate_versions()
        print(f"  versions negotiated          : {self.versions}")
        await self.probe("A2-whoami", "lp", "users/whoami")

    async def probe_courses(self) -> list[dict[str, Any]]:
        print("\n=== C1-C2. Courses ===\n")
        # Server-side filtering: no client-side sifting, no N+1 enrichment.
        result = await self.probe(
            "C1-myenrollments",
            "lp",
            "enrollments/myenrollments/",
            params={"isActive": "true", "canAccess": "true"},
            note="server-side filtered",
        )
        if not result.ok:
            return []

        raw = json.loads((FIXTURE_DIR / "C1-myenrollments.json").read_text(encoding="utf-8"))
        courses: list[dict[str, Any]] = []
        for item in extract_items(raw):
            org = (item or {}).get("OrgUnit") or {}
            if org.get("Id"):
                courses.append(
                    {
                        "id": org["Id"],
                        "name": org.get("Name", ""),
                        "type": (org.get("Type") or {}).get("Name", ""),
                    }
                )
        print(f"  active enrollments returned  : {len(courses)}")
        types = sorted({c["type"] for c in courses})
        print(f"  org unit types present       : {types}")
        return courses

    async def probe_course_routes(self, org_unit_id: int) -> None:
        print(f"\n=== C3-C16. Course-scoped routes (org unit {org_unit_id}) ===\n")

        await self.probe("C2-course-details", "lp", f"courses/{org_unit_id}")
        await self.probe("C3-content-root", "le", f"{org_unit_id}/content/root/")
        await self.probe("C7-dropbox-folders", "le", f"{org_unit_id}/dropbox/folders/",
                         note="Instructor-scope claim withdrawn; expected to work")
        await self.probe("C11-myGradeValues", "le", f"{org_unit_id}/grades/values/myGradeValues/")
        await self.probe("C12-grade-structure", "le", f"{org_unit_id}/grades/",
                         note="gates the grade PROJECTION only")
        await self.probe("C13-news", "le", f"{org_unit_id}/news/")

        # Calendar: startDateTime/endDateTime are REQUIRED. A bare call 400s,
        # and that must not be recorded as "blocked".
        now = utcnow()
        window = {
            "startDateTime": to_utc_param(now - timedelta(days=30)),
            "endDateTime": to_utc_param(now + timedelta(days=90)),
        }
        await self.probe("C14a-calendar-per-course", "le",
                         f"{org_unit_id}/calendar/events/myEvents/", params=window)

        # Classlist lives under le, not lp. A 404 here means a wrong path.
        await self.probe("C15-classlist", "le", f"{org_unit_id}/classlist/")
        await self.probe("C15b-classlist-paged", "le", f"{org_unit_id}/classlist/paged/")
        await self.probe("C16-role-enrollments", "lp",
                         f"enrollments/orgUnits/{org_unit_id}/users/")

    async def probe_cross_course_calendar(self, course_ids: list[int]) -> None:
        """The route that turns an N-request fan-out into one call."""
        if not course_ids:
            return
        print("\n=== C14b. Cross-course calendar (one request for all courses) ===\n")
        now = utcnow()
        await self.probe(
            "C14b-calendar-cross-course",
            "le",
            "calendar/events/myEvents/",
            params={
                "orgUnitIdsCSV": ",".join(str(i) for i in course_ids),
                "startDateTime": to_utc_param(now - timedelta(days=30)),
                "endDateTime": to_utc_param(now + timedelta(days=90)),
            },
            note="replaces per-course fan-out",
        )

    def report(self) -> None:
        print("\n" + "=" * 70)
        print("SUMMARY — transcribe into docs/08-api-probe-results.md")
        print("=" * 70)
        print(f"\nAuth strategy : {self.strategy}")
        print(f"API versions  : {self.versions}")
        print(f"Session file  : {describe_permissions(self.settings.session_path)[:200]}")
        print(f"\nFixtures saved to {FIXTURE_DIR}/ (gitignored)\n")

        print(f"{'ROUTE':<30} {'STATUS':<28} ITEMS")
        print("-" * 70)
        for r in self.results:
            count = "" if r.item_count is None else str(r.item_count)
            print(f"{r.label:<30} {r.verdict():<28} {count}")

        blocked = [r for r in self.results if not r.ok]
        if blocked:
            print("\nNot working — decide the honest degraded shape for each:")
            for r in blocked:
                print(f"  - {r.label}: {r.verdict()}")
                if r.note:
                    print(f"      note: {r.note}")

        print("\nDo not adjust a tool's promises to fit a hoped-for reading of these.")
        print("Update the docs from the results, not the other way around.\n")

    async def run(self) -> int:
        if not self.session.has_session():
            print("Not logged in. Run `python -m avenue_mcp login` first.", file=sys.stderr)
            return 1

        try:
            await self.probe_auth()
            await self.probe_bootstrap()
            courses = await self.probe_courses()

            if courses:
                await self.probe_course_routes(int(courses[0]["id"]))
                await self.probe_cross_course_calendar([int(c["id"]) for c in courses])
            else:
                print("\nNo active courses returned — skipping course-scoped probes.")

            self.report()
            return 0
        except NoSessionError as exc:
            print(f"\n{exc.to_text()}", file=sys.stderr)
            return 1
        finally:
            await self.client.aclose()


async def main_async() -> int:
    return await Prober().run()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
