"""get_class_list (docs/03-mcp-tools.md).

Written for the restricted case from the start. Full student rosters with
email addresses are personal information under FIPPA, and McMaster is more
likely to lock that down than less — a 403 is the expected and correct
outcome, not a bug to work around.

The route is under `le`, not `lp`. An earlier draft had it wrong, which
matters: the wrong path returns 404, and a 404 read as "blocked" would
permanently degrade this tool on the strength of a typo.
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.errors import AvenueMCPError

log = logging.getLogger(__name__)

# Role names that count as "staff" for the fallback. Brightspace lets
# institutions rename roles, so this is a heuristic over the role string
# rather than an ID lookup.
_STAFF_MARKERS = ("instructor", "teacher", "professor", "ta", "teaching assistant", "faculty")


def _is_staff(role: str) -> bool:
    lowered = (role or "").lower()
    return any(marker in lowered for marker in _STAFF_MARKERS)


def _person(entry: dict[str, Any]) -> dict[str, Any]:
    first = entry.get("FirstName") or ""
    last = entry.get("LastName") or ""
    name = (entry.get("DisplayName") or f"{first} {last}").strip()
    role = entry.get("RoleName") or (entry.get("Role") or {}).get("Name") or ""
    return {
        "name": name,
        "role": role,
        "email": entry.get("Email") or entry.get("EmailAddress") or None,
    }


async def get_class_list(client: D2LClient, *, org_unit_id: int) -> dict[str, Any]:
    """People associated with a course — instructors and TAs where available."""
    people: list[dict[str, Any]] = []
    roster_available = False
    note = ""

    for route in (f"{org_unit_id}/classlist/paged/", f"{org_unit_id}/classlist/"):
        try:
            raw = await client.get_json("le", route)
        except AvenueMCPError as exc:
            log.debug("classlist route %s failed: %s", route, exc)
            note = exc.message
            continue

        entries = raw if isinstance(raw, list) else (raw or {}).get("Items", [])
        people = [_person(e) for e in entries if isinstance(e, dict)]
        roster_available = True
        note = ""
        break

    if not roster_available:
        # Fallback: role-filtered enrollments. Often carries the same
        # restriction, and that's fine — instructor contact info is the part a
        # student actually needs ("who do I email about this?").
        try:
            entries = await client.get_paged(
                "lp", f"enrollments/orgUnits/{org_unit_id}/users/"
            )
            people = [
                _person(e.get("User") or e)
                for e in entries
                if isinstance(e, dict)
            ]
            note = "Full roster unavailable; showing enrollments visible to your account."
        except AvenueMCPError as exc:
            log.debug("role-filtered enrollments failed: %s", exc)
            note = (
                "Neither the class list nor the enrollment list is accessible from a "
                "student account on this Avenue instance. This is expected — student "
                "rosters are restricted personal information."
            )

    instructors = [p for p in people if _is_staff(p["role"])]
    students = [p for p in people if not _is_staff(p["role"])]

    if students and not note:
        # Withholding this is a deliberate choice, not a limitation, so say so
        # rather than letting an empty list read as "no classmates found".
        note = (
            f"{len(students)} student records were returned by Avenue but are not "
            f"included here. A full roster with names and emails is personal "
            f"information under FIPPA, and this tool returns instructor and TA "
            f"contacts by design."
        )

    return {
        "org_unit_id": org_unit_id,
        "instructors": instructors,
        "instructor_count": len(instructors),
        # The count answers "how big is this class" without handing over
        # a list of other people's names and email addresses.
        "student_count": len(students),
        "student_roster_returned": False,
        "note": note or None,
    }
