"""search_course_materials and sync_course_materials.

Search needs NO session -- it reads the local index. This is deliberate: a
student whose cookies expire mid-study still has full semantic search over
everything they have indexed. Do not add require_session() to the search path.
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.context import AppContext
from avenue_mcp.errors import EmbedModelMismatchError, NotFoundError
from avenue_mcp.rag import retrieve
from avenue_mcp.util.dates import now_utc, to_utc_iso

log = logging.getLogger(__name__)


async def search_course_materials(
    ctx: AppContext,
    query: str,
    org_unit_id: int | None = None,
    top_k: int = 8,
) -> dict[str, Any]:
    # No require_session(): this is the offline-capable tool.
    ctx.note_activity()

    indexed = ctx.store.indexed_courses()
    indexed_names = [c["course_name"] or f"Course {c['org_unit_id']}" for c in indexed]

    stored_model = ctx.store.embed_model()
    if stored_model and stored_model != ctx.settings.embed_model:
        raise EmbedModelMismatchError(
            f"Index was built with {stored_model}, configured model is "
            f"{ctx.settings.embed_model}."
        )

    if not indexed:
        return {
            "query": query,
            "results": [],
            "count": 0,
            "indexed_courses": [],
            # The empty-result ambiguity is the point: "the materials don't say"
            # and "you never indexed that course" are completely different
            # answers to give a user.
            "note": (
                "Nothing has been indexed yet, so there is nothing to search. "
                "Run sync_course_materials for a course first."
            ),
        }

    if org_unit_id is not None and not ctx.store.has_course(org_unit_id):
        return {
            "query": query,
            "results": [],
            "count": 0,
            "org_unit_id": org_unit_id,
            "indexed_courses": indexed_names,
            "note": (
                f"Course {org_unit_id} has not been indexed. Indexed courses: "
                f"{', '.join(indexed_names)}. Run sync_course_materials for it first."
            ),
        }

    results = retrieve.search(
        ctx.store,
        ctx.embedder,
        query,
        top_k=top_k,
        org_unit_id=org_unit_id,
        tz_name=ctx.settings.timezone,
    )

    return {
        "query": query,
        "org_unit_id": org_unit_id,
        "results": [
            {"text": r.text, "score": r.score, "citation": r.citation} for r in results
        ],
        "count": len(results),
        # Returned on EVERY call, empty or not.
        "indexed_courses": indexed_names,
        "note": (
            None
            if results
            else "No matching passages found in the indexed material. If you "
            "expected a hit, the file may not be indexed yet -- try "
            "sync_course_materials."
        ),
    }


async def sync_course_materials(
    ctx: AppContext, org_unit_id: int, force: bool = False
) -> dict[str, Any]:
    """The one heavyweight tool. Always user-initiated -- never on a timer,
    never automatically triggered by a search miss."""
    await ctx.require_session()

    course_name = await ctx.course_name(org_unit_id)
    if course_name == f"Course {org_unit_id}":
        # Confirm the course actually exists before a multi-minute operation.
        from avenue_mcp.tools.courses import list_courses

        courses = (await list_courses(ctx, include_inactive=True))["courses"]
        match = next((c for c in courses if c["org_unit_id"] == org_unit_id), None)
        if match is None:
            raise NotFoundError(
                f"No enrolled course with org_unit_id {org_unit_id}. "
                "Use list_courses to see valid IDs."
            )
        course_name = match["name"] or course_name

    ctx.remember_course(org_unit_id, course_name, None)
    report = await ctx.syncer.sync_course(org_unit_id, course_name, force=force)
    report["synced_at"] = to_utc_iso(now_utc())
    report["searchable"] = report["chunks_created"] > 0 or ctx.store.has_course(
        org_unit_id
    )

    parts = [
        f"{report['files_indexed']} file(s) indexed",
        f"{report['files_skipped_unchanged']} unchanged",
    ]
    if ctx.settings.index_discussions:
        parts.append(f"{report['threads_indexed']} thread(s) indexed")
    if report["errors"]:
        parts.append(f"{len(report['errors'])} error(s)")
    report["summary"] = ", ".join(parts)

    return report
