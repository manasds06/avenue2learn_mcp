"""Author-role lookup for discussion posts.

Discussion posts on some instances carry no role field at all. Carleton's are
shaped like this:

    {"PostingUserId": 6286, "PostingUserDisplayName": "...", "Message": {...}}

No `AuthorRole`, `Role`, or nested `Author.Role` -- so every post normalizes to
"Unknown", `has_instructor_replies` is always false, and the model cannot tell an
instructor's ruling from a classmate's guess. That distinction is most of why
indexing forums is worth doing at all.

`PostingUserId` does match the classlist's `Identifier`, so the role is
recoverable from the roster.

PRIVACY: the roster is used ONLY to map an id to a role. Names, emails, and
OrgDefinedIds are read and discarded -- never returned, never stored, never
indexed. docs/07 requires that posts carry roles and not names, and this keeps
that true while making the role actually populated.
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.client import models as m
from avenue_mcp.errors import APIError

log = logging.getLogger(__name__)

# Fields the classlist uses for the role, most specific first.
# ClasslistRoleDisplayName is what the route actually returns; the others are
# defensive breadth for instances that differ.
_ROLE_FIELDS = (
    "ClasslistRoleDisplayName",
    "RoleDisplayName",
    "RoleName",
    "Role",
    "RoleAlias",
)


async def role_map(client: Any, org_unit_id: int) -> dict[str, str]:
    """``{user_id: role_name}`` for a course, or ``{}`` if unavailable.

    Returns empty rather than raising: a missing role degrades attribution, but
    it must never take down reading a thread.
    """
    try:
        rows = await client.get_paged("le", f"{org_unit_id}/classlist/")
    except APIError as exc:
        log.debug("classlist unavailable for role lookup on %s: %s", org_unit_id, exc)
        return {}
    except Exception as exc:  # noqa: BLE001 -- never break the caller
        log.debug("role lookup failed on %s: %s", org_unit_id, exc)
        return {}

    out: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        uid = m.pick(row, "Identifier", "UserId", "ProfileIdentifier")
        role = m.pick(row, *_ROLE_FIELDS)
        if uid is not None and role:
            # Deliberately only these two keys. Everything else on the row is
            # personal information we have no use for here.
            out[str(uid)] = str(role)
    return out


def role_of_post(post: dict[str, Any], roles: dict[str, str] | None = None) -> Any:
    """The post's author role, falling back to the roster map by user id."""
    role = m.pick(post, "AuthorRole", "Role", "RoleName", "RoleAlias")
    if role is not None:
        return role

    author = m.pick(post, "Author", default={})
    if isinstance(author, dict):
        nested = m.pick(author, "Role", "RoleName", "RoleAlias")
        if nested is not None:
            return nested

    if roles:
        uid = m.pick(post, "PostingUserId", "UserId", "AuthorId")
        if uid is not None:
            return roles.get(str(uid))
    return None
