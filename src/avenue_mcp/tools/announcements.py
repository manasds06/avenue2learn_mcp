"""list_announcements (docs/03-mcp-tools.md).

The simplest end-to-end tool, and the one that proves the pattern:
session -> client -> normalize -> structured JSON.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.util.dates import describe, parse_d2l_date
from avenue_mcp.util.html import clean_body


async def list_announcements(
    client: D2LClient,
    *,
    org_unit_id: int,
    limit: int = 20,
    since: str | None = None,
) -> dict[str, Any]:
    """Recent course announcements, newest first."""
    tz = client.settings.timezone
    base_url = client.settings.base_url

    raw = await client.get_json("le", f"{org_unit_id}/news/")
    entries = raw if isinstance(raw, list) else []

    since_dt: datetime | None = parse_d2l_date(since) if since else None

    items: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        posted = parse_d2l_date(entry.get("StartDate") or entry.get("CreatedDate"))
        if since_dt is not None and posted is not None and posted <= since_dt:
            continue

        # Bodies are HTML. Return clean text plus the links separately —
        # raw markup wastes context, and dropping links loses the Zoom
        # link or the reading the user actually needs.
        body_html = (entry.get("Body") or {}).get("Html") or (entry.get("Body") or {}).get("Text")
        text, links = clean_body(body_html, base_url=base_url)

        items.append(
            {
                "id": entry.get("Id"),
                "title": entry.get("Title") or "",
                "body_text": text,
                "posted_at": describe(posted, tz),
                "links": links,
                "is_pinned": bool(entry.get("IsPinned", False)),
            }
        )

    # Newest first; undated items sort last rather than crashing the compare.
    items.sort(key=lambda i: (i["posted_at"] or {}).get("utc") or "", reverse=True)
    total = len(items)

    return {
        "org_unit_id": org_unit_id,
        "announcements": items[:limit],
        "count": min(total, limit),
        "total_available": total,
    }
