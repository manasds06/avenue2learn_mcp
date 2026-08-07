"""list_quizzes -- metadata only, deliberately.

Names, dates, availability windows, and whether you've attempted. Quiz
*questions* and *answers* are out of scope and stay that way: knowing a quiz is
due Friday is calendar information; retrieving its contents while it is open is
the thing academic integrity policies exist to prohibit.
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.context import AppContext
from avenue_mcp.errors import APIError, PermissionDeniedError
from avenue_mcp.util.dates import days_until, describe, now_utc, parse_d2l

log = logging.getLogger(__name__)


async def list_quizzes(
    ctx: AppContext, org_unit_id: int, include_completed: bool = True
) -> dict[str, Any]:
    await ctx.require_session()
    tz = ctx.settings.timezone
    now = now_utc()

    try:
        raw = await ctx.client.get_paged("le", f"{org_unit_id}/quizzes/")
    except (PermissionDeniedError, APIError) as exc:
        log.info("quizzes route unavailable for %s: %s", org_unit_id, exc)
        return await _calendar_fallback(ctx, org_unit_id, str(exc))

    quizzes: list[dict[str, Any]] = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        quiz_id = m.as_int(m.pick(q, "QuizId", "Id"))
        if quiz_id is None:
            continue

        start = parse_d2l(m.pick(q, "StartDate", "AvailabilityStartDate"))
        due = parse_d2l(m.pick(q, "DueDate", "Due"))
        end = parse_d2l(m.pick(q, "EndDate", "AvailabilityEndDate"))

        attempts_allowed = m.as_int(
            m.pick(q, "AttemptsAllowed", "MaxAttempts", "NumberOfAttemptsAllowed")
        )
        attempts_used, best = await _attempts(ctx, org_unit_id, quiz_id)

        # Three dates, three meanings. A quiz can be submittable AFTER due but
        # BEFORE end -- telling a student it is closed when it is merely late is
        # as damaging as the reverse.
        open_now = (start is None or start <= now) and (end is None or now <= end)
        past_due_open = bool(due and now > due and (end is None or now <= end))

        record = {
            "quiz_id": quiz_id,
            "name": m.pick(q, "Name", "Title"),
            "start_date": describe(start, tz),
            "due_date": describe(due, tz),
            "end_date": describe(end, tz),
            "days_until_due": days_until(due, now),
            "attempts_allowed": attempts_allowed,
            "attempts_used": attempts_used,
            "status": "attempted" if (attempts_used or 0) > 0 else "not_attempted",
            "is_available_now": open_now,
            "is_past_due_but_open": past_due_open,
            "is_closed": bool(end and now > end),
            "best_score": best,
        }
        if record["status"] == "attempted" and not include_completed:
            continue
        quizzes.append(record)

    quizzes.sort(key=lambda x: (x["due_date"]["utc"] or "9999", x["name"] or ""))

    return {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "quizzes": quizzes,
        "count": len(quizzes),
        "degraded": False,
        "note": "Quiz metadata only -- questions and answers are never retrieved.",
    }


async def _attempts(
    ctx: AppContext, org_unit_id: int, quiz_id: int
) -> tuple[int | None, float | None]:
    try:
        data = await ctx.client.get_paged(
            "le", f"{org_unit_id}/quizzes/{quiz_id}/attempts/"
        )
    except APIError:
        return None, None

    count = 0
    best: float | None = None
    for att in data:
        if not isinstance(att, dict):
            continue
        count += 1
        score = m.as_float(m.pick(att, "Score", "PointsEarned"))
        if score is not None and (best is None or score > best):
            best = score
    return count, best


async def _calendar_fallback(
    ctx: AppContext, org_unit_id: int, reason: str
) -> dict[str, Any]:
    """Deadlines survive a blocked quizzes route; attempt status does not."""
    from avenue_mcp.tools.assignments import _calendar_events

    try:
        events = await _calendar_events(ctx, org_unit_id)
    except APIError:
        events = []

    quizzes = [
        {
            "quiz_id": None,
            "name": ev["title"],
            "due_date": ev["due_date"],
            "days_until_due": days_until(ev["due_dt"]),
            "attempts_allowed": None,
            "attempts_used": None,
            "status": "unknown",
            "is_available_now": None,
            "is_past_due_but_open": None,
        }
        for ev in events
        if ev["kind"] == "quiz"
    ]

    return {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "quizzes": quizzes,
        "count": len(quizzes),
        "degraded": True,
        "note": (
            "The quizzes API is not available to student accounts on this "
            "instance, so attempt status and availability windows are unknown. "
            f"These entries come from the course calendar. ({reason})"
        ),
    }
