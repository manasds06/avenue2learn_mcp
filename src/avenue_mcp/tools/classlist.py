"""get_class_list -- instructor contacts, and honesty about the roster.

Written for the RESTRICTED case on purpose. Full student rosters with email
addresses are personal information under FIPPA, and McMaster is more likely to
lock this down than less, so a 403 is the expected and correct outcome.

If Phase 0 finds rosters available, this widens. Writing it optimistically and
narrowing later produces a tool that lies to the model until someone fixes it.
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.context import AppContext
from avenue_mcp.errors import APIError, PermissionDeniedError

log = logging.getLogger(__name__)

_STAFF_ROLES = ("Instructor", "TA")


async def get_class_list(ctx: AppContext, org_unit_id: int) -> dict[str, Any]:
    await ctx.require_session()

    instructors: list[dict[str, Any]] = []
    students: list[dict[str, Any]] = []
    roster_available = False
    note: str | None = None

    try:
        raw = await ctx.client.get_paged("lp", f"{org_unit_id}/classlist/")
        roster_available = True
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            person = _person(entry)
            if person["role"] in _STAFF_ROLES:
                instructors.append(person)
            else:
                students.append(person)
    except PermissionDeniedError as exc:
        log.info("classlist denied for %s (expected): %s", org_unit_id, exc)
        note = (
            "The full class roster is not accessible from a student account on "
            "this instance -- this is expected, not an error. Showing course "
            "staff only."
        )
    except APIError as exc:
        note = f"Could not read the class list: {exc}"

    if not instructors:
        found, staff_note = await _staff_via_enrollments(ctx, org_unit_id)
        instructors.extend(found)
        if staff_note and not note:
            note = staff_note

    if not instructors and note is None:
        note = (
            "No instructor information could be retrieved for this course from "
            "the API. Check the course homepage on Avenue directly."
        )

    return {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "instructors": instructors,
        "students": students if roster_available else [],
        "student_roster_available": roster_available and bool(students),
        "student_count": len(students) if roster_available else None,
        "note": note,
        "privacy_reminder": (
            "Any names or emails returned here are other people's personal "
            "information. Do not export, persist, or redistribute them."
        )
        if students
        else None,
    }


async def _staff_via_enrollments(
    ctx: AppContext, org_unit_id: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Fallback: role-filtered enrollments. May carry the same restriction."""
    try:
        raw = await ctx.client.get_paged(
            "lp", f"enrollments/orgUnits/{org_unit_id}/users/"
        )
    except PermissionDeniedError:
        return [], (
            "Neither the class list nor the enrollment list is available to "
            "student accounts on this instance."
        )
    except APIError as exc:
        return [], f"Could not read course enrollments: {exc}"

    staff: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        person = _person(entry)
        if person["role"] in _STAFF_ROLES:
            staff.append(person)
    return staff, None


def _person(entry: dict[str, Any]) -> dict[str, Any]:
    user = entry
    nested = m.pick(entry, "User")
    if isinstance(nested, dict):
        user = nested

    first = m.pick(user, "FirstName", "GivenName", default="") or ""
    last = m.pick(user, "LastName", "Surname", default="") or ""
    display = m.pick(user, "DisplayName", "FullName") or f"{first} {last}".strip()

    # Name-bearing keys FIRST. D2L classlist entries carry RoleId as an integer,
    # and normalize_role(103) matches no hint -> "Unknown" for everyone, which
    # put the instructors in the students array and left `instructors` empty.
    role_raw = m.pick(entry, "Role", "RoleName", "RoleDisplayName", "RoleAlias")
    if isinstance(role_raw, dict):
        role_raw = m.pick(role_raw, "Name", "Code")
    role = m.normalize_role(role_raw)
    if role == "Unknown":
        role = m.normalize_role(m.pick(entry, "RoleId"))

    return {
        "name": display or None,
        "role": role,
        "email": m.pick(user, "EmailAddress", "Email", "ExternalEmail"),
        "identifier": m.pick(user, "Identifier", "UserId", "Id"),
    }
