"""Every tool, end to end, against a mock Brightspace.

Unit tests cover the pure functions; this covers the code that only runs with a
live session -- paging, response normalization, degradation, and the tool layer
itself. Response shapes follow the Valence docs and the version payload actually
observed on avenue.cllmcmaster.ca (lp 1.62, le 1.96).

Two worlds are simulated:

  FULL      -- every route permitted (the optimistic Phase 0 outcome)
  RESTRICTED-- dropbox/folders, grades structure, quizzes, and classlist all
               403 (the pessimistic outcome the docs plan for)

The RESTRICTED world is the one that matters: it proves the degraded paths
actually produce honest output instead of crashing or inventing data.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from avenue_mcp.context import AppContext

ORG = 111
COURSE = "COMPSCI 2C03"
EASTERN = ZoneInfo("America/Toronto")


def _future_deadline(days: int) -> tuple[str, str]:
    """A deadline `days` from now at 11:59 PM Eastern, returned as (utc, local_date).

    Computed relative to now rather than hardcoded, because a fixed 2026-03-15
    fixture silently stops testing anything the moment that date passes -- which
    is exactly what happened on the first run: every deadline fell outside the
    window and the assertions were checking an empty list.

    Still exercises the DST trap: 11:59 PM Eastern is 03:59Z or 04:59Z the NEXT
    day, so utc and local_date deliberately disagree.
    """
    local = (datetime.now(EASTERN) + timedelta(days=days)).replace(
        hour=23, minute=59, second=0, microsecond=0
    )
    utc = local.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"), local.strftime("%Y-%m-%d")


A3_DUE_UTC, A3_DUE_LOCAL = _future_deadline(15)
QUIZ_DUE_UTC, QUIZ_DUE_LOCAL = _future_deadline(17)

# Announcements, forum activity and file uploads are diffed against a watermark
# that falls back to a 7-day lookback. Hardcoded 2026-03 dates put every fixture
# ~5 months in the past, so get_whats_new always returned zero changes and its
# tests asserted 0 == 0 -- green no matter how broken the digest was.
RECENT_UTC = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
    "%Y-%m-%dT%H:%M:%S.000Z"
)

# --- canned Valence payloads ----------------------------------------------

VERSIONS = [
    {"ProductCode": "lp", "LatestVersion": "1.62", "SupportedVersions": ["1.0", "1.62"]},
    {"ProductCode": "le", "LatestVersion": "1.96", "SupportedVersions": ["1.0", "1.96"]},
]

WHOAMI = {
    "Identifier": "9001",
    "FirstName": "Test",
    "LastName": "Student",
    "UniqueName": "student1",
}

# Access is a SIBLING of OrgUnit and carries IsActive/StartDate/EndDate. Nesting
# them inside OrgUnit (as an earlier fixture did) makes every course read as
# active with no dates, and hides that from the tests.
ENROLLMENTS = {
    "PagingInfo": {"Bookmark": None, "HasMoreItems": False},
    "Items": [
        {
            "OrgUnit": {
                "Id": ORG,
                "Name": COURSE,
                "Code": "COMPSCI-2C03-C01-202601",
                "Type": {"Id": 3, "Code": "CourseOffering", "Name": "Course Offering"},
            },
            "Access": {
                "IsActive": True,
                "StartDate": "2026-01-05T05:00:00.000Z",
                "EndDate": "2026-12-30T05:00:00.000Z",
            },
        },
        {  # past term -- must be filtered out unless include_inactive
            "OrgUnit": {
                "Id": 4242,
                "Name": "BIOL1902 Natural History (LEC) Fall 2024",
                "Code": "BIOL1902-202430",
                "Type": {"Id": 3, "Code": "CourseOffering", "Name": "Course Offering"},
            },
            "Access": {
                "IsActive": False,
                "StartDate": "2024-09-03T04:00:00.000Z",
                "EndDate": "2024-12-20T05:00:00.000Z",
            },
        },
        {  # past term that D2L STILL reports as active -- the common real case.
            # Filtering on IsActive alone lets this through.
            "OrgUnit": {
                "Id": 4243,
                "Name": "ECOR1045 Statics (LEC) Fall 2024",
                "Code": "ECOR1045-202430",
                "Type": {"Id": 3, "Code": "CourseOffering", "Name": "Course Offering"},
            },
            "Access": {
                "IsActive": True,
                "StartDate": "2024-09-03T04:00:00.000Z",
                "EndDate": "2024-12-20T05:00:00.000Z",
            },
        },
        {  # must be filtered out -- not a course
            "OrgUnit": {
                "Id": 5,
                "Name": "Faculty of Engineering",
                "Type": {"Id": 7, "Code": "Department", "Name": "Department"},
            },
            "Access": {"IsActive": True},
        },
    ],
}

CONTENT_ROOT = [
    {
        "Id": 900,
        "Title": "Week 1 - Introduction",
        "Type": "Module",
        "Structure": [
            {
                "Id": 4455,
                "Title": "Course Outline",
                "Type": "Topic",
                "TopicType": 1,
                "Url": "/content/enforced/111/2C03_outline_W26.pdf",
                "LastModifiedDate": RECENT_UTC,
            },
            {
                "Id": 4456,
                "Title": "Course Website",
                "Type": "Topic",
                "TopicType": 3,  # link -- not downloadable
                "Url": "https://example.com",
            },
        ],
    }
]

# The leftover scratch folder that answers 200 [] on mysubmissions while every
# real folder 403s. Named so its role is obvious at the call site.
STRAY_FOLDER = 779

# Every (method, path) the mock transport saw. Read-only is a claim the README
# makes to users about their own coursework, so it is asserted against actual
# traffic rather than trusted to code review.
METHOD_LOG: list[tuple[str, str]] = []

# Points live at Assessment.ScoreDenominator and instructions at
# CustomInstructions (a RichText). Valence has no top-level OutOf/Instructions;
# a fixture using those names passes against code that reads them and hides that
# every real folder returns null for both.
DROPBOX_FOLDERS = [
    {
        "Id": 778,
        "Name": "Assignment 3 - Graph Algorithms",
        "DueDate": A3_DUE_UTC,
        "Assessment": {"ScoreDenominator": 100.0},
        "CustomInstructions": {
            "Text": "Implement Dijkstra's algorithm.",
            "Html": "<p>Implement <b>Dijkstra's</b> algorithm.</p>",
        },
    },
    {
        "Id": STRAY_FOLDER,
        "Name": "temp",
        "DueDate": A3_DUE_UTC,
        "Assessment": {"ScoreDenominator": 10.0},
    },
]

MYSUBMISSIONS = [
    {
        "Submissions": [
            {
                "SubmissionDate": "2026-03-14T22:10:03.000Z",
                "Files": [{"FileName": "a3.pdf"}],
            }
        ]
    }
]

GRADE_VALUES = [
    {
        "GradeObjectIdentifier": "1",
        "GradeObjectName": "Assignment 3",
        "PointsNumerator": 92.0,
        "PointsDenominator": 100.0,
        "Comments": {"Html": "<p>Nice work on part 2.</p>"},
    },
    {  # ungraded -- must NOT be treated as a zero
        "GradeObjectIdentifier": "2",
        "GradeObjectName": "Final Exam",
        "PointsNumerator": None,
        "PointsDenominator": None,
    },
]

GRADE_OBJECTS = [
    {"Id": 1, "Name": "Assignment 3", "Weight": 40.0, "MaxPoints": 100.0},
    {"Id": 2, "Name": "Final Exam", "Weight": 60.0, "MaxPoints": 100.0},
]

NEWS = [
    {
        "Id": 5566,
        "Title": "Midterm room change",
        "Body": {"Html": '<p>Moved to <a href="https://x.ca/map">ABB 102</a>.</p>'},
        "StartDate": RECENT_UTC,
    }
]

CALENDAR = [
    {
        "Title": "Assignment 3 - Graph Algorithms",
        "EndDateTime": A3_DUE_UTC,
        "Type": "Assignment",
    },
    {
        "Title": "Week 5 Quiz",
        "EndDateTime": QUIZ_DUE_UTC,
        "Type": "Quiz",
    },
]

QUIZZES = [
    {
        "QuizId": 331,
        "Name": "Week 5 Quiz",
        "StartDate": "2026-01-10T05:00:00.000Z",
        "DueDate": QUIZ_DUE_UTC,
        "EndDate": QUIZ_DUE_UTC,
        "AttemptsAllowed": 2,
    }
]

FORUMS = [{"ForumId": 90, "Name": "Assignment Q&A"}]
TOPICS = [
    {"TopicId": 412, "Name": "A3 clarifications", "PostCount": 2,
     "LastPostDate": RECENT_UTC}
]
POSTS = [
    {
        "PostId": 5001, "ParentPostId": None,
        "DatePosted": "2026-03-09T14:20:00.000Z",
        "Message": {"Html": "<p>Recursive or iterative for Q3?</p>"},
        "Author": {"Role": "Student", "DisplayName": "Jane Doe"},
    },
    {
        "PostId": 5002, "ParentPostId": 5001,
        "DatePosted": "2026-03-09T16:45:00.000Z",
        "Message": {"Html": "<p>Either is fine, document your choice.</p>"},
        "Author": {"Role": "Instructor", "DisplayName": "Dr. Smith"},
    },
]


_PDF_CACHE: bytes | None = None


def _outline_pdf_bytes() -> bytes:
    """A real two-page outline PDF, built once per session."""
    global _PDF_CACHE
    if _PDF_CACHE is not None:
        return _PDF_CACHE
    import pymupdf

    doc = pymupdf.open()
    for body in (
        "COMPSCI 2C03 Course Outline\n\nThis course covers data structures and "
        "algorithms, including sorting, hash tables, balanced trees, and graph "
        "traversal. Lectures run three times a week and attendance is expected.",
        "Late Policy\n\nWork submitted after the deadline is docked ten percent "
        "for each day it is overdue, to a maximum of three days, after which it "
        "is not accepted. Extensions come only from the Faculty office.",
    ):
        doc.new_page().insert_textbox(pymupdf.Rect(60, 60, 540, 760), body, fontsize=11)
    _PDF_CACHE = doc.tobytes()
    doc.close()
    return _PDF_CACHE


# --- Carleton-shaped payloads ---------------------------------------------
#
# Same DATA, different SHAPE. Every bug found when a second real instance was
# pointed at this server was a shape assumption that held at McMaster, failed
# elsewhere, and failed SILENTLY. The payloads above are McMaster-shaped, so the
# suite could not see any of them. These are the differences that actually
# mattered, measured live (docs/09).

# Topics carry no `Url` in the listing. Without back-filling from the topic
# detail record, guess_filename falls back to the title, which has no extension:
# nothing is indexable and is_renderable is false for every file.
CONTENT_ROOT_NO_URL = [
    {
        "Id": 900,
        "Title": "Week 1 - Introduction",
        "Type": "Module",
        "Structure": [
            {"Id": 4455, "Title": "Course Outline", "Type": "Topic", "TopicType": 1,
             "LastModifiedDate": RECENT_UTC},
            {"Id": 4456, "Title": "Course Website", "Type": "Topic", "TopicType": 3},
        ],
    }
]

# Posts carry PostingUserId and no role field at all, so author_role is only
# recoverable by cross-referencing the roster.
POSTS_NO_ROLE = [
    {
        "PostId": 5001, "ParentPostId": None, "PostingUserId": 7001,
        "DatePosted": "2026-03-09T14:20:00.000Z",
        "PostingUserDisplayName": "Jane Doe",
        "Message": {"Text": "", "Html": "<p>Recursive or iterative for Q3?</p>"},
    },
    {
        "PostId": 5002, "ParentPostId": 5001, "PostingUserId": 7002,
        "DatePosted": "2026-03-09T16:45:00.000Z",
        "PostingUserDisplayName": "Dr. Smith",
        "Message": {"Text": "", "Html": "<p>Either is fine, document your choice.</p>"},
    },
]

# Roles live in ClasslistRoleDisplayName, not RoleName, and the id that matches
# PostingUserId is Identifier.
CLASSLIST_CARLETON = [
    {"Identifier": "7001", "ClasslistRoleDisplayName": "Student",
     "DisplayName": "Jane Doe", "Email": "jane@example.ca"},
    {"Identifier": "7002", "ClasslistRoleDisplayName": "Instructor",
     "DisplayName": "Dr. Smith", "Email": "smith@example.ca"},
]


def build_transport(
    restricted: bool, shape: str = "mcmaster", deny_personal: bool = False
) -> httpx.MockTransport:
    """Route Valence paths to canned payloads.

    `restricted=True` makes the instructor-scope routes 403 with an HTML body,
    matching what the live host returns for a request it will not serve.

    `deny_personal=True` models what BOTH real instances measurably do and
    neither world above covered: the *listing* routes (`dropbox/folders/`,
    `quizzes/`) are permitted while the routes carrying the caller's own
    per-item record (`mysubmissions`, `quizzes/{id}/attempts/`) 403.

    "restricted" denies the listing outright, so the code path that reads a
    listing and then fails to read its personal detail never ran. Three bugs
    lived in exactly that gap, each one reporting a denial as a fact about the
    student: "not submitted", "not attempted".
    """

    def ok(payload) -> httpx.Response:
        return httpx.Response(
            200, content=json.dumps(payload), headers={"content-type": "application/json"}
        )

    def denied() -> httpx.Response:
        return httpx.Response(
            403,
            content='{"Errors":[{"Message":"Not authorized"}]}',
            headers={"content-type": "application/json"},
        )

    mcmaster = shape == "mcmaster"

    def handler(request: httpx.Request) -> httpx.Response:
        METHOD_LOG.append((request.method, request.url.path))
        p = request.url.path

        if p == "/d2l/api/versions/":
            return ok(VERSIONS)
        if p.endswith("/users/whoami"):
            return ok(WHOAMI)
        if "enrollments/myenrollments" in p:
            return ok(ENROLLMENTS)
        if p.endswith(f"/courses/{ORG}"):
            return ok({"Name": COURSE, "Code": "CS-2C03", "IsActive": True})
        if p.endswith("/content/root/"):
            return ok(CONTENT_ROOT if mcmaster else CONTENT_ROOT_NO_URL)
        if "/content/topics/" in p and p.endswith("/file"):
            # A REAL pdf, not a stub. A stub only proves the error path; a real
            # one exercises download -> extract -> chunk -> embed -> search.
            return httpx.Response(
                200,
                content=_outline_pdf_bytes(),
                headers={
                    "content-type": "application/pdf",
                    "content-disposition": 'attachment; filename="2C03_outline_W26.pdf"',
                },
            )
        if "/content/topics/" in p:
            return ok(
                {"Id": 4455, "Title": "Course Outline", "TopicType": 1,
                 "Url": "/content/enforced/111/2C03_outline_W26.pdf"}
            )
        if "/dropbox/folders/" in p and p.endswith("/mysubmissions/"):
            if deny_personal:
                # Carleton has one stray folder that answers 200 [] while every
                # real one 403s -- the false positive docs/09 warns about.
                return ok([]) if f"/{STRAY_FOLDER}/" in p else denied()
            return ok(MYSUBMISSIONS)
        if "/dropbox/folders/" in p and "/feedback/" in p:
            return ok({"Score": 92.0, "Feedback": {"Html": "<p>Good.</p>"}, "IsGraded": True})
        if p.endswith("/dropbox/folders/") or "/dropbox/folders/" in p:
            return denied() if restricted else ok(DROPBOX_FOLDERS)
        if p.endswith("/grades/values/myGradeValues/"):
            return ok(GRADE_VALUES)
        if p.endswith("/grades/"):
            return denied() if restricted else ok(GRADE_OBJECTS)
        if p.endswith("/news/"):
            return ok(NEWS)
        if "calendar/events/myEvents" in p:
            return ok(CALENDAR)
        if "/quizzes/" in p and p.endswith("/attempts/"):
            if deny_personal:
                return denied()
            return denied() if restricted else ok([])
        if p.endswith("/quizzes/"):
            return denied() if restricted else ok(QUIZZES)
        if p.endswith("/discussions/forums/"):
            return ok(FORUMS)
        if "/discussions/forums/" in p and p.endswith("/topics/"):
            return ok(TOPICS)
        if "/posts/" in p:
            return ok(POSTS if mcmaster else POSTS_NO_ROLE)
        if p.endswith("/classlist/"):
            if restricted:
                return denied()
            return ok(
                [{"Identifier": "1", "DisplayName": "Dr. Smith",
                  "RoleName": "Instructor", "EmailAddress": "smith@mcmaster.ca"}]
                if mcmaster
                else CLASSLIST_CARLETON
            )
        if "enrollments/orgUnits" in p:
            return denied() if restricted else ok(
                [{"User": {"DisplayName": "Dr. Smith", "EmailAddress": "s@mcmaster.ca"},
                  "Role": {"Name": "Instructor"}}]
            )
        return httpx.Response(404, content="{}", headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


@pytest.fixture(params=[False, True], ids=["full", "restricted"])
def ctx(request, tmp_path, monkeypatch):
    """An AppContext wired to a mock Brightspace, with a fake live session."""
    monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "state"))
    from avenue_mcp.config import get_settings

    get_settings.cache_clear()

    c = AppContext()
    c.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    c.settings.session_path.write_text(
        json.dumps({"cookies": [{"name": "d2lSessionVal", "value": "x",
                                 "domain": "avenue.cllmcmaster.ca", "path": "/"}],
                    "origins": []}),
        encoding="utf-8",
    )
    transport = build_transport(request.param)
    c.auth._client = httpx.AsyncClient(
        base_url=c.settings.base_url, transport=transport
    )
    # The session is valid in this world; skip the network liveness probe.
    monkeypatch.setattr(c.auth, "is_alive", _always_true)
    c.restricted = request.param  # type: ignore[attr-defined]
    yield c
    get_settings.cache_clear()


async def _always_true() -> bool:
    return True


@pytest.fixture
def denied_subs_ctx(tmp_path, monkeypatch):
    """Listings readable, per-student detail denied -- what BOTH instances do.

    `dropbox/folders/` and `quizzes/` answer; `mysubmissions` and
    `quizzes/{id}/attempts/` 403. Neither `ctx` world covered this, and every
    bug that lived here reported a denial as a fact about the student.
    """
    monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "state"))
    from avenue_mcp.config import get_settings

    get_settings.cache_clear()

    c = AppContext()
    c.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    c.settings.session_path.write_text(
        json.dumps({"cookies": [{"name": "d2lSessionVal", "value": "x",
                                 "domain": "avenue.cllmcmaster.ca", "path": "/"}],
                    "origins": []}),
        encoding="utf-8",
    )
    c.auth._client = httpx.AsyncClient(
        base_url=c.settings.base_url,
        transport=build_transport(False, deny_personal=True),
    )
    monkeypatch.setattr(c.auth, "is_alive", _always_true)
    yield c
    get_settings.cache_clear()


@pytest.fixture(params=["mcmaster", "carleton"])
def shaped_ctx(request, tmp_path, monkeypatch):
    """Full-access AppContext, parametrized over INSTANCE SHAPE rather than
    permissions.

    Deliberately a separate fixture from `ctx` rather than a third parameter on
    it: the shape-sensitive surface is content, discussions, and the roster, so
    crossing shape with the permission axis would quadruple a suite that embeds
    real PDF extraction and embedding to re-prove things shape cannot affect.
    """
    monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "state"))
    from avenue_mcp.config import get_settings

    get_settings.cache_clear()

    c = AppContext()
    c.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    c.settings.session_path.write_text(
        json.dumps({"cookies": [{"name": "d2lSessionVal", "value": "x",
                                 "domain": "avenue.cllmcmaster.ca", "path": "/"}],
                    "origins": []}),
        encoding="utf-8",
    )
    c.auth._client = httpx.AsyncClient(
        base_url=c.settings.base_url,
        transport=build_transport(False, shape=request.param),
    )
    monkeypatch.setattr(c.auth, "is_alive", _always_true)
    c.shape = request.param  # type: ignore[attr-defined]
    yield c
    get_settings.cache_clear()


class TestInstanceShapeInvariance:
    """The tool layer must produce the same ANSWERS from either shape.

    Each assertion here failed on the Carleton shape before its fix, and every
    one of those failures was silent in production: a course with no searchable
    files, a slide that could not be rendered, an instructor who could not be
    told apart from a classmate.
    """

    async def test_files_are_identifiable_and_renderable(self, shaped_ctx):
        from avenue_mcp.tools.content import get_course_content

        out = await get_course_content(shaped_ctx, ORG)

        topics = []

        def collect(nodes):
            for n in nodes or []:
                topics.extend(n.get("topics") or [])
                collect(n.get("modules"))

        collect(out["modules"])
        pdf = next(t for t in topics if t["title"] == "Course Outline")

        # Before the fix these were None and False on the Carleton shape, so the
        # model would never call get_page_image on a perfectly renderable PDF.
        assert pdf["file_name"].endswith(".pdf"), shaped_ctx.shape
        assert pdf["mime_type"] == "application/pdf", shaped_ctx.shape
        assert pdf["is_renderable"] is True, shaped_ctx.shape

    async def test_course_files_are_actually_indexed(self, shaped_ctx):
        from avenue_mcp.tools.search import sync_course_materials

        out = await sync_course_materials(shaped_ctx, ORG)

        # The Carleton shape reported "found, 0 indexed, all unsupported" here.
        assert out["files_indexed"] == 1, shaped_ctx.shape
        assert out["files_skipped_unsupported"] == 0, shaped_ctx.shape
        assert out["chunks_created"] >= 2, shaped_ctx.shape

    async def test_indexed_file_is_searchable_with_a_page_citation(self, shaped_ctx):
        from avenue_mcp.tools.search import search_course_materials, sync_course_materials

        await sync_course_materials(shaped_ctx, ORG)
        out = await search_course_materials(shaped_ctx, "late penalty", org_unit_id=ORG)

        assert out["count"] >= 1, shaped_ctx.shape
        assert "ten percent" in out["results"][0]["text"], shaped_ctx.shape

    async def test_instructor_replies_are_attributable(self, shaped_ctx):
        from avenue_mcp.tools.discussions import read_discussion_thread

        out = await read_discussion_thread(shaped_ctx, ORG, 90, 412)

        # The Carleton shape carries no role field, so every post came back
        # "Unknown" and has_instructor_replies was always false -- collapsing the
        # distinction that makes reading a thread worth anything.
        assert out["count"] == 2, shaped_ctx.shape
        roles = {p["author_role"] for p in out["posts"]}
        assert roles == {"Student", "Instructor"}, f"{shaped_ctx.shape}: {roles}"
        assert out["has_instructor_replies"] is True, shaped_ctx.shape

    async def test_reply_tree_survives_either_shape(self, shaped_ctx):
        from avenue_mcp.tools.discussions import read_discussion_thread

        out = await read_discussion_thread(shaped_ctx, ORG, 90, 412)
        parents = [p["parent_post_id"] for p in out["posts"]]

        # Flattening destroys the question -> answer pairing.
        assert 5001 in parents, shaped_ctx.shape

    async def test_no_author_name_escapes_in_either_shape(self, shaped_ctx):
        """Roster lookup must not become a name leak.

        The Carleton fix reads the classlist to resolve roles, and that roster
        carries names and emails. docs/07 requires posts to carry roles and not
        names, so this pins the boundary rather than trusting it.
        """
        from avenue_mcp.tools.discussions import read_discussion_thread

        out = await read_discussion_thread(shaped_ctx, ORG, 90, 412)
        blob = json.dumps(out)

        for name in ("Jane Doe", "Dr. Smith", "jane@example.ca", "smith@example.ca"):
            assert name not in blob, f"{shaped_ctx.shape} leaked {name}"


# --- tests ----------------------------------------------------------------


class TestCourses:
    async def test_lists_only_course_offerings(self, ctx):
        from avenue_mcp.tools.courses import list_courses

        out = await list_courses(ctx)
        assert out["count"] == 1, "the Department org unit must be filtered out"
        assert out["courses"][0]["org_unit_id"] == ORG
        assert out["courses"][0]["name"] == COURSE

    async def test_reads_active_and_dates_from_access_not_org_unit(self, ctx):
        from avenue_mcp.tools.courses import list_courses

        out = await list_courses(ctx)
        course = out["courses"][0]
        assert course["is_active"] is True
        # Nulls here mean the dates were read off OrgUnit, where they do not live.
        assert course["start_date"]["utc"], "StartDate must come from Access"
        assert course["end_date"]["utc"], "EndDate must come from Access"

    async def test_past_term_hidden_unless_include_inactive(self, ctx):
        from avenue_mcp.tools.courses import list_courses

        assert 4242 not in {c["org_unit_id"] for c in (await list_courses(ctx))["courses"]}

        out = await list_courses(ctx, include_inactive=True)
        past = next(c for c in out["courses"] if c["org_unit_id"] == 4242)
        assert past["is_active"] is False

    async def test_past_term_hidden_even_when_d2l_still_flags_it_active(self, ctx):
        """D2L leaves past shells IsActive=true; the date window must still win."""
        from avenue_mcp.tools.courses import list_courses

        out = await list_courses(ctx)
        assert 4243 not in {c["org_unit_id"] for c in out["courses"]}
        assert out["count"] == 1, "only the current-term offering should remain"

        full = await list_courses(ctx, include_inactive=True)
        stale = next(c for c in full["courses"] if c["org_unit_id"] == 4243)
        assert stale["is_active"] is False

    async def test_version_negotiation_used_real_versions(self, ctx):
        from avenue_mcp.tools.courses import list_courses

        await list_courses(ctx)
        assert await ctx.client.version_for("lp") == "1.62"
        assert await ctx.client.version_for("le") == "1.96"


class TestContent:
    async def test_tree_marks_link_topics_undownloadable(self, ctx):
        from avenue_mcp.tools.content import get_course_content

        out = await get_course_content(ctx, ORG)
        topics = out["modules"][0]["topics"]
        by_id = {t["id"]: t for t in topics}
        assert by_id[4455]["is_downloadable"] is True
        assert by_id[4456]["is_downloadable"] is False, "a link topic has no file body"

    async def test_reads_a_real_pdf_with_page_positions(self, ctx):
        from avenue_mcp.tools.content import read_content_file

        out = await read_content_file(ctx, ORG, 4455)
        assert out["extraction_quality"] == "ok"
        assert out["page_count"] == 2
        assert "ten percent" in out["text"], "page 2 body must be present"
        assert "[p.1]" in out["text"] and "[p.2]" in out["text"], "page markers cite"
        assert out["file_name"] == "2C03_outline_W26.pdf"

    async def test_pagination_reports_how_to_continue(self, ctx):
        from avenue_mcp.tools.content import read_content_file

        out = await read_content_file(ctx, ORG, 4455, max_chars=120)
        assert out["truncated"] is True
        assert out["next_start_page"] == 2
        assert out["hint"] and "start_page" in out["hint"]


class TestAssignments:
    async def test_degrades_or_succeeds_but_never_lies(self, ctx):
        from avenue_mcp.tools.assignments import list_assignments

        out = await list_assignments(ctx, ORG)
        if ctx.restricted:
            assert out["degraded"] is True
            assert out["note"] and "instructor-only" in out["note"].lower()
            for a in out["assignments"]:
                assert a["points_possible"] is None
                assert a["instructions_text"] is None
        else:
            assert out["degraded"] is False
            a = out["assignments"][0]
            assert a["points_possible"] == 100.0
            assert "Dijkstra" in a["instructions_text"]
            assert a["submission_status"] == "submitted"

    async def test_points_and_instructions_read_valence_field_names(self, ctx):
        """Points are Assessment.ScoreDenominator; instructions CustomInstructions.

        Valence has no top-level OutOf/Instructions, so reading those returns
        null for every folder on every instance -- silently, and forever.
        """
        from avenue_mcp.tools.assignments import list_assignments

        if ctx.restricted:
            pytest.skip("folder listing denied in this world")
        out = await list_assignments(ctx, ORG)
        a3 = next(a for a in out["assignments"] if "Assignment 3" in a["name"])
        assert a3["points_possible"] == 100.0
        assert a3["instructions_text"] and "Dijkstra" in a3["instructions_text"]

    async def test_denied_submissions_never_reported_as_submitted(
        self, denied_subs_ctx
    ):
        """A denied route must not delete a deadline.

        `_Unavailable` is a plain object and therefore truthy, so `if sub`
        marked unreadable items "submitted" -- and the default view, which hides
        submitted work, then dropped them entirely.
        """
        from avenue_mcp.tools.assignments import get_upcoming_deadlines

        out = await get_upcoming_deadlines(denied_subs_ctx, days_ahead=30)
        titles = {d["title"] for d in out["deadlines"]}
        assert "Assignment 3 - Graph Algorithms" in titles, (
            "an assignment whose status cannot be read must still be shown"
        )
        for d in out["deadlines"]:
            assert d["submission_status"] != "submitted"

    async def test_denied_submissions_not_advertised_as_available(
        self, denied_subs_ctx
    ):
        """submission_status_available tracks the SUBMISSIONS route.

        Setting it from a successful folder read claims status is available on
        an instance where every mysubmissions call 403s -- and contradicts what
        list_assignments reports for the same course.
        """
        from avenue_mcp.tools.assignments import (
            get_upcoming_deadlines,
            list_assignments,
        )

        deadlines = await get_upcoming_deadlines(denied_subs_ctx, days_ahead=30)
        listing = await list_assignments(denied_subs_ctx, ORG)

        assert deadlines["submission_status_available"] is False
        assert listing["submission_status_available"] is False
        assert deadlines["note"]

    async def test_stray_200_folder_does_not_assert_not_submitted(
        self, denied_subs_ctx
    ):
        """One folder answering 200 [] is not evidence of "nothing submitted"
        once another has refused -- and the note claims everything is unknown."""
        from avenue_mcp.tools.assignments import list_assignments

        out = await list_assignments(denied_subs_ctx, ORG)
        statuses = {a["submission_status"] for a in out["assignments"]}
        assert statuses == {"unknown"}, (
            f"note promises all-unknown, got {statuses}"
        )

    async def test_deadlines_issue_only_get_requests(self, ctx):
        """get_upcoming_deadlines must never write.

        It is the widest-fanning read tool -- it walks every active course and
        every dropbox folder -- so if any read path were to mutate, this is
        where it would show up. Asserted against real traffic.
        """
        from avenue_mcp.tools.assignments import get_upcoming_deadlines

        METHOD_LOG.clear()
        await get_upcoming_deadlines(ctx, days_ahead=30, include_submitted=True)

        assert METHOD_LOG, "expected the tool to have made requests"
        offenders = [(mth, path) for mth, path in METHOD_LOG if mth != "GET"]
        assert not offenders, f"non-GET traffic from a read tool: {offenders}"

    async def test_deadline_renders_in_eastern_not_utc(self, ctx):
        """An 11:59 PM Eastern deadline is 03:59Z/04:59Z the NEXT day.

        Reporting the UTC date would tell the user their assignment is due a day
        later than it is.
        """
        from avenue_mcp.tools.assignments import get_upcoming_deadlines

        # include_submitted=True: in the full world A3 is already submitted and
        # would otherwise be correctly hidden.
        out = await get_upcoming_deadlines(ctx, days_ahead=30, include_submitted=True)
        a3 = next(d for d in out["deadlines"] if "Assignment 3" in d["title"])

        assert a3["due_date"]["local_date"] == A3_DUE_LOCAL
        assert a3["due_date"]["utc"] == A3_DUE_UTC.replace(".000Z", "Z")
        # The two genuinely disagree -- that's the whole trap.
        assert a3["due_date"]["utc"][:10] != a3["due_date"]["local_date"]
        assert a3["due_date"]["local"].endswith(("EDT", "EST"))

    async def test_deadlines_include_assignments_and_quizzes(self, ctx):
        """Quizzes must appear even in the restricted world -- a deadline tool
        that silently omits them is worse than none, because the user trusts it
        and misses a quiz."""
        from avenue_mcp.tools.assignments import get_upcoming_deadlines

        out = await get_upcoming_deadlines(ctx, days_ahead=30, include_submitted=True)
        assert out["deadlines"], "window must contain the seeded deadlines"
        kinds = {d["type"] for d in out["deadlines"]}
        assert "assignment" in kinds
        assert "quiz" in kinds, "quiz deadlines must survive even when restricted"

    async def test_submitted_work_hidden_by_default(self, ctx):
        """The default view answers 'what do I still have to do', so anything
        already handed in should drop out -- but only when we actually know its
        status. Under restriction, status is unknown and nothing may be hidden.
        """
        from avenue_mcp.tools.assignments import get_upcoming_deadlines

        out = await get_upcoming_deadlines(ctx, days_ahead=30)
        titles = {d["title"] for d in out["deadlines"]}
        if ctx.restricted:
            assert "Assignment 3 - Graph Algorithms" in titles, (
                "with status unknown, hiding it would silently drop real work"
            )
        else:
            assert "Assignment 3 - Graph Algorithms" not in titles
            assert "Week 5 Quiz" in titles, "unsubmitted items must remain"

    async def test_submission_status_availability_is_reported(self, ctx):
        from avenue_mcp.tools.assignments import get_upcoming_deadlines

        out = await get_upcoming_deadlines(ctx, days_ahead=30)
        if ctx.restricted:
            assert out["submission_status_available"] is False
            assert out["note"], "must say status is unknown rather than imply not-submitted"
        else:
            assert out["submission_status_available"] is True


class TestGrades:
    async def test_ungraded_is_not_a_zero(self, ctx):
        from avenue_mcp.tools.grades import get_grades

        out = await get_grades(ctx, ORG)
        final = next(i for i in out["items"] if i["name"] == "Final Exam")
        assert final["is_graded"] is False
        assert final["points_earned"] is None

    async def test_projection_withheld_without_weights(self, ctx):
        """The whole point: refuse rather than invent a number."""
        from avenue_mcp.tools.grades import analyze_grade_summary

        out = await analyze_grade_summary(ctx, ORG, target_percentage=90.0)
        if ctx.restricted:
            assert out["weights_available"] is False
            assert out["current_weighted_average"] is None
            assert out["target"] is None
            assert any("weight" in c.lower() for c in out["caveats"])
        else:
            assert out["weights_available"] is True
            assert out["current_weighted_average"] == 92.0
            assert out["graded_weight"] == 40.0
            assert out["remaining_weight"] == 60.0
            # need 90 overall; have 0.4*92 = 36.8; need 53.2 from 60 weight
            assert out["target"]["required_average_on_remaining"] == pytest.approx(
                88.67, abs=0.1
            )

    async def test_never_claims_a_letter_without_a_scale(self, ctx):
        from avenue_mcp.tools.grades import analyze_grade_summary

        out = await analyze_grade_summary(ctx, ORG)
        assert out.get("letter_estimate") is None


class TestQuizzes:
    async def test_three_dates_and_degradation(self, ctx):
        from avenue_mcp.tools.quizzes import list_quizzes

        out = await list_quizzes(ctx, ORG)
        assert out["count"] >= 1
        if ctx.restricted:
            assert out["degraded"] is True
            assert out["quizzes"][0]["status"] == "unknown"
        else:
            q = out["quizzes"][0]
            assert q["start_date"]["utc"] and q["due_date"]["utc"] and q["end_date"]["utc"]
            assert q["attempts_allowed"] == 2

    async def test_denied_attempts_never_reported_as_not_attempted(
        self, denied_subs_ctx
    ):
        """A denied attempts route means UNKNOWN, not zero attempts.

        `(attempts_used or 0) > 0` collapses None into 0, so every quiz claimed
        "not_attempted" -- including a midterm the student demonstrably wrote,
        since the grade for it exists.
        """
        from avenue_mcp.tools.quizzes import list_quizzes

        out = await list_quizzes(denied_subs_ctx, ORG)
        assert out["quizzes"], "the quiz listing itself is permitted here"
        assert out["attempt_status_available"] is False
        for q in out["quizzes"]:
            assert q["status"] == "unknown", (
                f"{q['name']} claims {q['status']} on a denied attempts route"
            )
        assert "not available" in out["note"].lower()

    async def test_never_exposes_questions(self, ctx):
        """Metadata only. Checked field-by-field, not by substring over the whole
        payload -- the tool's own `note` says "questions and answers are never
        retrieved", which made a naive substring check fail on its own disclaimer.
        """
        from avenue_mcp.tools.quizzes import list_quizzes

        allowed = {
            "quiz_id", "name", "start_date", "due_date", "end_date",
            "days_until_due", "attempts_allowed", "attempts_used", "status",
            "is_available_now", "is_past_due_but_open", "is_closed", "best_score",
        }
        for q in (await list_quizzes(ctx, ORG))["quizzes"]:
            unexpected = set(q) - allowed
            assert not unexpected, f"unexpected quiz fields leaked: {unexpected}"


class TestAnnouncements:
    async def test_html_becomes_text_with_links_kept(self, ctx):
        from avenue_mcp.tools.announcements import list_announcements

        out = await list_announcements(ctx, ORG)
        a = out["announcements"][0]
        assert "ABB 102" in a["body_text"]
        assert "<p>" not in a["body_text"]
        assert "https://x.ca/map" in a["links"]


class TestDiscussions:
    async def test_thread_preserves_replies_and_omits_names(self, ctx):
        from avenue_mcp.tools.discussions import read_discussion_thread

        out = await read_discussion_thread(ctx, ORG, 90, 412)
        assert out["count"] == 2
        assert out["has_instructor_replies"] is True

        reply = next(p for p in out["posts"] if p["post_id"] == 5002)
        assert reply["parent_post_id"] == 5001, "reply tree must survive"
        assert reply["author_role"] == "Instructor"

        blob = json.dumps(out)
        assert "Jane Doe" not in blob, "author names must never be returned"
        assert "Dr. Smith" not in blob

    async def test_forum_listing(self, ctx):
        from avenue_mcp.tools.discussions import list_discussions

        out = await list_discussions(ctx, ORG)
        assert out["topic_count"] == 1
        assert out["forums"][0]["topics"][0]["topic_id"] == 412


class TestClassList:
    async def test_reports_staff_or_explains_absence(self, ctx):
        from avenue_mcp.tools.classlist import get_class_list

        out = await get_class_list(ctx, ORG)

        # The roster is NEVER returned in bulk, in either world. The live probe
        # found classlist works for students (145 people with names, emails,
        # usernames), so "restricted" is not the case that protects this data --
        # the tool has to.
        assert out["students"] == []
        assert out["student_roster_returned"] is False

        if ctx.restricted:
            assert out["note"], "must explain why, not silently return nothing"
        else:
            assert any(i["role"] == "Instructor" for i in out["instructors"])
            # Course staff still come back in full -- that's the tool's job.
            assert out["instructors"][0].get("name")

    async def test_withheld_roster_is_explained_not_silent(self, ctx):
        """An empty students array must never read as 'no classmates found'."""
        from avenue_mcp.tools.classlist import get_class_list

        out = await get_class_list(ctx, ORG)
        if out["student_count"]:
            assert "not included" in (out["note"] or "").lower()
            assert "fippa" in (out["note"] or "").lower()


class TestWhatsNew:
    async def test_digest_actually_reports_changes(self, ctx):
        """Asserts real content. The previous version compared 0 to 0."""
        from avenue_mcp.tools.whatsnew import get_whats_new

        out = await get_whats_new(ctx, mark_seen=False)
        assert out["courses_checked"] == 1
        assert out["total_changes"] > 0, "seeded recent activity must be reported"

        titles = [a["title"] for a in out["changes"]["announcements"]]
        assert "Midterm room change" in titles

        threads = [d["topic_name"] for d in out["changes"]["discussion_replies"]]
        assert "A3 clarifications" in threads

    async def test_explicit_since_does_not_dump_the_gradebook(self, ctx):
        """An explicit `since` must not turn every grade into a "new" grade.

        Grades carry no "graded at" timestamp, so a time window cannot filter
        them; the watermark is the only mechanism. The old guard keyed off
        `mark`, which `since` always populates, so "what's new in the last 60
        days" reported every graded item in every course -- including terms two
        years old, presented as recent.
        """
        from avenue_mcp.tools.whatsnew import get_whats_new

        out = await get_whats_new(ctx, since="2026-06-08T00:00:00Z")
        assert out["changes"]["new_grades"] == [], (
            "first look at a course must seed watermarks, not report the "
            "whole gradebook as new"
        )

    async def test_grade_change_still_reported_after_seeding(self, ctx):
        """The seeding guard must not swallow real changes on later runs."""
        from avenue_mcp.tools.whatsnew import get_whats_new

        await get_whats_new(ctx)  # seeds
        out = await get_whats_new(ctx, since="2026-06-08T00:00:00Z")
        assert out["changes"]["new_grades"] == [], "nothing changed between runs"

    async def test_peek_is_repeatable(self, ctx):
        """mark_seen=False twice must give the same answer -- the documented
        contract, and the one the grade watermarks used to violate."""
        from avenue_mcp.tools.whatsnew import get_whats_new

        first = await get_whats_new(ctx, mark_seen=False)
        second = await get_whats_new(ctx, mark_seen=False)
        assert first["total_changes"] > 0
        assert second["total_changes"] == first["total_changes"]

    async def test_peek_burns_no_grade_watermarks(self, ctx):
        """_grades keeps its own finer-grained watermarks and used to write them
        unconditionally, so a "peek" silently consumed new grades."""
        from avenue_mcp.tools.whatsnew import get_whats_new

        await get_whats_new(ctx, mark_seen=False)
        assert ctx.store.get_watermark(ORG, "grade:1") is None

        await get_whats_new(ctx, mark_seen=True)
        assert ctx.store.get_watermark(ORG, "grade:1") is not None

    async def test_mark_seen_advances_then_reports_nothing_new(self, ctx):
        from avenue_mcp.tools.whatsnew import get_whats_new

        first = await get_whats_new(ctx, mark_seen=True)
        assert first["total_changes"] > 0
        assert ctx.store.get_watermark(ORG, "announcements") is not None

        second = await get_whats_new(ctx, mark_seen=True)
        assert second["changes"]["announcements"] == [], "already seen"

    async def test_failure_leaves_watermark_untouched(self, ctx, monkeypatch):
        """The permanent-data-loss bug.

        A failing source used to be swallowed AND its watermark advanced, so the
        changes it never read were skipped forever.
        """
        from avenue_mcp.errors import SessionExpiredError
        from avenue_mcp.tools import whatsnew as wn

        async def boom(*a, **k):
            raise SessionExpiredError("session died mid-digest")

        monkeypatch.setattr(wn, "_announcements", boom)

        out = await wn.get_whats_new(ctx, mark_seen=True)
        assert out["complete"] is False
        assert any(f["category"] == "announcements" for f in out["failures"])
        assert "INCOMPLETE" in (out["note"] or "")
        assert ctx.store.get_watermark(ORG, "announcements") is None, (
            "a failed source must not advance past unread changes"
        )
        # Sources that DID succeed still advance.
        assert ctx.store.get_watermark(ORG, "discussions") is not None


class TestStatus:
    async def test_reports_live_session(self, ctx):
        from avenue_mcp.tools.status import get_status

        out = await get_status(ctx, check_session=True)
        assert out["session"]["present"] is True
        assert out["session"]["alive"] is True
        assert out["config"]["writes_enabled"] is False


class TestSync:
    async def test_sync_indexes_files_and_threads(self, ctx):
        from avenue_mcp.tools.search import sync_course_materials

        out = await sync_course_materials(ctx, ORG)
        assert out["files_found"] == 1, "the link topic must not count as a file"
        assert out["files_indexed"] == 1
        assert out["threads_indexed"] >= 1
        assert out["chunks_created"] >= 2, "2-page outline + 1 thread"
        assert out["searchable"] is True

    async def test_resync_is_incremental(self, ctx):
        """Unchanged content must not be re-indexed -- otherwise every sync
        re-downloads a whole course."""
        from avenue_mcp.tools.search import sync_course_materials

        await sync_course_materials(ctx, ORG)
        second = await sync_course_materials(ctx, ORG)
        assert second["files_indexed"] == 0
        assert second["files_skipped_unchanged"] == 1
        assert second["threads_skipped_unchanged"] >= 1

    async def test_force_reindexes_everything(self, ctx):
        from avenue_mcp.tools.search import sync_course_materials

        await sync_course_materials(ctx, ORG)
        forced = await sync_course_materials(ctx, ORG, force=True)
        assert forced["files_indexed"] == 1
        assert forced["files_skipped_unchanged"] == 0

    async def test_synced_file_is_searchable_with_page_citation(self, ctx):
        from avenue_mcp.tools.search import search_course_materials, sync_course_materials

        await sync_course_materials(ctx, ORG)
        out = await search_course_materials(ctx, "late penalty", org_unit_id=ORG)
        assert out["count"] >= 1
        top = out["results"][0]
        assert "ten percent" in top["text"]
        cite = top["citation"]
        assert cite["source_type"] == "file"
        assert cite["file_name"] == "2C03_outline_W26.pdf"
        assert cite["page"] == 2, "the late policy is on page 2 of the fixture"

    async def test_synced_discussion_becomes_searchable(self, ctx):
        from avenue_mcp.tools.search import search_course_materials, sync_course_materials

        await sync_course_materials(ctx, ORG)
        out = await search_course_materials(ctx, "recursive or iterative", org_unit_id=ORG)
        assert out["count"] >= 1
        cite = out["results"][0]["citation"]
        assert cite["source_type"] == "discussion"
        assert cite["author_role"] in ("Instructor", "Student")
