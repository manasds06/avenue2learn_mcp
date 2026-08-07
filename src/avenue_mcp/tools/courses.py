"""list_courses (docs/03-mcp-tools.md).

The root tool: every course-scoped tool needs an org_unit_id and this is where
one comes from.

Note what this deliberately does NOT do: enrich each course through
`GET /lp/{v}/courses/{id}`. `myenrollments` already returns start/end/active in
its Access block, so enrichment would be an N+1 against data we were handed.
"""

from __future__ import annotations

from typing import Any

from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.util.dates import describe, parse_d2l_date

# Enrollments include departments and semester containers as well as actual
# courses. Only offerings are useful; the rest is noise in every result.
_COURSE_OFFERING = "Course Offering"


def _normalize(entry: dict[str, Any], tz: str) -> dict[str, Any] | None:
    org = entry.get("OrgUnit") or {}
    org_id = org.get("Id")
    if org_id is None:
        return None

    access = entry.get("Access") or {}
    return {
        "org_unit_id": org_id,
        "name": org.get("Name") or "",
        "code": org.get("Code") or "",
        "type": (org.get("Type") or {}).get("Name") or "",
        "start_date": describe(parse_d2l_date(access.get("StartDate")), tz),
        "end_date": describe(parse_d2l_date(access.get("EndDate")), tz),
        "is_active": bool(access.get("IsActive", False)),
        "can_access": bool(access.get("CanAccess", True)),
    }


async def list_courses(
    client: D2LClient,
    *,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Return the caller's course offerings."""
    tz = client.settings.timezone

    # Filter server-side where the API supports it — cheaper than pulling
    # every past term and sifting locally.
    params: dict[str, Any] = {}
    if not include_inactive:
        params["isActive"] = "true"
        params["canAccess"] = "true"

    entries = await client.get_paged("lp", "enrollments/myenrollments/", params=params)

    courses: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        course = _normalize(entry, tz)
        if course is None:
            continue
        if course["type"] and course["type"] != _COURSE_OFFERING:
            continue
        # isActive is a server-side hint, not a guarantee across instances;
        # re-check so include_inactive=False means what it says.
        if not include_inactive and not course["is_active"]:
            continue
        courses.append(course)

    courses.sort(key=lambda c: (c["name"] or "").lower())
    return {
        "courses": courses,
        "count": len(courses),
        "include_inactive": include_inactive,
    }
