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
        # `le`, not `lp`. The wrong component returns 404, which the except
        # arms below would record as "roster unavailable" — degrading this
        # tool permanently on the strength of a typo rather than a permission.
        raw = await ctx.client.get_paged("le", f"{org_unit_id}/classlist/")
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

    # Only reach for the fallback if the classlist itself failed. When it
    # succeeded but simply lists no staff, the enrollments route's 403 would
    # otherwise overwrite the note with "the class list is not available" --
    # about a route that just answered.
    if not instructors and not roster_available:
        found, staff_note = await _staff_via_enrollments(ctx, org_unit_id)
        instructors.extend(found)
        if staff_note and not note:
            note = staff_note

    if not instructors and note is None:
        note = (
            "No instructor information could be retrieved for this course from "
            "the API. Check the course homepage on Avenue directly."
        )

    if instructors and not any(i.get("email") for i in instructors):
        # Measured 2026-08-07: the classlist route populates DisplayName,
        # FirstName, LastName, Username and Identifier, but leaves Email empty
        # for staff. Username is present and McMaster addresses are
        # conventionally <macid>@mcmaster.ca -- but composing one would be a
        # guess presented as a contact detail, so the tool reports the gap.
        contact_note = (
            "Avenue did not provide email addresses for course staff. Names and "
            "roles are accurate; find contact details on the course homepage or "
            "in the outline rather than guessing an address."
        )
        note = f"{note} {contact_note}" if note else contact_note

    if students and not note:
        # Withholding this is a deliberate choice, not a limitation, so say so
        # rather than letting an empty array read as "no classmates found".
        note = (
            f"{len(students)} student records were returned by Avenue but are "
            f"deliberately not included here. A roster with names and emails is "
            f"personal information under FIPPA, and this tool returns course "
            f"staff by design. Ask the user to look them up on Avenue directly "
            f"if they genuinely need a classmate's contact details."
        )

    return {
        "org_unit_id": org_unit_id,
        "course_name": await ctx.course_name(org_unit_id),
        "instructors": instructors,
        # NOT returned in bulk, even when Avenue hands them over.
        #
        # Both implementations of this tool were written expecting a 403 here,
        # per docs/02 and docs/07. The live probe found the route DOES work for
        # students -- 145 classmates with names, emails, usernames, and
        # OrgDefinedIds on a single course. Passing that array back means every
        # call ships a third of a lecture hall's personal information to
        # whatever model provider the MCP client uses.
        #
        # docs/07 is unambiguous that this data is FIPPA-protected and must not
        # be exported or persisted, and says the tool "is designed to return
        # instructor contacts". Honouring that is more important than surfacing
        # a roster nobody asked for. The count answers "how big is this class"
        # without handing over the list.
        "students": [],
        "student_roster_returned": False,
        "student_count": len(students) if roster_available else None,
        "note": note,
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
    # `ClasslistRoleDisplayName` is what the live classlist route actually
    # returns (measured 2026-08-07: "Instructor" x1, "TA 1" x10, "Student"
    # x134). It was absent from this list, so every entry normalized to
    # "Unknown", every person landed in `students`, `instructors` came back
    # empty -- and the empty-instructors branch then fired the enrollments
    # fallback, whose 403 overwrote the note with "the class list is not
    # available" about a route that had just returned 145 rows.
    role_raw = m.pick(
        entry,
        "ClasslistRoleDisplayName",
        "Role",
        "RoleName",
        "RoleDisplayName",
        "RoleAlias",
    )
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
