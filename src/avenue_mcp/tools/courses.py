"""list_courses -- the entry point for almost every other tool."""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.context import AppContext
from avenue_mcp.util.dates import describe, now_utc, parse_d2l

log = logging.getLogger(__name__)


async def list_courses(ctx: AppContext, include_inactive: bool = False) -> dict[str, Any]:
    await ctx.require_session()

    entries = await ctx.client.get_paged("lp", "enrollments/myenrollments/")
    tz = ctx.settings.timezone
    now = now_utc()

    courses: list[dict[str, Any]] = []
    seen: set[int] = set()

    for entry in entries:
        org_unit = m.enrollment_org_unit(entry)
        if not isinstance(org_unit, dict):
            continue
        # Enrollments contain departments and semester containers too; without
        # this filter every result is polluted with faculty and term nodes.
        if not m.is_course_offering(org_unit):
            continue

        base = m.course_from_org_unit(org_unit)
        oid = base["org_unit_id"]
        if oid is None or oid in seen:
            continue
        seen.add(oid)

        start = parse_d2l(m.pick(org_unit, "StartDate"))
        end = parse_d2l(m.pick(org_unit, "EndDate"))
        active = m.pick(org_unit, "IsActive")

        if active is None:
            # No explicit flag: fall back to the date window, and treat a course
            # with no dates as current rather than hiding it.
            is_active = True
            if end is not None and end < now:
                is_active = False
            if start is not None and start > now:
                is_active = False
        else:
            is_active = bool(active)

        record = {
            **base,
            "is_active": is_active,
            "start_date": describe(start, tz),
            "end_date": describe(end, tz),
        }

        if is_active or include_inactive:
            courses.append(record)
        ctx.remember_course(oid, base["name"], base["code"])

    courses.sort(key=lambda c: (not c["is_active"], (c["name"] or "").lower()))

    return {
        "courses": courses,
        "count": len(courses),
        "include_inactive": include_inactive,
        "note": (
            None
            if courses
            else "No active courses found. Try include_inactive=true to see past terms."
        ),
    }
