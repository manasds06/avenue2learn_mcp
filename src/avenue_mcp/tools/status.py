"""get_status -- diagnostics.

This exists because without it the model gets an error and guesses. It cannot
distinguish "logged out" from "never synced" from "no such data", so it either
tells the user to re-login unnecessarily or asserts an absence that isn't real.
One cheap tool removes an entire class of confused round-trip.

Works with no session.
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.context import AppContext
from avenue_mcp.util.dates import now_utc, to_utc_iso

log = logging.getLogger(__name__)

OFFLINE_CAPABLE = ["search_course_materials", "get_status", "get_page_image (if cached)"]


async def get_status(ctx: AppContext, check_session: bool = True) -> dict[str, Any]:
    ctx.note_activity()

    session: dict[str, Any] = {
        **ctx.auth.status(),
        "keepalive_enabled": ctx.keepalive.enabled,
        "keepalive_running": ctx.keepalive.running,
        "alive": None,
    }

    if check_session and ctx.auth.session_present:
        try:
            session["alive"] = await ctx.auth.is_alive()
        except Exception as exc:  # noqa: BLE001 -- diagnostics must not raise
            session["alive"] = False
            session["probe_error"] = str(exc)
    elif not ctx.auth.session_present:
        session["alive"] = False

    indexed = ctx.store.indexed_courses()
    stats = ctx.store.stats()
    stored_model = stats.get("embed_model")

    index = {
        "courses_indexed": indexed,
        "course_count": len(indexed),
        "documents": stats["documents"],
        "chunks": stats["chunks"],
        "embed_model": stored_model,
        "configured_embed_model": ctx.settings.embed_model,
        "model_matches_index": (
            stored_model is None or stored_model == ctx.settings.embed_model
        ),
        "path": stats["path"],
    }

    advice: list[str] = []
    if not session["present"]:
        advice.append("Not logged in. Run `avenue-mcp login` to enable live-data tools.")
    elif session["alive"] is False:
        advice.append(
            "Session expired. Run `avenue-mcp login`. Local search still works."
        )
    if not indexed:
        advice.append(
            "No courses indexed. Run sync_course_materials to enable "
            "search_course_materials."
        )
    if not index["model_matches_index"]:
        advice.append(
            "Embedding model does not match the index. Re-sync with force=true, "
            "or restore the previous AVENUE_MCP_EMBED_MODEL."
        )
    if ctx.settings.enable_writes:
        advice.append(
            "WRITE MODE IS ENABLED. submit_assignment can submit real work, and "
            "submissions cannot be undone."
        )

    return {
        "checked_at": to_utc_iso(now_utc()),
        "session": session,
        "index": index,
        "config": {
            "base_url": ctx.settings.base_url,
            "timezone": ctx.settings.timezone,
            "writes_enabled": ctx.settings.enable_writes,
            "index_discussions": ctx.settings.index_discussions,
            "render_dpi": ctx.settings.render_dpi,
            "grade_scale_configured": ctx.settings.grade_scale is not None,
            "state_dir": str(ctx.settings.state_dir),
        },
        "offline_capable": OFFLINE_CAPABLE,
        "advice": advice or ["Everything looks set up."],
    }
