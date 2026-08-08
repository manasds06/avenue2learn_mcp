"""get_whats_new -- the watermark digest.

"What did I miss?" is the question a student actually asks most, and answering it
by polling each course tool separately is slow and easy to get wrong.

Watermarks are per-course AND per-category. One global timestamp breaks the
moment you sync one course and not another: the un-synced one either floods the
next digest or silently loses changes.

`mark_seen=False` matters more than it looks -- a user asking "what's new?" twice
in one conversation should get the same answer both times, not an empty second
response because the first call consumed the watermark.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.context import AppContext
from avenue_mcp.errors import PermissionDeniedError
from avenue_mcp.util.dates import describe, now_utc, parse_d2l, to_utc_iso, within_window
from avenue_mcp.util.html import to_text, truncate

log = logging.getLogger(__name__)

CATEGORIES = ("announcements", "files", "grades", "discussions", "deadlines")
DEFAULT_LOOKBACK_DAYS = 7
_SNIPPET = 400


async def get_whats_new(
    ctx: AppContext,
    since: str | None = None,
    mark_seen: bool = True,
    org_unit_id: int | None = None,
) -> dict[str, Any]:
    await ctx.require_session()
    tz = ctx.settings.timezone
    ref = now_utc()

    from avenue_mcp.tools.courses import list_courses

    courses = (await list_courses(ctx, include_inactive=False))["courses"]
    if org_unit_id is not None:
        courses = [c for c in courses if c["org_unit_id"] == org_unit_id]

    override = parse_d2l(since) if since else None
    fallback = to_utc_iso(ref.replace(microsecond=0)) or ""

    changes: dict[str, list[dict[str, Any]]] = {
        "announcements": [],
        "new_files": [],
        "new_grades": [],
        "discussion_replies": [],
        "newly_open": [],
    }
    advanced: list[dict[str, str]] = []

    # Determined BEFORE anything writes a watermark -- computing it afterwards
    # meant the "first run" note could never fire on an actual first run.
    first_run = not override and all(
        ctx.store.get_watermark(c["org_unit_id"], "announcements") is None
        for c in courses
    )

    failures: list[dict[str, str]] = []

    async def per_course(course: dict[str, Any]) -> None:
        oid = course["org_unit_id"]
        name = course["name"]

        marks = {
            cat: (
                to_utc_iso(override)
                if override
                else ctx.store.get_watermark(oid, cat)
            )
            for cat in CATEGORIES
        }

        jobs = {
            "announcements": _announcements(ctx, oid, name, marks["announcements"], changes, tz),
            "files": _files(ctx, oid, name, marks["files"], changes, tz),
            "grades": _grades(ctx, oid, name, marks["grades"], changes, mark_seen),
            "discussions": _discussions(ctx, oid, name, marks["discussions"], changes, tz),
            "deadlines": _deadlines(ctx, oid, name, marks["deadlines"], changes, tz, ref),
        }
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)

        # A watermark is advanced ONLY for a category that actually succeeded.
        #
        # Previously every exception was discarded and all five watermarks moved
        # forward regardless, so a session expiring mid-digest returned
        # "nothing new" AND permanently skipped past the changes it never read.
        # Losing data silently is far worse than reporting an error.
        for category, result in zip(jobs, results):
            if isinstance(result, BaseException):
                failures.append(
                    {
                        "course_name": name or str(oid),
                        "category": category,
                        "error": type(result).__name__,
                        "message": str(result)[:200],
                    }
                )
                continue
            if mark_seen:
                ctx.store.set_watermark(oid, category, fallback)
                advanced.append({"org_unit_id": str(oid), "category": category})

    outcomes = await asyncio.gather(
        *(per_course(c) for c in courses), return_exceptions=True
    )
    for course, outcome in zip(courses, outcomes):
        if isinstance(outcome, BaseException):
            failures.append(
                {
                    "course_name": course.get("name") or str(course.get("org_unit_id")),
                    "category": "course",
                    "error": type(outcome).__name__,
                    "message": str(outcome)[:200],
                }
            )

    total = sum(len(v) for v in changes.values())

    notes: list[str] = []
    if first_run:
        notes.append(
            "First run: no watermark existed, so this reports roughly the last "
            f"{DEFAULT_LOOKBACK_DAYS} days. Later calls report only what is new."
        )
    if failures:
        notes.append(
            f"{len(failures)} source(s) could not be read, so this digest is "
            "INCOMPLETE -- see `failures`. Their watermarks were left untouched, "
            "so nothing has been skipped permanently."
        )
        if any(f["error"] in ("SessionExpiredError", "NoSessionError") for f in failures):
            notes.append("The Brightspace session looks invalid. Run `avenue-mcp login`.")

    return {
        "since": to_utc_iso(override) if override else "per-course watermarks",
        "generated_at": to_utc_iso(ref),
        "timezone": tz,
        "courses_checked": len(courses),
        "changes": changes,
        "total_changes": total,
        "watermark_advanced": mark_seen and not failures,
        "complete": not failures,
        "failures": failures,
        "note": " ".join(notes) or None,
    }


def _is_new(when: Any, mark: str | None) -> bool:
    """No watermark yet -> fall back to a short lookback rather than dumping a
    term's worth of history into the first digest."""
    if when is None:
        return False
    stamp = to_utc_iso(when)
    if stamp is None:
        return False
    if mark:
        return stamp > mark
    from avenue_mcp.util.dates import days_until

    age = days_until(when)
    return age is not None and age >= -DEFAULT_LOOKBACK_DAYS


async def _announcements(
    ctx: AppContext, oid: int, course: str | None, mark: str | None,
    changes: dict[str, list[dict[str, Any]]], tz: str,
) -> None:
    try:
        raw = await ctx.client.get_paged("le", f"{oid}/news/")
    except PermissionDeniedError as exc:
        log.debug("news unavailable for %s: %s", oid, exc)
        return
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        posted = parse_d2l(m.pick(entry, "StartDate", "DatePosted", "CreatedDate"))
        if not _is_new(posted, mark):
            continue
        body = m.pick(entry, "Body", "Content", default="")
        if isinstance(body, dict):
            body = m.pick(body, "Html", "Text", default="")
        snippet, _ = truncate(to_text(str(body or "")), _SNIPPET)
        changes["announcements"].append(
            {
                "course_name": course,
                "org_unit_id": oid,
                "title": m.pick(entry, "Title", "Subject"),
                "posted_at": describe(posted, tz),
                "snippet": snippet or None,
            }
        )


async def _files(
    ctx: AppContext, oid: int, course: str | None, mark: str | None,
    changes: dict[str, list[dict[str, Any]]], tz: str,
) -> None:
    try:
        topics = await ctx.syncer.discover_files(oid)
    except PermissionDeniedError as exc:
        log.debug("content unavailable for %s: %s", oid, exc)
        return
    for topic in topics:
        modified = parse_d2l(topic.get("last_modified"))
        if not _is_new(modified, mark):
            continue
        indexed = ctx.store.get_file_doc(oid, topic["topic_id"]) is not None
        changes["new_files"].append(
            {
                "course_name": course,
                "org_unit_id": oid,
                "file_name": topic.get("file_name") or topic.get("title"),
                "module_path": topic.get("module_path"),
                "topic_id": topic["topic_id"],
                "last_modified": describe(modified, tz),
                # Lets the model proactively offer a re-sync so the file becomes
                # searchable, closing the freshness loop on demand.
                "indexed": indexed,
            }
        )


async def _grades(
    ctx: AppContext, oid: int, course: str | None, mark: str | None,
    changes: dict[str, list[dict[str, Any]]], mark_seen: bool = True,
) -> None:
    """Grades are not cached and not diffed by timestamp -- the API gives no
    reliable "graded at". We report newly-populated items instead.

    `mark_seen` is honoured here, not just by the caller. These per-item
    watermarks are a second, finer-grained store, and writing them during a
    peek made `mark_seen=False` violate its own contract: a second call in the
    same conversation reported zero new grades because the "peek" had already
    consumed them.
    """
    from avenue_mcp.tools.grades import _grade_values

    try:
        values = await _grade_values(ctx, oid)
    except PermissionDeniedError as exc:
        log.debug("grades unavailable for %s: %s", oid, exc)
        return

    for val in values:
        earned = m.as_float(m.pick(val, "PointsNumerator", "PointsEarned"))
        if earned is None:
            continue
        key = f"grade:{m.pick(val, 'GradeObjectIdentifier', 'GradeObjectId', default='?')}"
        seen = ctx.store.get_watermark(oid, key)
        stamp = f"{earned}"
        if seen == stamp:
            continue
        if mark_seen:
            ctx.store.set_watermark(oid, key, stamp)
        if seen is None and mark is None:
            continue  # first-ever run: do not report the whole gradebook
        changes["new_grades"].append(
            {
                "course_name": course,
                "org_unit_id": oid,
                "item_name": m.pick(val, "GradeObjectName", "Name"),
                "points_earned": earned,
                "points_possible": m.as_float(
                    m.pick(val, "PointsDenominator", "PointsPossible")
                ),
            }
        )


async def _discussions(
    ctx: AppContext, oid: int, course: str | None, mark: str | None,
    changes: dict[str, list[dict[str, Any]]], tz: str,
) -> None:
    try:
        forums = await ctx.client.get_paged("le", f"{oid}/discussions/forums/")
    except PermissionDeniedError as exc:
        log.debug("discussions unavailable for %s: %s", oid, exc)
        return

    for forum in forums:
        if not isinstance(forum, dict):
            continue
        forum_id = m.as_int(m.pick(forum, "ForumId", "Id"))
        if forum_id is None:
            continue
        try:
            topics = await ctx.client.get_paged(
                "le", f"{oid}/discussions/forums/{forum_id}/topics/"
            )
        except PermissionDeniedError:
            continue
        for topic in topics:
            if not isinstance(topic, dict):
                continue
            last = parse_d2l(m.pick(topic, "LastPostDate", "LastPost", "LastModified"))
            if not _is_new(last, mark):
                continue
            changes["discussion_replies"].append(
                {
                    "course_name": course,
                    "org_unit_id": oid,
                    "forum_id": forum_id,
                    "topic_id": m.as_int(m.pick(topic, "TopicId", "Id")),
                    "topic_name": m.pick(topic, "Name", "Title"),
                    "last_post_at": describe(last, tz),
                    "post_count": m.as_int(m.pick(topic, "PostCount", "NumberOfPosts")),
                }
            )


async def _deadlines(
    ctx: AppContext, oid: int, course: str | None, mark: str | None,
    changes: dict[str, list[dict[str, Any]]], tz: str, ref: Any,
) -> None:
    """Items that have newly opened or become due soon."""
    from avenue_mcp.tools.assignments import _calendar_events

    try:
        events = await _calendar_events(ctx, oid)
    except PermissionDeniedError:
        return
    for ev in events:
        if not within_window(ev["due_dt"], 7, ref):
            continue
        changes["newly_open"].append(
            {
                "course_name": course,
                "org_unit_id": oid,
                "title": ev["title"],
                "type": ev["kind"],
                "due_date": ev["due_date"],
            }
        )
