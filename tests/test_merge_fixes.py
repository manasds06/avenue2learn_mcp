"""Regression tests for defects found while merging two parallel implementations.

Each is a bug that existed on `main` and was fixed by taking the correction from
the `manas` branch. They live together so it is obvious what must never regress.

See MERGE-NOTES.md for how each was found.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from avenue_mcp.context import AppContext


def _json(payload) -> httpx.Response:
    return httpx.Response(
        200, content=json.dumps(payload), headers={"content-type": "application/json"}
    )


def _ctx_with(handler, tmp_path, monkeypatch) -> AppContext:
    """An AppContext whose transport is a caller-supplied handler."""
    monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "state"))
    from avenue_mcp.config import get_settings

    get_settings.cache_clear()

    ctx = AppContext()
    ctx.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.settings.session_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "d2lSessionVal",
                        "value": "x",
                        "domain": "avenue.cllmcmaster.ca",
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    ctx.auth._client = httpx.AsyncClient(
        base_url=ctx.settings.base_url, transport=httpx.MockTransport(handler)
    )

    async def _alive() -> bool:
        return True

    monkeypatch.setattr(ctx.auth, "is_alive", _alive)
    return ctx


class TestCalendarRequiresDateRange:
    """`startDateTime`/`endDateTime` are REQUIRED on calendar/events/myEvents/.

    Valence returns 400 without them. The route is both the primary deadline
    source and the fallback when the dropbox listing is instructor-only, so a
    bare call made "what's due this week" fail in every course — and the 400
    read as "no deadlines" rather than "malformed request".
    """

    @pytest.mark.asyncio
    async def test_window_params_are_sent(self, tmp_path, monkeypatch):
        seen: dict[str, list[str]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p == "/d2l/api/versions/":
                return _json(
                    [
                        {"ProductCode": "lp", "LatestVersion": "1.62"},
                        {"ProductCode": "le", "LatestVersion": "1.96"},
                    ]
                )
            if "calendar/events/myEvents" in p:
                seen.update(parse_qs(urlparse(str(request.url)).query))
                # Mirror the real API: reject a call with no window.
                if "startDateTime" not in seen or "endDateTime" not in seen:
                    return httpx.Response(
                        400,
                        content='{"Errors":[{"Message":"startDateTime is required"}]}',
                        headers={"content-type": "application/json"},
                    )
                return _json([])
            return _json([])

        ctx = _ctx_with(handler, tmp_path, monkeypatch)
        try:
            from avenue_mcp.tools.assignments import _calendar_events

            await _calendar_events(ctx, 111)
        finally:
            await ctx.aclose()

        assert "startDateTime" in seen, "calendar called without the required window"
        assert "endDateTime" in seen

        # Milliseconds are REQUIRED, not cosmetic: the live host rejects
        # `...T00:00:00Z` with the same 400 it gives for omitting the param.
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        for key in ("startDateTime", "endDateTime"):
            assert seen[key][0].endswith(".000Z"), (
                f"{key}={seen[key][0]!r} lacks milliseconds; Valence returns 400"
            )

        start = datetime.strptime(seen["startDateTime"][0], fmt)
        end = datetime.strptime(seen["endDateTime"][0], fmt)
        assert start < end

        # The window must actually span now, or it returns nothing that matters.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert start <= now <= end


class TestClasslistUsesLearningEnvironment:
    """classlist is under `le`, not `lp`.

    The wrong component 404s. get_class_list's except arms would record that as
    "roster unavailable" — degrading the tool permanently on a typo rather than
    on a real permission boundary.
    """

    @pytest.mark.asyncio
    async def test_classlist_requested_under_le(self, tmp_path, monkeypatch):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            paths.append(p)
            if p == "/d2l/api/versions/":
                return _json(
                    [
                        {"ProductCode": "lp", "LatestVersion": "1.62"},
                        {"ProductCode": "le", "LatestVersion": "1.96"},
                    ]
                )
            if "classlist" in p:
                return _json(
                    [
                        {
                            "DisplayName": "Dr. Smith",
                            "RoleName": "Instructor",
                            "EmailAddress": "s@mcmaster.ca",
                        }
                    ]
                )
            return _json([])

        ctx = _ctx_with(handler, tmp_path, monkeypatch)
        try:
            from avenue_mcp.tools.classlist import get_class_list

            await get_class_list(ctx, 111)
        finally:
            await ctx.aclose()

        classlist_calls = [p for p in paths if "classlist" in p]
        assert classlist_calls, "classlist was never requested"
        for path in classlist_calls:
            assert "/d2l/api/le/" in path, f"classlist requested under the wrong component: {path}"
            assert "/d2l/api/lp/" not in path


class TestTimezoneDataIsAvailable:
    """Windows ships no system tz database.

    Every deadline goes through ZoneInfo("America/Toronto"). Without the
    `tzdata` package that raises ZoneInfoNotFoundError on Windows, which reads
    as a bug in the deadline code rather than a missing dependency.
    """

    def test_eastern_resolves(self):
        from avenue_mcp.util.dates import to_local

        dt = datetime(2026, 3, 16, 3, 59, tzinfo=timezone.utc)
        local = to_local(dt, "America/Toronto")
        assert local is not None
        # The DST trap: 03:59Z on Mar 16 is 11:59 PM Eastern on Mar 15.
        assert local.strftime("%Y-%m-%d") == "2026-03-15"

    def test_missing_tzdata_gives_an_actionable_error(self):
        from avenue_mcp.errors import ConfigError
        from avenue_mcp.util.dates import to_local

        with pytest.raises(ConfigError) as excinfo:
            to_local(datetime.now(timezone.utc), "Not/AZone")
        assert "tzdata" in str(excinfo.value)

    def test_tzdata_is_declared_for_windows(self):
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        assert "tzdata" in text, "tzdata must be declared or Windows installs are broken"


class TestSessionFilePermissions:
    """chmod(0o600) is a silent no-op on Windows.

    session.json is equivalent to a logged-in Avenue session, so the POSIX-only
    path left the project's most sensitive artifact inheriting whatever the
    parent directory granted while the code reported it protected.
    """

    def test_restrict_is_inert_off_windows_and_never_raises(self, tmp_path):
        from avenue_mcp.auth.filelock import restrict_to_owner

        target = tmp_path / "session.json"
        target.write_text("{}", encoding="utf-8")
        # Returns a bool either way; must not raise on any platform.
        assert isinstance(restrict_to_owner(target), bool)

    def test_missing_file_is_handled(self, tmp_path):
        from avenue_mcp.auth.filelock import describe_permissions, restrict_to_owner

        missing = tmp_path / "nope.json"
        assert restrict_to_owner(missing) is False
        assert "does not exist" in describe_permissions(missing)

    @pytest.mark.skipif(sys.platform != "win32", reason="ACL behaviour is Windows-only")
    def test_windows_acl_drops_inherited_entries(self, tmp_path):
        from avenue_mcp.auth.filelock import describe_permissions, restrict_to_owner

        target = tmp_path / "session.json"
        target.write_text("{}", encoding="utf-8")

        before = describe_permissions(target)
        assert "SYSTEM" in before or "Administrators" in before, (
            "expected inherited ACEs before restriction; test premise is wrong"
        )

        assert restrict_to_owner(target) is True
        after = describe_permissions(target)
        assert "SYSTEM" not in after
        assert "Administrators" not in after
        # And it must still be readable by us.
        assert target.read_text(encoding="utf-8") == "{}"


class TestSubmissionStatusIsNotAsserted:
    """`mysubmissions` is 403 on avenue.cllmcmaster.ca despite being documented
    as a Learner route (docs/08, 2026-08-07).

    The tool defaulted every record to "not_submitted" and only upgraded it on a
    successful read, so a blocked route silently became "you have not submitted
    this" for every assignment in the course -- a confidently wrong answer about
    a deadline.
    """

    @pytest.mark.asyncio
    async def test_blocked_route_yields_unknown_not_not_submitted(
        self, tmp_path, monkeypatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p == "/d2l/api/versions/":
                return _json(
                    [
                        {"ProductCode": "lp", "LatestVersion": "1.62"},
                        {"ProductCode": "le", "LatestVersion": "1.96"},
                    ]
                )
            if "mysubmissions" in p:
                # What the live host actually does.
                return httpx.Response(
                    403,
                    content='{"Errors":[{"Message":"Not authorized"}]}',
                    headers={"content-type": "application/json"},
                )
            if p.endswith("/dropbox/folders/"):
                return _json(
                    [{"Id": 7, "Name": "Assignment 3", "DueDate": "2099-01-01T04:59:00.000Z"}]
                )
            return _json([])

        ctx = _ctx_with(handler, tmp_path, monkeypatch)
        try:
            from avenue_mcp.tools.assignments import list_assignments

            out = await list_assignments(ctx, 111)
        finally:
            await ctx.aclose()

        assert out["assignments"], "the folder listing itself must still work"
        for a in out["assignments"]:
            assert a["submission_status"] == "unknown", (
                "a blocked submissions route must not be reported as 'not_submitted'"
            )

        assert out["submission_status_available"] is False
        assert "unknown" in (out.get("note") or "").lower()

    @pytest.mark.asyncio
    async def test_readable_route_still_reports_submitted(self, tmp_path, monkeypatch):
        """The fix must not blind the tool where the route DOES work."""

        def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p == "/d2l/api/versions/":
                return _json(
                    [
                        {"ProductCode": "lp", "LatestVersion": "1.62"},
                        {"ProductCode": "le", "LatestVersion": "1.96"},
                    ]
                )
            if "mysubmissions" in p:
                return _json(
                    [
                        {
                            "Submissions": [
                                {
                                    "SubmissionDate": "2026-01-02T10:00:00.000Z",
                                    "Files": [{"FileName": "a3.pdf"}],
                                }
                            ]
                        }
                    ]
                )
            if p.endswith("/dropbox/folders/"):
                return _json(
                    [{"Id": 7, "Name": "Assignment 3", "DueDate": "2099-01-01T04:59:00.000Z"}]
                )
            return _json([])

        ctx = _ctx_with(handler, tmp_path, monkeypatch)
        try:
            from avenue_mcp.tools.assignments import list_assignments

            out = await list_assignments(ctx, 111)
        finally:
            await ctx.aclose()

        assert out["submission_status_available"] is True
        assert out["assignments"][0]["submission_status"] == "submitted"


class TestQuizAttemptStatusIsNotAsserted:
    """`quizzes/{id}/attempts/` is 403 on this instance (docs/08).

    `_attempts` returns None on denial, but the status expression was
    `"attempted" if (attempts_used or 0) > 0 else "not_attempted"` -- so None
    collapsed to 0 and every quiz was reported as NOT ATTEMPTED to a student
    who may well have taken it. Found while porting to TypeScript; identical in
    shape to the list_assignments "not_submitted" bug.
    """

    @pytest.mark.asyncio
    async def test_blocked_attempts_route_yields_unknown(self, tmp_path, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p == "/d2l/api/versions/":
                return _json(
                    [
                        {"ProductCode": "lp", "LatestVersion": "1.62"},
                        {"ProductCode": "le", "LatestVersion": "1.96"},
                    ]
                )
            if "/attempts/" in p:
                return httpx.Response(
                    403,
                    content='{"Errors":[{"Message":"Not authorized"}]}',
                    headers={"content-type": "application/json"},
                )
            if p.endswith("/quizzes/"):
                return _json(
                    [{"QuizId": 5, "Name": "Lab Safety Quiz",
                      "DueDate": "2099-01-01T04:59:00.000Z"}]
                )
            return _json([])

        ctx = _ctx_with(handler, tmp_path, monkeypatch)
        try:
            from avenue_mcp.tools.quizzes import list_quizzes

            out = await list_quizzes(ctx, 111)
        finally:
            await ctx.aclose()

        assert out["quizzes"], "the quiz listing itself must still work"
        for q in out["quizzes"]:
            assert q["status"] == "unknown", (
                "a blocked attempts route must not be reported as 'not_attempted'"
            )
        assert out["attempt_status_available"] is False
        assert "unknown" in out["note"].lower()

    @pytest.mark.asyncio
    async def test_readable_attempts_route_still_reports_attempted(
        self, tmp_path, monkeypatch
    ):
        """The fix must not blind the tool where the route DOES work."""

        def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p == "/d2l/api/versions/":
                return _json(
                    [
                        {"ProductCode": "lp", "LatestVersion": "1.62"},
                        {"ProductCode": "le", "LatestVersion": "1.96"},
                    ]
                )
            if "/attempts/" in p:
                return _json([{"Score": 9.0}])
            if p.endswith("/quizzes/"):
                return _json(
                    [{"QuizId": 5, "Name": "Lab Safety Quiz",
                      "DueDate": "2099-01-01T04:59:00.000Z"}]
                )
            return _json([])

        ctx = _ctx_with(handler, tmp_path, monkeypatch)
        try:
            from avenue_mcp.tools.quizzes import list_quizzes

            out = await list_quizzes(ctx, 111)
        finally:
            await ctx.aclose()

        assert out["attempt_status_available"] is True
        assert out["quizzes"][0]["status"] == "attempted"
        assert out["quizzes"][0]["attempts_used"] == 1


class TestDeadlineViewSurvivesBlockedSubmissions:
    """The `_Unavailable` sentinel is a truthy object.

    In _enrich_with_assignments the status was `"submitted" if sub else
    "not_submitted"`, so once mysubmissions started returning the sentinel
    every item became "submitted" -- and because include_submitted defaults to
    False, they were then filtered out. A blocked route would have silently
    emptied "what's due this week", which is the product's main question.
    """

    @staticmethod
    def _handler(deny_submissions: bool):
        from datetime import datetime, timedelta, timezone

        soon = (datetime.now(timezone.utc) + timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            p = request.url.path
            if p == "/d2l/api/versions/":
                return _json(
                    [
                        {"ProductCode": "lp", "LatestVersion": "1.62"},
                        {"ProductCode": "le", "LatestVersion": "1.96"},
                    ]
                )
            if "myenrollments" in p:
                return _json(
                    [
                        {
                            "OrgUnit": {"Id": 111, "Name": "SFWRENG 2DA4",
                                        "Type": {"Name": "Course Offering"}},
                            "Access": {"IsActive": True, "CanAccess": True},
                        }
                    ]
                )
            if "mysubmissions" in p:
                if deny_submissions:
                    return httpx.Response(
                        403,
                        content='{"Errors":[{"Message":"Not authorized"}]}',
                        headers={"content-type": "application/json"},
                    )
                return _json([])
            if p.endswith("/dropbox/folders/"):
                return _json([{"Id": 7, "Name": "Assignment 3", "DueDate": soon}])
            if "calendar/events/myEvents" in p:
                return _json([{"Title": "Assignment 3", "EndDateTime": soon}])
            return _json([])

        return handler

    @pytest.mark.asyncio
    async def test_blocked_submissions_does_not_empty_the_view(
        self, tmp_path, monkeypatch
    ):
        ctx = _ctx_with(self._handler(deny_submissions=True), tmp_path, monkeypatch)
        try:
            from avenue_mcp.tools.assignments import get_upcoming_deadlines

            out = await get_upcoming_deadlines(ctx, days_ahead=14, include_submitted=False)
        finally:
            await ctx.aclose()

        assert out["count"] > 0, (
            "a blocked submissions route must not filter every deadline away"
        )
        assert all(d["submission_status"] == "unknown" for d in out["deadlines"])
        assert out["submission_status_available"] is False
        assert out["note"], "must explain why status is missing"

    @pytest.mark.asyncio
    async def test_working_submissions_still_reports_availability(
        self, tmp_path, monkeypatch
    ):
        ctx = _ctx_with(self._handler(deny_submissions=False), tmp_path, monkeypatch)
        try:
            from avenue_mcp.tools.assignments import get_upcoming_deadlines

            out = await get_upcoming_deadlines(ctx, days_ahead=14, include_submitted=False)
        finally:
            await ctx.aclose()

        assert out["submission_status_available"] is True
        assert all(d["submission_status"] != "unknown" for d in out["deadlines"])
