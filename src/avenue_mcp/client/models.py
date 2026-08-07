"""Normalization helpers for Valence responses.

Valence field naming is inconsistent across routes and versions, so every
accessor here tries several spellings rather than assuming one. Phase 0 fixture
capture is what turns these guesses into certainties.
"""

from __future__ import annotations

from typing import Any

# Org unit type names that represent an actual course a student takes.
COURSE_OFFERING_TYPES = {"course offering", "courseoffering", "course"}


def pick(obj: Any, *names: str, default: Any = None) -> Any:
    """First present, non-None value among `names`."""
    if not isinstance(obj, dict):
        return default
    for n in names:
        if n in obj and obj[n] is not None:
            return obj[n]
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v is not None:
            return v
    return default


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- enrollments ----------------------------------------------------------


def enrollment_org_unit(entry: Any) -> dict[str, Any] | None:
    """myenrollments entries wrap the org unit under 'OrgUnit'."""
    if not isinstance(entry, dict):
        return None
    ou = entry.get("OrgUnit")
    return ou if isinstance(ou, dict) else entry


def is_course_offering(org_unit: dict[str, Any]) -> bool:
    """Filter out departments and semester containers.

    Enrollments contain org units that are not courses; without this every
    result is polluted with faculty and term nodes.
    """
    t = org_unit.get("Type")
    if isinstance(t, dict):
        name = str(pick(t, "Name", "Code", default="")).lower()
        if name:
            return any(k in name for k in COURSE_OFFERING_TYPES)
        code = as_int(pick(t, "Id", "TypeId"))
        return code == 3  # 3 is Course Offering in stock D2L
    if isinstance(t, str):
        return any(k in t.lower() for k in COURSE_OFFERING_TYPES)
    return True  # be permissive rather than hide a real course


def course_from_org_unit(org_unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "org_unit_id": as_int(pick(org_unit, "Id", "OrgUnitId", "Identifier")),
        "name": pick(org_unit, "Name", "OrgUnitName"),
        "code": pick(org_unit, "Code", "OrgUnitCode"),
    }


# --- content --------------------------------------------------------------

# Topic types that have a downloadable body. Link and embedded-page topics
# do not, and attempting to download them wastes a request.
_FILE_TOPIC_TYPES = {1}  # 1 = File, 3 = Link in stock D2L


def topic_is_file(topic: dict[str, Any]) -> bool:
    ttype = as_int(pick(topic, "TopicType", "Type"))
    if ttype is not None:
        return ttype in _FILE_TOPIC_TYPES
    url = pick(topic, "Url", "Location", default="") or ""
    if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
        return False
    return bool(url)


def safe_filename(raw: str | None, fallback: str = "file") -> str:
    """Reduce a server-supplied string to a single safe path component.

    SECURITY: the return value is used as a directory entry when caching
    downloads, and both the name and the bytes come from the remote server. An
    unsanitized value like "../../../../.bashrc" escapes the cache directory --
    pathlib does not normalize "..", so the OS resolves it and we write
    attacker-chosen content to an attacker-chosen path.

    Anyone able to create a Content topic in a course you are enrolled in
    controls this string, so it is treated as hostile input.
    """
    if not raw:
        return fallback

    # Take the last component under either separator, then strip any remaining
    # separators, drive letters, and NULs.
    name = str(raw).replace("\\", "/").split("/")[-1]
    name = name.replace("\x00", "").strip().strip(".")
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*')
    name = name.strip()

    if not name or name in (".", ".."):
        return fallback
    return name[:180]  # keep well under common filesystem limits


def guess_filename(topic: dict[str, Any]) -> str | None:
    """A display/cache filename for a topic. Always sanitized -- see above."""
    url = pick(topic, "Url", "Location")
    if isinstance(url, str) and url:
        tail = url.rstrip("/").split("/")[-1].split("?")[0]
        if "." in tail:
            return safe_filename(tail)
    title = pick(topic, "Title", "Name")
    return safe_filename(str(title)) if title else None


def mime_from_name(name: str | None) -> str | None:
    if not name or "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].lower()
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "ppt": "application/vnd.ms-powerpoint",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "txt": "text/plain",
        "md": "text/markdown",
        "html": "text/html",
        "htm": "text/html",
        "csv": "text/csv",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "mp4": "video/mp4",
        "zip": "application/zip",
    }.get(ext)


# --- discussions ----------------------------------------------------------

# Role names that indicate authority. An instructor's reply outranks a
# classmate's guess, and that ranking is the whole point of storing role.
_INSTRUCTOR_HINTS = ("instructor", "teacher", "professor", "lecturer", "faculty")
_TA_HINTS = ("teaching assistant", "ta", "assistant", "marker", "grader")


def normalize_role(raw: Any) -> str:
    """Map a D2L role label onto Instructor / TA / Student / Unknown.

    Only the role is ever stored -- never the author's name. See
    docs/07-risks-and-policy.md.
    """
    if raw is None:
        return "Unknown"
    if isinstance(raw, dict):
        raw = pick(raw, "Name", "Code", "RoleName", default="")
    text = str(raw).strip().lower()
    if not text:
        return "Unknown"
    if any(h in text for h in _INSTRUCTOR_HINTS):
        return "Instructor"
    if text == "ta" or any(h in text for h in _TA_HINTS):
        return "TA"
    if "student" in text or "learner" in text:
        return "Student"
    return "Unknown"
