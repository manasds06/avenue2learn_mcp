"""Wording for a denied route, calibrated to what we actually know.

A 403 means the same thing mechanically everywhere, but what we can honestly
*say* about it differs. On an instance we have probed, a denial on a known-denied
route is expected and permanent. On an instance nobody has probed, the same 403
might be a permanent restriction or might be specific to one course -- and
asserting the former would be borrowing another school's measurements.

This only changes prose. It must never gate a request: every tool discovers the
truth by attempting the route and catching PermissionDeniedError, and skipping a
call because a profile says "denied" would hide a route that actually works.
"""

from __future__ import annotations

from avenue_mcp.config import Settings


def describe_denial(settings: Settings, key: str) -> str:
    """One sentence on how much weight to put on this denial."""
    prof = settings.institution_profile
    status = prof.capability(key)
    if status == "denied":
        doc = f" (see {prof.probe_doc})" if prof.probe_doc else ""
        return (
            f"This route is denied to student accounts on {prof.lms_name} and "
            f"that is expected, not an error{doc}."
        )
    if status == "permitted":
        return (
            f"This route normally works on {prof.lms_name}, so this denial is "
            f"more likely specific to this course than a general restriction."
        )
    return (
        f"API permissions on this {prof.lms_name} instance have not been "
        f"systematically verified, so this may be a permanent restriction or "
        f"specific to this course. Report it as observed here, and do not "
        f"generalize it to other courses."
    )


def capability_status(settings: Settings, key: str) -> str:
    """Machine-readable counterpart to describe_denial."""
    return settings.institution_profile.capability(key)
