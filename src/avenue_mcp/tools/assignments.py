"""Assignments and the unified deadline view.

`dropbox/folders/` is documented as Instructor-scope, so this module is built to
degrade: if the folder listing is denied, deadlines still come from the calendar
route, and the response says plainly what was lost rather than silently
returning less.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.context import AppContext
from avenue_mcp.errors import APIError, PermissionDeniedError
from avenue_mcp.util.dates import (
    days_until,
    describe,
    now_utc,
    parse_d2l,
    to_utc_iso,
    to_utc_param,
    within_window,
)
from avenue_mcp.util.html import to_text, truncate

log = logging.getLogger(__name__)

_INSTRUCTIONS_CAP = 2000


async def list_assignments(
    ctx: AppContext, org_unit_id: int, include_submitted: bool = True
) -> dict[str, Any]:
    await ctx.require_session()
    tz = ctx.settings.timezone

    folders: list[Any] = []
    degraded = False
    degrade_reason: str | None = None

    try:
        folders = await ctx.client.get_paged("le", f"{org_unit_id}/dropbox/folders/")
    except PermissionDeniedError as exc:
        degraded = True
        degrade_reason = (
            "The assignment folder list is instructor-only on this instance, so "
            "point values, instructions, and submission status are unavailable. "
            "Falling back to calendar-derived due dates."
        )
        log.info("dropbox folders denied for %s: %s", org_unit_id, exc)
    except APIError as exc:
        degraded = True
        degrade_reason = f"Could not read assignment folders: {exc}"

    if degraded:
        events = await _calendar_events(ctx, org_unit_id)
        items = [
            {
                "folder_id": None,
                "name": ev["title"],
                "due_date": ev["due_date"],
                "points_possible": None,
                "instructions_text": None,
                "submission_status": "unknown",
                "grade": None,
            }
            for ev in events
            if ev["kind"] in ("assignment", "unknown")
        ]
        return {
            "org_unit_id": org_unit_id,
            "course_name": await ctx.course_name(org_unit_id),
            "assignments": items,
            "count": len(items),
            "degraded": True,
            "note": degrade_reason,
        }

    assignments: list[dict[str, Any]] = []
    status_unavailable = False
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        folder_id = m.as_int(m.pick(folder, "Id", "FolderId"))
        if folder_id is None:
            continue

        due = parse_d2l(m.pick(folder, "DueDate", "Due"))
        instructions = m.pick(folder, "Instructions", default="")
        if isinstance(instructions, dict):
            instructions = m.pick(instructions, "Html", "Text", default="")
        text, _ = truncate(to_text(str(instructions or "")), _INSTRUCTIONS_CAP)

        record: dict[str, Any] = {
            "folder_id": folder_id,
            "name": m.pick(folder, "Name", "Title"),
            "due_date": describe(due, tz),
            "days_until_due": days_until(due),
            "points_possible": m.as_float(
                m.pick(folder, "OutOf", "PointsPossible", "TotalPoints")
            ),
            "instructions_text": text or None,
            "submission_status": "not_submitted",
            "submitted_at": None,
            "grade": None,
        }

        sub = await _my_submission(ctx, org_unit_id, folder_id)
        if isinstance(sub, _Unavailable):
            # Do NOT leave this at "not_submitted". Avenue refused to tell us,
            # and reporting "you haven't submitted A3" to someone who has is a
            # confidently wrong answer about a deadline -- the single most
            # damaging thing this tool could say.
            record["submission_status"] = "unknown"
            status_unavailable = True
        elif sub is not None:
            record["submission_status"] = "submitted"
            record["submitted_at"] = describe(sub["submitted_at"], tz)
            record["submitted_files"] = sub["files"]
        feedback = await _my_feedback(ctx, org_unit_id, folder_id)
        if feedback is not None:
            record["grade"] = feedback

        if record["submission_status"] == "submitted" and not include_submitted:
            continue
        assignments.append(record)

    assignments.sort(key=lambda a: (a["due_date"]["utc"] or "9999", a["name"] or ""))

    out: dict[str, Any] = {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "assignments": assignments,
        "count": len(assignments),
        "degraded": False,
        "submission_status_available": not status_unavailable,
    }
    if status_unavailable:
        out["note"] = (
            "Submission status is not available on this Avenue instance -- the "
            "learner submissions route is denied to student accounts, so every "
            "assignment shows submission_status 'unknown'. Do not tell the user "
            "they have or have not submitted anything; point them at Avenue to "
            "check. Due dates, points, and instructions above are accurate."
        )
    return out


class _Unavailable:
    """Sentinel: the submissions route could not be read.

    Distinct from None-meaning-nothing-submitted, because conflating them makes
    the tool assert "you have not submitted this" on no evidence.
    """


UNAVAILABLE = _Unavailable()


async def _my_submission(
    ctx: AppContext, org_unit_id: int, folder_id: int
) -> dict[str, Any] | _Unavailable | None:
    """The `mysubmissions` naming suggests D2L built this one for students.

    It is nonetheless **403 on avenue.cllmcmaster.ca** (measured 2026-08-07,
    docs/08), so on this instance submission status is simply not knowable.
    """
    try:
        data = await ctx.client.get(
            "le",
            f"{org_unit_id}/dropbox/folders/{folder_id}/submissions/mysubmissions/",
            # Never cached, for the same reason grades aren't: submitting and
            # then being told "not_submitted" for five minutes is the worst
            # possible moment to serve stale data.
            cache=False,
        )
    except APIError as exc:
        log.debug("mysubmissions unavailable for folder %s: %s", folder_id, exc)
        return UNAVAILABLE

    entries = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    latest: dict[str, Any] | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for sub in _submission_records(entry):
            when = parse_d2l(m.pick(sub, "SubmissionDate", "DateSubmitted", "Date"))
            if latest is None or (when and (latest["submitted_at"] is None or when > latest["submitted_at"])):
                latest = {
                    "submitted_at": when,
                    "files": [
                        m.pick(f, "FileName", "Name")
                        for f in (m.pick(sub, "Files", default=[]) or [])
                        if isinstance(f, dict)
                    ],
                }
    return latest


def _submission_records(entry: dict[str, Any]) -> list[dict[str, Any]]:
    subs = m.pick(entry, "Submissions")
    if isinstance(subs, list):
        return [s for s in subs if isinstance(s, dict)]
    return [entry]


async def _my_feedback(
    ctx: AppContext, org_unit_id: int, folder_id: int
) -> dict[str, Any] | None:
    who = await _my_user_id(ctx)
    if who is None:
        return None
    try:
        data = await ctx.client.get(
            "le", f"{org_unit_id}/dropbox/folders/{folder_id}/feedback/user/{who}"
        )
    except APIError:
        return None
    if not isinstance(data, dict):
        return None

    score = m.as_float(m.pick(data, "Score", "PointsEarned"))
    body = m.pick(data, "Feedback", default="")
    if isinstance(body, dict):
        body = m.pick(body, "Html", "Text", default="")
    text = to_text(str(body or ""))
    if score is None and not text:
        return None
    return {
        "points": score,
        "feedback_text": text or None,
        "is_released": bool(m.pick(data, "IsGraded", "Released", default=bool(score is not None))),
    }


async def _my_user_id(ctx: AppContext) -> int | None:
    """Cached on AppContext, including the failure case.

    Without caching the negative result this re-requested whoami once per
    assignment folder per course, forever, because `None` was indistinguishable
    from "not looked up yet".
    """
    if ctx.whoami_resolved:
        return ctx.whoami_id
    try:
        data = await ctx.client.get("lp", "users/whoami")
        ctx.whoami_id = m.as_int(m.pick(data, "Identifier", "UserId", "Id"))
    except APIError:
        ctx.whoami_id = None
    ctx.whoami_resolved = True
    return ctx.whoami_id


# --- unified deadline view -------------------------------------------------


async def get_upcoming_deadlines(
    ctx: AppContext, days_ahead: int = 14, include_submitted: bool = False
) -> dict[str, Any]:
    """Cross-course, time-windowed. The most common Avenue question.

    Fans out across every active course, so this is the most request-heavy read
    tool -- throttled and cached like everything else.
    """
    await ctx.require_session()
    tz = ctx.settings.timezone
    ref = now_utc()

    from avenue_mcp.tools.courses import list_courses

    course_list = (await list_courses(ctx, include_inactive=False))["courses"]

    async def gather(course: dict[str, Any]) -> list[dict[str, Any]]:
        oid = course["org_unit_id"]
        out: list[dict[str, Any]] = []
        try:
            events = await _calendar_events(ctx, oid)
        except APIError as exc:
            log.debug("calendar unavailable for %s: %s", oid, exc)
            events = []
        for ev in events:
            due = ev["due_dt"]
            if not within_window(due, days_ahead, ref):
                continue
            out.append(
                {
                    "course_name": course["name"],
                    "org_unit_id": oid,
                    "title": ev["title"],
                    "type": ev["kind"],
                    "due_date": describe(due, tz),
                    "days_until": days_until(due, ref),
                    "submission_status": "unknown",
                }
            )
        return out

    batches = await asyncio.gather(*(gather(c) for c in course_list), return_exceptions=True)
    deadlines: list[dict[str, Any]] = []
    for batch in batches:
        if isinstance(batch, list):
            deadlines.extend(batch)

    # Enrich with real submission status where the dropbox route allows it.
    enriched, any_status = await _enrich_with_assignments(
        ctx, course_list, deadlines, days_ahead, ref
    )
    deadlines = enriched

    if not include_submitted:
        deadlines = [d for d in deadlines if d["submission_status"] != "submitted"]

    deadlines.sort(key=lambda d: (d["due_date"]["utc"] or "9999", d["course_name"] or ""))

    return {
        "window_days": days_ahead,
        "generated_at": to_utc_iso(ref),
        "timezone": tz,
        "deadlines": deadlines,
        "count": len(deadlines),
        "submission_status_available": any_status,
        "note": (
            None
            if any_status
            else "Submission status is unavailable on this instance, so items you "
            "have already handed in may still appear."
        ),
    }


async def _enrich_with_assignments(
    ctx: AppContext,
    courses: list[dict[str, Any]],
    deadlines: list[dict[str, Any]],
    days_ahead: int,
    ref: Any,
) -> tuple[list[dict[str, Any]], bool]:
    any_status = False
    by_course: dict[int, list[dict[str, Any]]] = {}

    for course in courses:
        oid = course["org_unit_id"]
        try:
            folders = await ctx.client.get_paged("le", f"{oid}/dropbox/folders/")
        except APIError:
            continue
        any_status = True
        records: list[dict[str, Any]] = []
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            fid = m.as_int(m.pick(folder, "Id", "FolderId"))
            due = parse_d2l(m.pick(folder, "DueDate", "Due"))
            if fid is None or not within_window(due, days_ahead, ref):
                continue
            sub = await _my_submission(ctx, oid, fid)
            records.append(
                {
                    "title": str(m.pick(folder, "Name", "Title", default="") or ""),
                    "due_utc": to_utc_iso(due),
                    "status": "submitted" if sub else "not_submitted",
                    "points_possible": m.as_float(m.pick(folder, "OutOf", "PointsPossible")),
                    "folder_id": fid,
                }
            )
        by_course[oid] = records

    for d in deadlines:
        records = by_course.get(d["org_unit_id"], [])
        title = (d["title"] or "").strip().lower()
        for r in records:
            # Title must match. Matching on the due timestamp ALONE is wrong:
            # a quiz and an assignment in the same course routinely share an
            # 11:59 PM deadline, so the quiz inherited the assignment's
            # "submitted" status and was then filtered out of the default view
            # entirely -- the user simply never heard about the quiz.
            #
            # The timestamp is kept only as a tie-breaker for near-identical
            # titles (e.g. "A3" in the calendar vs "Assignment 3" in the folder).
            same_title = r["title"].strip().lower() == title
            same_time = bool(r["due_utc"]) and r["due_utc"] == d["due_date"]["utc"]
            if same_title or (same_time and _titles_related(r["title"], d["title"])):
                d["submission_status"] = r["status"]
                d["points_possible"] = r["points_possible"]
                d["folder_id"] = r["folder_id"]
                break

    # Any assignment the calendar missed entirely still belongs in the answer.
    known = {(d["org_unit_id"], (d["title"] or "").lower()) for d in deadlines}
    tz = ctx.settings.timezone
    for oid, records in by_course.items():
        course_name = next(
            (c["name"] for c in courses if c["org_unit_id"] == oid), None
        )
        for r in records:
            if (oid, r["title"].lower()) in known:
                continue
            due = parse_d2l(r["due_utc"])
            deadlines.append(
                {
                    "course_name": course_name,
                    "org_unit_id": oid,
                    "title": r["title"],
                    "type": "assignment",
                    "due_date": describe(due, tz),
                    "days_until": days_until(due, ref),
                    "submission_status": r["status"],
                    "points_possible": r["points_possible"],
                    "folder_id": r["folder_id"],
                }
            )

    return deadlines, any_status


def _titles_related(a: str | None, b: str | None) -> bool:
    """Do two titles plausibly name the same item?

    Used only to let a shared due-date link a calendar entry to a dropbox folder
    when the wording differs slightly. Requires a real token overlap, so a quiz
    and an assignment sharing a deadline are never conflated.
    """
    def tokens(s: str | None) -> set[str]:
        return {
            t
            for t in "".join(c if c.isalnum() else " " for c in (s or "").lower()).split()
            if len(t) > 1
        }

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    return bool(ta & tb) and not _different_kind(a, b)


def _different_kind(a: str | None, b: str | None) -> bool:
    """True when the two titles clearly name different KINDS of item."""
    quiz_words = ("quiz", "test", "exam", "midterm")
    a_l, b_l = (a or "").lower(), (b or "").lower()
    a_quiz = any(w in a_l for w in quiz_words)
    b_quiz = any(w in b_l for w in quiz_words)
    return a_quiz != b_quiz


async def _calendar_events(
    ctx: AppContext,
    org_unit_id: int,
    *,
    days_back: int = 30,
    days_ahead: int = 120,
) -> list[dict[str, Any]]:
    """Doing double duty: primary deadline source AND the fallback if the
    dropbox folder listing is instructor-only.

    `startDateTime` and `endDateTime` are REQUIRED on this route — Valence
    returns 400 without them. Calling it bare fails every time, and because
    this route is also the fallback for a blocked dropbox listing, that
    failure would look like "no deadlines" rather than "malformed request".
    """
    now = now_utc()
    window = {
        "startDateTime": to_utc_param(now - timedelta(days=days_back)),
        "endDateTime": to_utc_param(now + timedelta(days=days_ahead)),
    }
    data = await ctx.client.get_paged(
        "le", f"{org_unit_id}/calendar/events/myEvents/", params=window
    )
    out: list[dict[str, Any]] = []
    for ev in data:
        if not isinstance(ev, dict):
            continue
        title = str(m.pick(ev, "Title", "Name", default="") or "").strip()
        due = parse_d2l(
            m.pick(ev, "EndDateTime", "EndDate", "StartDateTime", "StartDate", "DueDate")
        )
        if not title or due is None:
            continue
        out.append(
            {
                "title": title,
                "due_dt": due,
                "due_date": describe(due, ctx.settings.timezone),
                "kind": _classify(ev, title),
            }
        )
    return out


def _classify(ev: dict[str, Any], title: str) -> str:
    raw = " ".join(
        str(m.pick(ev, k, default="") or "") for k in ("Type", "AssociatedEntity", "Description")
    ).lower()
    blob = f"{raw} {title.lower()}"
    if any(k in blob for k in ("quiz", "test", "exam", "midterm")):
        return "quiz"
    if any(k in blob for k in ("assignment", "dropbox", "submission", "lab", "project")):
        return "assignment"
    if "discussion" in blob:
        return "discussion"
    return "unknown"
