"""Date handling.

Brightspace returns UTC. McMaster deadlines are set in Eastern and typically
land at 11:59 PM local, which is 03:59 or 04:59 UTC *the next day* depending
on DST. Reporting "due March 16" for a March 15 deadline costs real marks, so
every user-facing timestamp carries an explicit local rendering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from avenue_mcp.errors import ConfigError


def parse_d2l(value: str | None) -> datetime | None:
    """Parse a D2L timestamp into an aware UTC datetime.

    D2L emits ISO-8601 with a trailing Z and often milliseconds:
    "2026-03-15T03:59:00.000Z". Also tolerates offsets and naive strings
    (assumed UTC).
    """
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zone(tz_name: str) -> ZoneInfo:
    """Resolve a timezone, with an actionable message when tz data is absent.

    Windows ships no system tz database, so a bare ZoneInfo("America/Toronto")
    raises ZoneInfoNotFoundError there unless the `tzdata` package is present.
    Every deadline goes through this function, so the failure would look like
    a bug in the deadline code rather than a missing dependency.
    """
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"No timezone data for {tz_name!r}. On Windows this means the "
            f"`tzdata` package is missing — install it with `pip install tzdata`."
        ) from exc


def to_local(dt: datetime | None, tz_name: str) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(_zone(tz_name))


def local_str(dt: datetime | None, tz_name: str) -> str | None:
    """Human-readable local rendering, e.g. 'Sat 15 Mar 2026, 11:59 PM EDT'."""
    loc = to_local(dt, tz_name)
    if loc is None:
        return None
    return loc.strftime("%a %d %b %Y, %I:%M %p %Z").replace(" 0", " ")


def local_date_str(dt: datetime | None, tz_name: str) -> str | None:
    """Local calendar date only -- the field most likely to be off by one."""
    loc = to_local(dt, tz_name)
    if loc is None:
        return None
    return loc.strftime("%Y-%m-%d")


def describe(dt: datetime | None, tz_name: str) -> dict[str, str | None]:
    """The standard shape for any user-facing timestamp.

    Always emits UTC *and* local, so a model cannot accidentally report a UTC
    date as if it were local.
    """
    return {
        "utc": to_utc_iso(dt),
        "local": local_str(dt, tz_name),
        "local_date": local_date_str(dt, tz_name),
    }


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def days_until(dt: datetime | None, ref: datetime | None = None) -> float | None:
    if dt is None:
        return None
    ref = ref or now_utc()
    return round((dt - ref).total_seconds() / 86400.0, 2)


def within_window(dt: datetime | None, days: int, ref: datetime | None = None) -> bool:
    """True if dt falls in [now, now + days]. Past dates are excluded."""
    if dt is None:
        return False
    ref = ref or now_utc()
    return ref <= dt <= ref + timedelta(days=days)
