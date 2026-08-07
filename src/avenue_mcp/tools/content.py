"""Content tools: browse the tree, read a file, render a page."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.client.topics import resolve_topic_filename
from avenue_mcp.context import AppContext
from avenue_mcp.errors import ExtractionError, RenderError
from avenue_mcp.rag import extract as extractor
from avenue_mcp.rag import render as renderer
from avenue_mcp.util.dates import describe, parse_d2l

log = logging.getLogger(__name__)

MAX_TREE_DEPTH = 10


async def get_course_content(
    ctx: AppContext, org_unit_id: int, max_depth: int = MAX_TREE_DEPTH
) -> dict[str, Any]:
    """Structure only. Reading a file's text is read_content_file; searching
    across files by meaning is search_course_materials."""
    await ctx.require_session()
    tz = ctx.settings.timezone

    root = await ctx.client.get("le", f"{org_unit_id}/content/root/")
    seen_modules: set[int] = set()
    counter = {"topics": 0}
    unreadable: list[dict[str, Any]] = []

    async def walk(nodes: Any, depth: int) -> list[dict[str, Any]]:
        if depth > max_depth or not isinstance(nodes, list):
            return []
        modules: list[dict[str, Any]] = []
        topics: list[dict[str, Any]] = []

        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = str(m.pick(node, "Title", "Name", default="") or "")
            node_type = str(m.pick(node, "Type", default="")).lower()
            is_module = (
                node_type == "module"
                or "Structure" in node
                or "Modules" in node
                or m.as_int(m.pick(node, "ModuleId")) is not None
            )

            if is_module:
                mod_id = m.as_int(m.pick(node, "Id", "ModuleId"))
                if mod_id is None or mod_id in seen_modules:
                    continue
                seen_modules.add(mod_id)
                structure = m.pick(node, "Structure", "Modules", "Topics")
                if not isinstance(structure, list):
                    try:
                        structure = await ctx.client.get(
                            "le", f"{org_unit_id}/content/modules/{mod_id}/structure/"
                        )
                    except Exception as exc:  # noqa: BLE001 -- keep walking
                        # Recorded, not silently swallowed. Dropping a whole
                        # subtree and still reporting a confident topic_count
                        # made the tool claim a course has 12 files when it has
                        # 40, with nothing to indicate the tree was partial.
                        log.info("module %s unreadable: %s", mod_id, exc)
                        unreadable.append(
                            {"module_id": mod_id, "title": title, "reason": str(exc)[:160]}
                        )
                        structure = []
                children = await walk(structure, depth + 1)
                nested_topics = [c for c in children if c.get("_kind") == "topic"]
                nested_modules = [c for c in children if c.get("_kind") == "module"]
                modules.append(
                    {
                        "_kind": "module",
                        "id": mod_id,
                        "title": title,
                        "topics": [_clean(t) for t in nested_topics],
                        "modules": [_clean(x) for x in nested_modules],
                    }
                )
                continue

            topic_id = m.as_int(m.pick(node, "Id", "TopicId"))
            if topic_id is None:
                continue
            counter["topics"] += 1
            # Resolved against the topic detail record when the listing omits
            # Url, otherwise every file reports mime_type null and
            # is_renderable false. See client/topics.py.
            file_name, mime_type = await resolve_topic_filename(
                ctx.client, org_unit_id, node
            )
            downloadable = m.topic_is_file(node)
            topics.append(
                {
                    "_kind": "topic",
                    "id": topic_id,
                    "title": title or file_name,
                    "type": _topic_type_name(node),
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "last_modified": describe(
                        parse_d2l(m.pick(node, "LastModifiedDate", "LastModified")), tz
                    ),
                    # False for link and embedded-page topics, so the model does
                    # not attempt read_content_file on something with no body.
                    "is_downloadable": downloadable,
                    "is_renderable": bool(file_name and renderer.is_renderable(file_name)),
                }
            )

        return modules + topics

    tree = await walk(root if isinstance(root, list) else [root], 0)

    return {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "modules": [_clean(x) for x in tree if x.get("_kind") == "module"],
        "topics": [_clean(x) for x in tree if x.get("_kind") == "topic"],
        "topic_count": counter["topics"],
        "complete": not unreadable,
        "unreadable_modules": unreadable,
        "note": (
            f"{len(unreadable)} module(s) could not be read, so this tree is "
            "INCOMPLETE and topic_count undercounts the course."
            if unreadable
            else None
        ),
    }


def _clean(node: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in node.items() if k != "_kind"}


def _topic_type_name(node: dict[str, Any]) -> str:
    t = m.as_int(m.pick(node, "TopicType", "Type"))
    return {1: "File", 3: "Link"}.get(t or 0, "Topic")


async def read_content_file(
    ctx: AppContext,
    org_unit_id: int,
    topic_id: int,
    max_chars: int = 12_000,
    start_page: int = 1,
) -> dict[str, Any]:
    """Extract text from one file, paginated by page/slide.

    The default cap is 12k characters (~3k tokens), not 50k: a 60-page deck
    fully extracted is a large fraction of a context window from one call, and
    the model usually needs one section rather than the whole thing.
    """
    await ctx.require_session()

    meta: dict[str, Any] = {}
    try:
        meta = await ctx.client.get("le", f"{org_unit_id}/content/topics/{topic_id}") or {}
    except Exception as exc:  # noqa: BLE001 -- metadata is a nice-to-have
        log.debug("topic metadata unavailable for %s: %s", topic_id, exc)

    title = m.pick(meta, "Title", "Name")
    file_name = m.guess_filename(meta) if meta else None
    dest = ctx.settings.cache_dir / str(org_unit_id) / str(topic_id) / (
        file_name or f"topic-{topic_id}"
    )

    info = await ctx.client.download(
        "le", f"{org_unit_id}/content/topics/{topic_id}/file", dest
    )
    path = Path(info["path"])
    actual_name = info.get("filename") or file_name or path.name

    if not extractor.is_supported(path) and not extractor.is_supported(actual_name):
        raise ExtractionError(
            f"{actual_name} is not a text-extractable format. Supported: "
            "PDF, DOCX, PPTX, HTML, TXT, MD, CSV."
        )

    result = extractor.extract(path, info.get("mime_type"))

    if result.quality != "ok":
        return {
            "topic_id": topic_id,
            "title": title,
            "file_name": actual_name,
            "mime_type": info.get("mime_type"),
            "page_count": result.page_count,
            "text": "",
            "truncated": False,
            "extraction_quality": result.quality,
            # Reported, not silently returned as empty -- a model that thinks a
            # document is blank will confidently say the outline doesn't
            # mention late penalties.
            "note": result.note,
        }

    segments = result.segments
    if start_page > 1:
        segments = [s for s in segments if (s.page or 0) >= start_page] or segments[
            min(start_page - 1, len(segments) - 1):
        ]

    parts: list[str] = []
    used = 0
    next_start: int | None = None
    first_pos = segments[0].position if segments else None
    last_pos = first_pos

    for seg in segments:
        block = f"[{seg.position}]\n{seg.text}"
        if used + len(block) > max_chars and parts:
            next_start = seg.page or None
            break
        parts.append(block)
        used += len(block) + 2
        last_pos = seg.position

    text = "\n\n".join(parts)
    truncated = next_start is not None or (
        len(segments) > len(parts) and len(parts) > 0
    )

    return {
        "topic_id": topic_id,
        "title": title,
        "file_name": actual_name,
        "mime_type": info.get("mime_type"),
        "page_count": result.page_count,
        "pages_returned": (
            f"{first_pos}–{last_pos}" if first_pos != last_pos else first_pos
        ),
        "text": text,
        "truncated": truncated,
        "next_start_page": next_start,
        "extraction_quality": result.quality,
        "hint": (
            "Output was truncated. Call again with start_page=%s, or use "
            "search_course_materials to jump straight to the relevant passage."
            % next_start
            if truncated and next_start
            else None
        ),
    }


async def get_page_image(
    ctx: AppContext,
    org_unit_id: int,
    topic_id: int,
    page: int,
    dpi: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Render one page/slide as PNG. Returns (png_bytes, metadata).

    Text extraction gets the words and loses the figure, so this is the tool for
    diagrams, graphs, and layout-dependent tables.
    """
    dpi = dpi or ctx.settings.render_dpi
    ctx.note_activity()

    # Prefer a cached copy: this can work with no session at all.
    cached = _find_cached(ctx, org_unit_id, topic_id)
    if cached is None:
        await ctx.require_session()
        meta = {}
        try:
            meta = await ctx.client.get(
                "le", f"{org_unit_id}/content/topics/{topic_id}"
            ) or {}
        except Exception:  # noqa: BLE001
            pass
        file_name = m.guess_filename(meta) if meta else None
        dest = ctx.settings.cache_dir / str(org_unit_id) / str(topic_id) / (
            file_name or f"topic-{topic_id}"
        )
        info = await ctx.client.download(
            "le", f"{org_unit_id}/content/topics/{topic_id}/file", dest
        )
        cached = Path(info["path"])

    if not renderer.is_renderable(cached):
        raise RenderError(
            f"{cached.name} cannot be rendered as an image. Only PDF and PPTX "
            "are supported -- use read_content_file for its text instead."
        )

    png, meta = renderer.render_page(
        cached, page, dpi=dpi, cache_dir=ctx.settings.render_dir
    )
    meta.update(
        {
            "org_unit_id": org_unit_id,
            "topic_id": topic_id,
            "course_name": await _safe_course_name(ctx, org_unit_id),
        }
    )
    return png, meta


def _find_cached(ctx: AppContext, org_unit_id: int, topic_id: int) -> Path | None:
    folder = ctx.settings.cache_dir / str(org_unit_id) / str(topic_id)
    if not folder.is_dir():
        return None
    for candidate in sorted(folder.iterdir()):
        if candidate.is_file() and renderer.is_renderable(candidate):
            return candidate
    return None


async def _safe_course_name(ctx: AppContext, org_unit_id: int) -> str:
    stored = ctx.store.course_name(org_unit_id)
    if stored:
        return stored
    try:
        return await ctx.course_name(org_unit_id)
    except Exception:  # noqa: BLE001
        return f"Course {org_unit_id}"


