"""list_announcements -- course news."""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.context import AppContext
from avenue_mcp.util.dates import describe, now_utc, parse_d2l
from avenue_mcp.util.html import to_text_and_links, truncate

log = logging.getLogger(__name__)

_BODY_CAP = 4000


async def list_announcements(
    ctx: AppContext,
    org_unit_id: int,
    limit: int = 20,
    since: str | None = None,
) -> dict[str, Any]:
    await ctx.require_session()
    tz = ctx.settings.timezone
    now = now_utc()
    cutoff = parse_d2l(since) if since else None

    raw = await ctx.client.get_paged("le", f"{org_unit_id}/news/")
    items: list[dict[str, Any]] = []

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        posted = parse_d2l(
            m.pick(entry, "StartDate", "DatePosted", "CreatedDate", "PostedDate")
        )
        ends = parse_d2l(m.pick(entry, "EndDate"))

        if ends is not None and ends < now:
            continue  # expired
        if cutoff is not None and posted is not None and posted <= cutoff:
            continue

        body = m.pick(entry, "Body", "Content", default="")
        if isinstance(body, dict):
            body = m.pick(body, "Html", "Text", "Content", default="")
        text, links = to_text_and_links(str(body or ""))
        text, was_truncated = truncate(text, _BODY_CAP)

        items.append(
            {
                "id": m.as_int(m.pick(entry, "Id", "NewsId")),
                "title": m.pick(entry, "Title", "Subject", "Name"),
                "body_text": text,
                "truncated": was_truncated,
                "posted_at": describe(posted, tz),
                "links": links,
            }
        )

    items.sort(key=lambda a: a["posted_at"]["utc"] or "", reverse=True)
    if limit > 0:
        items = items[:limit]

    return {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "announcements": items,
        "count": len(items),
        "since": since,
    }
