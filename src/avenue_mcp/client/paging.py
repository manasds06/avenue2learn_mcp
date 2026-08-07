"""Bookmark pagination (docs/02-api-surface.md).

Valence collection routes return a `PagingInfo` block with `Bookmark` and
`HasMoreItems`. Pass the bookmark back as a query param until exhausted.

Implemented once, generically. A per-call-site paging loop is how you end up
silently truncating a course list at page one.
"""

from __future__ import annotations

from typing import Any

# Runaway guard. A legitimate response never needs this many pages; a paging
# bug that ignores HasMoreItems would otherwise loop forever.
MAX_PAGES = 50


def extract_items(payload: Any) -> list[Any]:
    """Pull the item list out of a response that may or may not be paged.

    Valence is inconsistent here: some routes return a bare array, others wrap
    items in `Items`/`Objects` alongside `PagingInfo`.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("Items", "Objects", "items", "objects"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def paging_info(payload: Any) -> tuple[str | None, bool]:
    """Return (bookmark, has_more) from a paged response."""
    if not isinstance(payload, dict):
        return None, False

    info = payload.get("PagingInfo") or payload.get("pagingInfo")
    if not isinstance(info, dict):
        return None, False

    bookmark = info.get("Bookmark") or info.get("bookmark")
    has_more = info.get("HasMoreItems", info.get("hasMoreItems", False))
    return (bookmark if isinstance(bookmark, str) else None), bool(has_more)
