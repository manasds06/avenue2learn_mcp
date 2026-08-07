"""UTC <-> America/Toronto handling.

Brightspace returns UTC; McMaster deadlines are set in Eastern time and
typically land at 11:59 PM local, which is 03:59 or 04:59 UTC *the next day*
depending on DST. A tool that reports "due March 16" for a March 15 deadline is
worse than one that reports nothing — so every date we surface carries both the
UTC instant and an explicit local rendering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from avenue_mcp.errors import ConfigError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_d2l_date(value: object) -> datetime | None:
    """Parse a Valence date field into an aware UTC datetime.

    Handles the two shapes Brightspace emits: ISO-8601 with a trailing `Z`
    (most routes) and epoch milliseconds (some calendar fields). Returns None
    for null/blank rather than raising — a missing due date is normal data,
    not an error.
    """
    if value is None or value == "":
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        # Epoch milliseconds. Unambiguous in practice: a seconds value this
        # large would be year 33658.
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)

    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc)
            if value.tzinfo
            else value.replace(tzinfo=timezone.utc)
        )

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None

    # A naive timestamp from Brightspace is UTC by convention.
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_utc_param(dt: datetime) -> str:
    """Format for a Valence UTCDateTime query parameter.

    The calendar routes require startDateTime/endDateTime, so this is on the
    critical path for get_upcoming_deadlines rather than a convenience.
    """
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _zone(tz_name: str) -> ZoneInfo:
    """Resolve a timezone, with an actionable message when the tz data is absent.

    Windows ships no system tz database. Without the `tzdata` package,
    ZoneInfo raises a bare ZoneInfoNotFoundError that reads like a bug in this
    code rather than a missing dependency.
    """
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"No timezone data for {tz_name!r}. On Windows this means the `tzdata` "
            f"package is missing — install it with `pip install tzdata`."
        ) from exc


def to_local(dt: datetime | None, tz_name: str = "America/Toronto") -> datetime | None:
    return dt.astimezone(_zone(tz_name)) if dt is not None else None


def iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 UTC with a `Z` suffix, matching Brightspace's own style."""
    return to_utc_param(dt) if dt is not None else None


def render_local(dt: datetime | None, tz_name: str = "America/Toronto") -> str | None:
    """Human-readable local rendering, e.g. `Sun 15 Mar 2026, 11:59 PM EDT`."""
    local = to_local(dt, tz_name)
    if local is None:
        return None
    return local.strftime("%a %d %b %Y, %I:%M %p %Z").replace(" 0", " ")


def describe(dt: datetime | None, tz_name: str = "America/Toronto") -> dict[str, object] | None:
    """The standard date block every tool returns: UTC instant + local rendering.

    Both are present on purpose. The UTC value is what a model should compare
    and sort on; the local string is what a human should be told.
    """
    if dt is None:
        return None
    local = to_local(dt, tz_name)
    assert local is not None
    return {
        "utc": iso_utc(dt),
        "local": render_local(dt, tz_name),
        "local_date": local.date().isoformat(),
    }


def days_until(dt: datetime | None, *, now: datetime | None = None) -> float | None:
    """Fractional days from now until `dt`. Negative when already past."""
    if dt is None:
        return None
    delta: timedelta = dt - (now or utcnow())
    return round(delta.total_seconds() / 86400.0, 2)
