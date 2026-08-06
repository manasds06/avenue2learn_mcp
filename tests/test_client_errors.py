"""Client error-mapping tests.

The SessionExpired / PermissionDenied split is the load-bearing one: both mean
"you can't have this", but one is fixed by logging in and the other never will
be. Collapsing them sends users into a re-login loop against a permission wall.
"""

from __future__ import annotations

import httpx
import pytest

from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.client import models as m
from avenue_mcp.errors import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    SessionExpiredError,
    UpstreamError,
)


def resp(status: int, *, headers=None, text="", url="https://x/d2l/api/lp/1.0/thing"):
    request = httpx.Request("GET", url)
    return httpx.Response(status, headers=headers or {}, text=text, request=request)


class TestStatusMapping:
    def test_403_is_permission_not_auth(self):
        """A 403 on a healthy session is a permissions FACT to report."""
        with pytest.raises(PermissionDeniedError):
            D2LClient._raise_for_status(resp(403), "/x")

    def test_401_is_session_expired(self):
        with pytest.raises(SessionExpiredError):
            D2LClient._raise_for_status(resp(401), "/x")

    def test_404(self):
        with pytest.raises(NotFoundError):
            D2LClient._raise_for_status(resp(404), "/x")

    def test_400(self):
        with pytest.raises(InvalidRequestError):
            D2LClient._raise_for_status(resp(400), "/x")

    def test_413_is_invalid_request(self):
        with pytest.raises(InvalidRequestError):
            D2LClient._raise_for_status(resp(413), "/x")

    def test_500_is_upstream(self):
        with pytest.raises(UpstreamError):
            D2LClient._raise_for_status(resp(500), "/x")

    def test_success_passes(self):
        D2LClient._raise_for_status(resp(200), "/x")
        D2LClient._raise_for_status(resp(204), "/x")

    def test_html_error_body_not_leaked_into_message(self):
        r = resp(500, text="<html><body>Server Error page</body></html>")
        with pytest.raises(UpstreamError) as exc:
            D2LClient._raise_for_status(r, "/x")
        assert "html" not in str(exc.value).lower()


class TestExpiryDetection:
    """Do not trust status codes alone. Brightspace may answer an expired
    session with a 302 to SSO, or a 200 whose body is a login form."""

    def test_html_200_on_api_route_is_expiry(self):
        r = resp(200, headers={"content-type": "text/html; charset=utf-8"})
        with pytest.raises(SessionExpiredError):
            D2LClient._raise_for_session(r, "/d2l/api/lp/1.0/users/whoami")

    def test_json_200_is_fine(self):
        r = resp(200, headers={"content-type": "application/json"})
        D2LClient._raise_for_session(r, "/d2l/api/lp/1.0/users/whoami")

    def test_redirect_offsite_is_expiry(self):
        r = resp(302, headers={"location": "https://login.microsoftonline.com/x"})
        with pytest.raises(SessionExpiredError):
            D2LClient._raise_for_session(r, "/d2l/api/lp/1.0/thing")

    def test_redirect_to_login_path_is_expiry(self):
        r = resp(302, headers={"location": "/d2l/login?target=x"})
        with pytest.raises(SessionExpiredError):
            D2LClient._raise_for_session(r, "/d2l/api/lp/1.0/thing")

    def test_html_200_on_non_api_route_is_allowed(self):
        r = resp(200, headers={"content-type": "text/html"})
        D2LClient._raise_for_session(r, "/d2l/home")


class TestRetryPolicy:
    def test_upstream_retried(self):
        from avenue_mcp.client.d2l import _retryable

        assert _retryable(UpstreamError("500"))

    def test_permission_never_retried(self):
        from avenue_mcp.client.d2l import _retryable

        assert not _retryable(PermissionDeniedError("403"))

    def test_session_expiry_never_retried(self):
        from avenue_mcp.client.d2l import _retryable

        assert not _retryable(SessionExpiredError("401"))

    def test_not_found_never_retried(self):
        from avenue_mcp.client.d2l import _retryable

        assert not _retryable(NotFoundError("404"))

    def test_timeout_retried(self):
        from avenue_mcp.client.d2l import _retryable

        assert _retryable(httpx.TimeoutException("slow"))


class TestNormalization:
    def test_pick_first_present(self):
        assert m.pick({"b": 2}, "a", "b") == 2

    def test_pick_case_insensitive_fallback(self):
        assert m.pick({"orgunitid": 5}, "OrgUnitId") == 5

    def test_pick_default(self):
        assert m.pick({}, "a", default="x") == "x"

    def test_as_int_rejects_bool(self):
        assert m.as_int(True) is None
        assert m.as_int("7") == 7
        assert m.as_int("nope") is None

    def test_course_offering_filter_keeps_courses(self):
        assert m.is_course_offering({"Type": {"Name": "Course Offering"}})

    def test_course_offering_filter_drops_departments(self):
        assert not m.is_course_offering({"Type": {"Name": "Department"}})

    def test_role_normalization(self):
        assert m.normalize_role("Instructor") == "Instructor"
        assert m.normalize_role("Course Instructor") == "Instructor"
        assert m.normalize_role("Teaching Assistant") == "TA"
        assert m.normalize_role("TA") == "TA"
        assert m.normalize_role("Student") == "Student"
        assert m.normalize_role("Learner") == "Student"
        assert m.normalize_role(None) == "Unknown"
        assert m.normalize_role({"Name": "Instructor"}) == "Instructor"

    def test_link_topic_is_not_a_file(self):
        assert not m.topic_is_file({"TopicType": 3, "Url": "https://example.com"})

    def test_file_topic_is_a_file(self):
        assert m.topic_is_file({"TopicType": 1, "Url": "/content/outline.pdf"})

    def test_mime_from_name(self):
        assert m.mime_from_name("a.pdf") == "application/pdf"
        assert m.mime_from_name("a.unknownext") is None
        assert m.mime_from_name(None) is None
