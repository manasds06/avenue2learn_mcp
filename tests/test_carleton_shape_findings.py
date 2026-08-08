"""Response-shape assumptions that a second institution disproved.

Every bug here was silent on McMaster's shape and only appeared once a second
Brightspace instance was pointed at. The mock in test_integration_tools.py is
McMaster-shaped, which is exactly why none of these were caught: an assumption
you only ever test against the thing you derived it from is not tested at all.
"""

from __future__ import annotations

import httpx
import pytest

from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.client.roles import role_map, role_of_post


class TestRoleRecovery:
    """Posts with no role field must still get one, via the roster."""

    POST = {"PostId": 1, "PostingUserId": 6286, "Message": {"Html": "<p>hi</p>"}}

    def test_explicit_role_on_post_wins(self):
        post = {**self.POST, "AuthorRole": "Instructor"}
        assert role_of_post(post, {"6286": "Student"}) == "Instructor"

    def test_falls_back_to_roster_by_user_id(self):
        # Without this the post normalizes to "Unknown",
        # has_instructor_replies is always false, and the model cannot tell an
        # instructor's ruling from a classmate's guess.
        assert role_of_post(self.POST, {"6286": "Instructor"}) == "Instructor"

    def test_int_vs_str_user_ids_still_match(self):
        # PostingUserId is an int; classlist Identifier is a string.
        assert role_of_post(self.POST, {"6286": "TA 1"}) == "TA 1"

    def test_no_roster_degrades_to_none_not_a_guess(self):
        assert role_of_post(self.POST, {}) is None
        assert role_of_post(self.POST, None) is None

    def test_user_absent_from_roster_is_not_invented(self):
        # Someone who dropped the course is no longer on the roster. "Unknown"
        # is the honest answer; guessing "Student" would be worse.
        assert role_of_post(self.POST, {"9999": "Instructor"}) is None


class TestRoleMapPrivacy:
    """The roster is read for id -> role ONLY."""

    ROWS = [
        {
            "Identifier": "6286",
            "ClasslistRoleDisplayName": "Instructor",
            "DisplayName": "Real Person",
            "Email": "someone@example.ca",
            "OrgDefinedId": "100123456",
        }
    ]

    class _Client:
        def __init__(self, rows):
            self.rows = rows

        async def get_paged(self, component, suffix, **kw):
            return self.rows

    async def test_only_id_and_role_are_kept(self):
        out = await role_map(self._Client(self.ROWS), 1)
        assert out == {"6286": "Instructor"}

        # Nothing personal may survive the mapping. docs/07 requires the index
        # to carry roles and not names; this is where that is enforced.
        blob = repr(out)
        for leaked in ("Real Person", "someone@example.ca", "100123456"):
            assert leaked not in blob

    async def test_denied_classlist_degrades_quietly(self):
        from avenue_mcp.errors import PermissionDeniedError

        class Denied:
            async def get_paged(self, *a, **kw):
                raise PermissionDeniedError("instructor only")

        # A course that hides its roster loses role attribution. It must not
        # lose the ability to read the thread.
        assert await role_map(Denied(), 1) == {}


class TestHtmlDownloadIsNotALoginWall:
    """A .html course file is a file, not a sign-in page."""

    def _resp(self, headers):
        request = httpx.Request("GET", "https://x.test/d2l/api/le/1.96/1/content/topics/2/file")
        return httpx.Response(200, headers=headers, request=request)

    URL = "/d2l/api/le/1.96/1/content/topics/2/file"

    def test_html_with_attachment_disposition_is_allowed(self):
        # Measured on Carleton: an instructor-uploaded Scheduler.html returned
        # 200 + text/html + Content-Disposition, and the client declared the
        # session expired on a session that was alive -- sending the user to
        # re-login against a wall that did not exist.
        resp = self._resp(
            {
                "content-type": "text/html",
                "content-disposition": 'attachment; filename="Scheduler.html"',
            }
        )
        D2LClient._raise_for_session(resp, self.URL, expect_json=False)

    def test_html_without_disposition_is_still_a_login_wall(self):
        from avenue_mcp.errors import SessionExpiredError

        resp = self._resp({"content-type": "text/html"})
        with pytest.raises(SessionExpiredError):
            D2LClient._raise_for_session(resp, self.URL, expect_json=False)

    def test_json_routes_keep_the_strict_rule(self):
        from avenue_mcp.errors import SessionExpiredError

        # An API route answering HTML is a login wall even with a disposition
        # header -- JSON is the only acceptable answer there.
        resp = self._resp(
            {"content-type": "text/html", "content-disposition": "attachment"}
        )
        with pytest.raises(SessionExpiredError):
            D2LClient._raise_for_session(resp, "/d2l/api/lp/1.62/users/whoami")
