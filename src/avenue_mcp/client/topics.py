"""Filename resolution for content topics.

Some Brightspace instances omit `Url` from the module *structure* listing
(`content/root/`, `content/modules/{id}/structure/`) while returning it from
`content/topics/{id}`. Carleton does this; McMaster does not.

`guess_filename` then falls back to the display title, which carries no
extension -- and everything downstream keys off the extension:

  * rag/sync.py    -> extractor.is_supported()  -> nothing is ever indexed
  * tools/content  -> renderer.is_renderable()  -> is_renderable is always false,
                                                   so the model never tries to
                                                   render a page
  * mime_from_name -> None                      -> extraction loses its fallback

Both failures are silent: they look like a course with no usable files rather
than a shape assumption that did not hold. Shared here so the two call sites
cannot drift apart.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Sequence

from avenue_mcp.client import models as m
from avenue_mcp.errors import APIError

log = logging.getLogger(__name__)


def has_extension(name: str | None) -> bool:
    return bool(name) and bool(Path(name).suffix)


def _is_external(url: object) -> bool:
    """An absolute Url is a link to a publisher or syllabus site, not a file
    stored in Brightspace. Following one downloads somebody else's HTML."""
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


async def resolve_topic_filename(
    client: Any, org_unit_id: int, node: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Return ``(file_name, mime_type)`` for one topic.

    Consults ``content/topics/{id}`` only when the listing's name has no usable
    extension, so an instance that already includes `Url` costs no extra
    request. Falls back to the listing's own name on any failure -- a missing
    extension degrades a feature, while raising here would take down the whole
    content tree.
    """
    name = m.guess_filename(node)
    if has_extension(name):
        return name, m.mime_from_name(name)

    topic_id = m.as_int(m.pick(node, "Id", "TopicId"))
    if topic_id is None:
        return name, m.mime_from_name(name)

    try:
        detail = await client.get("le", f"{org_unit_id}/content/topics/{topic_id}")
    except APIError as exc:
        log.debug("topic %s detail unreadable: %s", topic_id, exc)
        return name, m.mime_from_name(name)

    if not isinstance(detail, dict) or _is_external(m.pick(detail, "Url", "Location")):
        return name, m.mime_from_name(name)

    resolved = m.guess_filename(detail)
    if has_extension(resolved):
        return resolved, m.mime_from_name(resolved)
    return name, m.mime_from_name(name)


async def resolve_many(
    client: Any, org_unit_id: int, nodes: Sequence[dict[str, Any]]
) -> list[tuple[str | None, str | None]]:
    """resolve_topic_filename over several topics, concurrently."""
    return list(
        await asyncio.gather(
            *(resolve_topic_filename(client, org_unit_id, n) for n in nodes)
        )
    )
