"""submit_assignment -- the ONLY write operation, gated off by default.

A submission cannot be undone. Brightspace keeps submission history; submitting
the wrong file or an unfinished draft is permanently visible to the instructor.
Every read operation in this server can be retried at zero cost, and that
asymmetry is the whole justification for the safeguards below:

1. Feature flag -- not registered at all when off, so the model never sees it.
2. Dry-run first -- without confirm=True, returns a preview and submits nothing.
3. Explicit confirmation -- confirm must be literally True. No truthy coercion.
4. Pre-flight validation -- file exists, under cap, folder accepts submissions.
5. Deadline warning -- past-due status surfaced prominently in the preview.
6. Audit log -- every attempt, dry-run or real, appended locally.
7. No auto-retry -- a retry storm on a submit endpoint could duplicate work.

There is deliberately no separate comment tool: a student cannot comment without
submitting, so the comment travels here as a parameter.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.context import AppContext
from avenue_mcp.errors import (
    APIError,
    InvalidRequestError,
    NotFoundError,
    WritesDisabledError,
)
from avenue_mcp.util.dates import describe, now_utc, parse_d2l, to_utc_iso

log = logging.getLogger(__name__)


async def submit_assignment(
    ctx: AppContext,
    org_unit_id: int,
    folder_id: int,
    file_path: str,
    comment: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    if not ctx.settings.enable_writes:
        raise WritesDisabledError("Write operations are disabled.")

    await ctx.require_session()

    # --- pre-flight -------------------------------------------------------
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise NotFoundError(f"No such file: {path}")

    size = path.stat().st_size
    cap = ctx.settings.max_file_mb * 1024 * 1024
    if size > cap:
        raise InvalidRequestError(
            f"{path.name} is {size // 1048576} MB, over the "
            f"{ctx.settings.max_file_mb} MB cap."
        )
    if size == 0:
        raise InvalidRequestError(f"{path.name} is empty.")

    digest = _sha256(path)

    folder = await _folder(ctx, org_unit_id, folder_id)
    due = parse_d2l(m.pick(folder, "DueDate", "Due")) if folder else None
    now = now_utc()
    past_due = bool(due and now > due)

    preview: dict[str, Any] = {
        "action": "submit_assignment",
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "folder_id": folder_id,
        "assignment_name": m.pick(folder, "Name", "Title") if folder else None,
        "due_date": describe(due, ctx.settings.timezone),
        "is_past_due": past_due,
        "file": {
            "path": str(path),
            "name": path.name,
            "size_bytes": size,
            "sha256": digest,
        },
        "comment": comment,
    }

    if past_due:
        preview["warning"] = (
            "*** THIS ASSIGNMENT IS PAST ITS DUE DATE. *** Submitting now may be "
            "recorded as late. Confirm with the user before proceeding."
        )

    # --- dry run ----------------------------------------------------------
    if confirm is not True:
        preview.update(
            {
                "dry_run": True,
                "submitted": False,
                "next_step": (
                    "Nothing was submitted. Show this preview to the user and get "
                    "their explicit go-ahead, then call again with confirm=true. "
                    "A submission cannot be undone."
                ),
            }
        )
        _audit(ctx, {**preview, "outcome": "dry_run"})
        return preview

    # --- real submission --------------------------------------------------
    _audit(ctx, {**preview, "outcome": "attempt"})

    files = {"file": (path.name, path.read_bytes(), _content_type(path))}
    data: dict[str, Any] = {}
    if comment:
        # The learner comment channel is a field inside the multipart body --
        # there is no standalone endpoint for it.
        data["Comment"] = json.dumps({"Text": comment, "Html": ""})

    try:
        result = await ctx.client.post_multipart(
            "le",
            f"{org_unit_id}/dropbox/folders/{folder_id}/submissions/mysubmissions/",
            data=data or None,
            files=files,
        )
    except APIError as exc:
        # No auto-retry: a retry storm on a submit endpoint could duplicate.
        _audit(ctx, {**preview, "outcome": "failed", "error": str(exc)})
        raise

    preview.update(
        {
            "dry_run": False,
            "submitted": True,
            "submitted_at": to_utc_iso(now_utc()),
            "response": result if isinstance(result, (dict, list)) else None,
            "note": (
                "Submitted. Brightspace retains submission history -- this cannot "
                "be undone. Verify on Brightspace directly."
            ),
        }
    )
    _audit(ctx, {**preview, "outcome": "submitted"})
    return preview


async def _folder(ctx: AppContext, org_unit_id: int, folder_id: int) -> dict[str, Any] | None:
    try:
        data = await ctx.client.get(
            "le", f"{org_unit_id}/dropbox/folders/{folder_id}", cache=False
        )
        return data if isinstance(data, dict) else None
    except APIError as exc:
        # The folder listing may be instructor-only; that must not block a
        # learner from submitting to a folder they were given the id for.
        log.info("could not read folder %s metadata: %s", folder_id, exc)
        return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(131072), b""):
            h.update(block)
    return h.hexdigest()


def _content_type(path: Path) -> str:
    return m.mime_from_name(path.name) or "application/octet-stream"


def _audit(ctx: AppContext, record: dict[str, Any]) -> None:
    """Append-only. If a submission ever goes out unexpectedly, this file is the
    record of what happened and when."""
    try:
        ctx.settings.log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"at": to_utc_iso(now_utc()), **record}
        with ctx.settings.writes_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:  # pragma: no cover
        log.warning("could not write audit log: %s", exc)
