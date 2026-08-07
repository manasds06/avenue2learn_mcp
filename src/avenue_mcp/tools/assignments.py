"""list_assignments and get_upcoming_deadlines (docs/03-mcp-tools.md).

get_upcoming_deadlines uses the CROSS-COURSE calendar route
(`GET /le/{v}/calendar/events/myEvents/?orgUnitIdsCSV=...`), which answers for
every course in one request. An earlier design fanned out one call per course
and was described as "the most request-heavy read tool"; it isn't one.

Both calendar variants require startDateTime and endDateTime. A bare call
returns 400, so `days_ahead` is converted into an explicit window rather than
passed through.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.errors import PermissionDeniedError
from avenue_mcp.tools.courses import list_courses
from avenue_mcp.util.dates import (
    days_until,
    describe,
    parse_d2l_date,
    to_utc_param,
    utcnow,
)
from avenue_mcp.util.html import html_to_text

log = logging.getLogger(__name__)


async def _my_submissions(
    client: D2LClient, org_unit_id: int, folder_id: int
) -> list[dict[str, Any]]:
    """The learner-scoped submissions route. Degrades quietly to empty."""
    try:
        raw = await client.get_json(
            "le", f"{org_unit_id}/dropbox/folders/{folder_id}/submissions/mysubmissions/"
        )
    except Exception as exc:  # noqa: BLE001 - one folder must not kill the list
        log.debug("mysubmissions failed for folder %s: %s", folder_id, exc)
        return []
    return [s for s in (raw if isinstance(raw, list) else []) if isinstance(s, dict)]


async def list_assignments(
    client: D2LClient,
    *,
    org_unit_id: int,
    include_submitted: bool = True,
) -> dict[str, Any]:
    """A course's assignments with due dates, points, and submission status."""
    tz = client.settings.timezone

    try:
        folders = await client.get_json("le", f"{org_unit_id}/dropbox/folders/")
    except PermissionDeniedError:
        # The honest degraded shape: say what happened and point at the tool
        # that still works, rather than returning an empty list that reads
        # as "this course has no assignments".
        return {
            "org_unit_id": org_unit_id,
            "assignments": [],
            "count": 0,
            "assignments_available": False,
            "note": (
                "Assignment folders are not accessible from your account on this "
                "Avenue instance. Due dates may still be available via "
                "get_upcoming_deadlines, which reads the course calendar."
            ),
        }

    assignments: list[dict[str, Any]] = []
    for folder in folders if isinstance(folders, list) else []:
        if not isinstance(folder, dict):
            continue

        folder_id = folder.get("Id")
        due = parse_d2l_date(folder.get("DueDate"))

        submissions = (
            await _my_submissions(client, org_unit_id, int(folder_id))
            if folder_id is not None
            else []
        )
        submitted_at = None
        for submission in submissions:
            stamp = parse_d2l_date(submission.get("SubmissionDate"))
            if stamp and (submitted_at is None or stamp > submitted_at):
                submitted_at = stamp

        status = "submitted" if submissions else "not_submitted"
        if not include_submitted and status == "submitted":
            continue

        instructions = (folder.get("Instructions") or {}).get("Html") or ""

        assignments.append(
            {
                "folder_id": folder_id,
                "name": folder.get("Name") or "",
                "due_date": describe(due, tz),
                "days_until_due": days_until(due),
                "points_possible": folder.get("Assessment", {}).get("ScoreDenominator")
                if isinstance(folder.get("Assessment"), dict)
                else None,
                "instructions_text": html_to_text(instructions) or None,
                "submission_status": status,
                "submitted_at": describe(submitted_at, tz),
                "submission_count": len(submissions),
            }
        )

    assignments.sort(key=lambda a: (a["due_date"] or {}).get("utc") or "9999")
    return {
        "org_unit_id": org_unit_id,
        "assignments": assignments,
        "count": len(assignments),
        "assignments_available": True,
    }


def _event_type(event: dict[str, Any]) -> str:
    """Best-effort classification of a calendar event."""
    raw = " ".join(
        str(event.get(k) or "")
        for k in ("AssociationType", "EventType", "Title", "Description")
    ).lower()
    if "dropbox" in raw or "assignment" in raw:
        return "assignment"
    if "quiz" in raw or "test" in raw or "exam" in raw or "midterm" in raw:
        return "quiz"
    if "discussion" in raw:
        return "discussion"
    return "event"


async def get_upcoming_deadlines(
    client: D2LClient,
    *,
    days_ahead: int = 14,
    include_submitted: bool = False,
) -> dict[str, Any]:
    """Dated items across all active courses, soonest first."""
    tz = client.settings.timezone
    now = utcnow()
    window_end = now + timedelta(days=days_ahead)

    courses = (await list_courses(client))["courses"]
    if not courses:
        return {
            "window_days": days_ahead,
            "generated_at": describe(now, tz),
            "deadlines": [],
            "count": 0,
            "note": "No active courses found.",
        }

    names = {int(c["org_unit_id"]): c["name"] for c in courses}
    params = {
        "orgUnitIdsCSV": ",".join(str(i) for i in names),
        "startDateTime": to_utc_param(now),
        "endDateTime": to_utc_param(window_end),
    }

    # One request for every course. Falls back to per-course calls only if the
    # cross-course variant isn't available on this instance.
    try:
        events = await client.get_json("le", "calendar/events/myEvents/", params=params)
        source = "cross-course"
    except Exception as exc:  # noqa: BLE001
        log.info("Cross-course calendar unavailable (%s); falling back per course.", exc)
        events = []
        source = "per-course fallback"
        per_course = {
            "startDateTime": params["startDateTime"],
            "endDateTime": params["endDateTime"],
        }
        for org_unit_id in names:
            try:
                got = await client.get_json(
                    "le", f"{org_unit_id}/calendar/events/myEvents/", params=per_course
                )
            except Exception as inner:  # noqa: BLE001
                log.debug("Calendar failed for %s: %s", org_unit_id, inner)
                continue
            for event in got if isinstance(got, list) else []:
                if isinstance(event, dict):
                    event.setdefault("OrgUnitId", org_unit_id)
                    events.append(event)

    deadlines: list[dict[str, Any]] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        due = parse_d2l_date(event.get("EndDateTime") or event.get("StartDateTime"))
        if due is None or due < now or due > window_end:
            continue

        org_unit_id = event.get("OrgUnitId") or event.get("OrgUnitIdentifier")
        try:
            org_unit_id = int(org_unit_id) if org_unit_id is not None else None
        except (TypeError, ValueError):
            org_unit_id = None

        deadlines.append(
            {
                "title": event.get("Title") or "",
                "course_name": names.get(org_unit_id, "") if org_unit_id else "",
                "org_unit_id": org_unit_id,
                "due_date": describe(due, tz),
                "days_until": days_until(due, now=now),
                "type": _event_type(event),
                "description": html_to_text(
                    (event.get("Description") or {}).get("Html")
                    if isinstance(event.get("Description"), dict)
                    else event.get("Description")
                )
                or None,
            }
        )

    deadlines.sort(key=lambda d: (d["due_date"] or {}).get("utc") or "")

    return {
        "window_days": days_ahead,
        "generated_at": describe(now, tz),
        "courses_covered": len(names),
        "source": source,
        "deadlines": deadlines,
        "count": len(deadlines),
        "note": (
            "Reflects the course calendar. Items your instructor didn't put on the "
            "calendar won't appear here."
        ),
    }
