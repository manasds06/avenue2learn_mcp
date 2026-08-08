"""Discussion tools.

Forum threads are frequently the ONLY place a clarification exists -- an
instructor answering "does Q3 want the recursive version?" in a reply is not in
the outline, not on a slide, and not in an announcement.

Author names are never returned. Posts carry `author_role` only: the role is what
determines authority, and the name is other students' personal information with
no use case here. See docs/07-risks-and-policy.md.

Read-only, permanently. Posting in a student's name to a space their classmates
read is a higher-consequence write than submitting an assignment.
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.client.roles import role_map, role_of_post
from avenue_mcp.context import AppContext
from avenue_mcp.errors import APIError
from avenue_mcp.util.dates import describe, parse_d2l
from avenue_mcp.util.html import to_text, truncate

log = logging.getLogger(__name__)

_POST_CAP = 3000


async def list_discussions(ctx: AppContext, org_unit_id: int) -> dict[str, Any]:
    await ctx.require_session()
    tz = ctx.settings.timezone

    forums_raw = await ctx.client.get_paged("le", f"{org_unit_id}/discussions/forums/")
    forums: list[dict[str, Any]] = []
    topic_count = 0

    for forum in forums_raw:
        if not isinstance(forum, dict):
            continue
        forum_id = m.as_int(m.pick(forum, "ForumId", "Id"))
        if forum_id is None:
            continue

        try:
            topics_raw = await ctx.client.get_paged(
                "le", f"{org_unit_id}/discussions/forums/{forum_id}/topics/"
            )
        except APIError as exc:
            log.debug("forum %s topics unreadable: %s", forum_id, exc)
            topics_raw = []

        topics: list[dict[str, Any]] = []
        for topic in topics_raw:
            if not isinstance(topic, dict):
                continue
            thread_id = m.as_int(m.pick(topic, "TopicId", "Id"))
            if thread_id is None:
                continue
            topic_count += 1
            last = parse_d2l(m.pick(topic, "LastPostDate", "LastPost", "LastModified"))
            topics.append(
                {
                    "topic_id": thread_id,
                    "name": m.pick(topic, "Name", "Title"),
                    "post_count": m.as_int(
                        m.pick(topic, "PostCount", "NumberOfPosts", "TotalPosts")
                    ),
                    "last_post_at": describe(last, tz),
                    # The highest-signal field here: a thread the instructor has
                    # answered is usually the authoritative one.
                    "has_instructor_replies": None,
                }
            )

        forums.append(
            {
                "forum_id": forum_id,
                "name": m.pick(forum, "Name", "Title"),
                "topics": topics,
            }
        )

    return {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "forums": forums,
        "topic_count": topic_count,
        "note": (
            "has_instructor_replies is only known after reading a thread or "
            "syncing the course. Use read_discussion_thread, or "
            "search_course_materials to search indexed threads by meaning."
        ),
    }


async def read_discussion_thread(
    ctx: AppContext,
    org_unit_id: int,
    forum_id: int,
    topic_id: int,
    max_posts: int = 50,
) -> dict[str, Any]:
    await ctx.require_session()
    tz = ctx.settings.timezone

    raw = await ctx.client.get_paged(
        "le", f"{org_unit_id}/discussions/forums/{forum_id}/topics/{topic_id}/posts/"
    )

    # Posts on some instances carry no role field, only PostingUserId. Without
    # this, every author is "Unknown" and has_instructor_replies is always false.
    # Roster is used for id -> role only; names are never read out of it.
    roles = await role_map(ctx.client, org_unit_id)

    posts: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        body = m.pick(entry, "Message", "Body", "Content", default="")
        if isinstance(body, dict):
            body = m.pick(body, "Html", "Text", "Content", default="")
        text, _ = truncate(to_text(str(body or "")), _POST_CAP)
        if not text:
            continue

        posted = parse_d2l(m.pick(entry, "DatePosted", "PostingDate", "CreatedDate"))
        posts.append(
            {
                "post_id": m.as_int(m.pick(entry, "PostId", "Id")),
                # Preserves the reply tree. Flattening destroys the
                # question -> answer pairing, which is the whole value.
                "parent_post_id": m.as_int(m.pick(entry, "ParentPostId", "ParentId")),
                "author_role": m.normalize_role(role_of_post(entry, roles)),
                "posted_at": describe(posted, tz),
                "body_text": text,
            }
        )

    posts.sort(key=lambda p: (p["posted_at"]["utc"] or "", p["post_id"] or 0))
    truncated = len(posts) > max_posts > 0
    if truncated:
        posts = posts[:max_posts]

    return {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "forum_id": forum_id,
        "topic_id": topic_id,
        "posts": posts,
        "count": len(posts),
        "truncated": truncated,
        "has_instructor_replies": any(
            p["author_role"] in ("Instructor", "TA") for p in posts
        ),
        "note": "Author names are intentionally omitted; only roles are reported.",
    }

