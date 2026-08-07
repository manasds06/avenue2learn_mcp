"""Date tests, including the DST boundary cases.

These are not optional. Brightspace returns UTC; McMaster deadlines are Eastern
and land at 11:59 PM local, which is 03:59 or 04:59 UTC *the next day* depending
on DST. Reporting "due March 16" for a March 15 deadline costs real marks.
"""

from __future__ import annotations

from datetime import datetime, timezone

from avenue_mcp.util.dates import (
    days_until,
    describe,
    local_date_str,
    parse_d2l,
    to_utc_iso,
    within_window,
)

TZ = "America/Toronto"


class TestParse:
    def test_z_suffix_with_millis(self):
        dt = parse_d2l("2026-03-15T03:59:00.000Z")
        assert dt == datetime(2026, 3, 15, 3, 59, tzinfo=timezone.utc)

    def test_z_suffix_no_millis(self):
        assert parse_d2l("2026-03-15T03:59:00Z") is not None

    def test_offset(self):
        dt = parse_d2l("2026-03-14T23:59:00-04:00")
        assert dt == datetime(2026, 3, 15, 3, 59, tzinfo=timezone.utc)

    def test_naive_assumed_utc(self):
        dt = parse_d2l("2026-03-15T03:59:00")
        assert dt is not None and dt.tzinfo is timezone.utc

    def test_date_only(self):
        assert parse_d2l("2026-03-15") is not None

    def test_empty_and_none(self):
        assert parse_d2l("") is None
        assert parse_d2l(None) is None
        assert parse_d2l("   ") is None

    def test_garbage(self):
        assert parse_d2l("not a date") is None


class TestDSTBoundary:
    """The bug class this file exists for.

    In 2026, US/Canada DST begins Sunday 8 March and ends Sunday 1 November.
    An 11:59 PM Eastern deadline is 04:59Z next day under EST, 03:59Z under EDT.
    """

    def test_est_deadline_before_transition(self):
        # 5 March 2026, 11:59 PM EST -> 6 March 04:59 UTC
        dt = parse_d2l("2026-03-06T04:59:00.000Z")
        assert local_date_str(dt, TZ) == "2026-03-05"

    def test_edt_deadline_after_transition(self):
        # 15 March 2026, 11:59 PM EDT -> 16 March 03:59 UTC
        dt = parse_d2l("2026-03-16T03:59:00.000Z")
        assert local_date_str(dt, TZ) == "2026-03-15"

    def test_deadline_inside_transition_week(self):
        # 10 March 2026 (transition week), 11:59 PM EDT -> 11 March 03:59 UTC
        dt = parse_d2l("2026-03-11T03:59:00.000Z")
        assert local_date_str(dt, TZ) == "2026-03-10"

    def test_utc_date_differs_from_local_date(self):
        """The whole point: the naive answer is a day late."""
        dt = parse_d2l("2026-03-16T03:59:00.000Z")
        assert to_utc_iso(dt).startswith("2026-03-16")   # UTC says the 16th
        assert local_date_str(dt, TZ) == "2026-03-15"     # local says the 15th

    def test_fall_back_boundary(self):
        # 30 Oct 2026, 11:59 PM EDT -> 31 Oct 03:59 UTC
        assert local_date_str(parse_d2l("2026-10-31T03:59:00Z"), TZ) == "2026-10-30"
        # 5 Nov 2026, 11:59 PM EST -> 6 Nov 04:59 UTC
        assert local_date_str(parse_d2l("2026-11-06T04:59:00Z"), TZ) == "2026-11-05"

    def test_describe_carries_both(self):
        out = describe(parse_d2l("2026-03-16T03:59:00.000Z"), TZ)
        assert out["utc"] == "2026-03-16T03:59:00Z"
        assert out["local_date"] == "2026-03-15"
        assert "EDT" in out["local"]

    def test_describe_labels_est_in_winter(self):
        out = describe(parse_d2l("2026-01-16T04:59:00.000Z"), TZ)
        assert "EST" in out["local"]
        assert out["local_date"] == "2026-01-15"


class TestWindows:
    def test_days_until_future(self):
        ref = datetime(2026, 3, 1, tzinfo=timezone.utc)
        dt = datetime(2026, 3, 15, tzinfo=timezone.utc)
        assert days_until(dt, ref) == 14.0

    def test_days_until_past_is_negative(self):
        ref = datetime(2026, 3, 20, tzinfo=timezone.utc)
        dt = datetime(2026, 3, 15, tzinfo=timezone.utc)
        assert days_until(dt, ref) == -5.0

    def test_within_window_includes_future(self):
        ref = datetime(2026, 3, 1, tzinfo=timezone.utc)
        assert within_window(datetime(2026, 3, 10, tzinfo=timezone.utc), 14, ref)

    def test_within_window_excludes_past(self):
        ref = datetime(2026, 3, 10, tzinfo=timezone.utc)
        assert not within_window(datetime(2026, 3, 1, tzinfo=timezone.utc), 14, ref)

    def test_within_window_excludes_beyond(self):
        ref = datetime(2026, 3, 1, tzinfo=timezone.utc)
        assert not within_window(datetime(2026, 4, 1, tzinfo=timezone.utc), 14, ref)

    def test_none_is_never_in_window(self):
        assert not within_window(None, 14)
        assert days_until(None) is None
