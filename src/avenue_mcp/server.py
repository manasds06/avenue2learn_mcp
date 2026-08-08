"""FastMCP server: the entire public surface.

Tool descriptions are the most important text in this project. The client model
picks a tool almost entirely from its description, so each one says WHEN to use
it -- and when NOT to, because the second most common failure is reaching for the
wrong tool among near-neighbours.

Every v1 tool is read-only and says so, which lets the model call freely.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from avenue_mcp.config import get_settings
from avenue_mcp.context import AppContext, get_context
from avenue_mcp.errors import AvenueMCPError
from avenue_mcp.institutions import Institution
from avenue_mcp.util.logging import configure_logging
from avenue_mcp.tools import (
    announcements as t_news,
    assignments as t_assign,
    classlist as t_class,
    content as t_content,
    courses as t_courses,
    discussions as t_disc,
    grades as t_grades,
    quizzes as t_quiz,
    search as t_search,
    status as t_status,
    whatsnew as t_new,
    writes as t_writes,
)

log = logging.getLogger(__name__)

from avenue_mcp import __version__

def _instructions(prof: Institution) -> str:
    """Compose the server instructions for one institution.

    A pure function so it can be unit-tested without reloading this module --
    reimporting server.py re-registers all 17 tools onto a fresh module global.
    """
    text = (
        f"Tools for {prof.display}, a D2L Brightspace instance. All tools are "
        "read-only unless write mode is explicitly enabled.\n\n"
        "Start with list_courses to get an org_unit_id, or get_whats_new for a "
        "catch-up. When a tool fails, call get_status to find out why before "
        "telling the user to log in.\n\n"
        "Report deadline times using the `local` rendering, never the UTC one: "
        f"deadlines are set in {prof.timezone} and the UTC date is often a day "
        "later."
    )
    if not prof.verified:
        text += (
            "\n\nAPI permissions on this Brightspace instance have not been "
            "verified yet. When a tool reports a route unavailable, relay that "
            "as something observed on this call -- not as a known limitation of "
            "the institution, and do not generalize it to other courses."
        )
    return text


_PROFILE = get_settings().institution_profile

server = MCPServer(
    # The registered name stays "avenue" regardless of institution: renaming it
    # would reset the user's tool-permission grants in their MCP client.
    "avenue",
    title=_PROFILE.display,
    version=__version__,
    instructions=_instructions(_PROFILE),
)


def _ctx() -> AppContext:
    return get_context()


def _guard(fn):  # noqa: ANN001, ANN202
    """Convert typed errors into structured results with a next step.

    An exception that reaches the client as a bare string loses the actionable
    hint, and the model then has nothing useful to tell the user.
    """
    import functools

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except AvenueMCPError as exc:
            log.info("%s: %s", type(exc).__name__, exc)
            return exc.as_dict()
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected failure in %s", fn.__name__)
            return {
                "error": type(exc).__name__,
                "message": str(exc),
                "next_step": "This looks like a bug. Check the server log for a traceback.",
            }

    return wrapper


# --- courses ---------------------------------------------------------------


@server.tool()
@_guard
async def list_courses(include_inactive: bool = False) -> dict[str, Any]:
    """List your current Brightspace courses with IDs, names, codes, and term dates. Read-only.

    USE THIS FIRST in almost any Brightspace task -- every other course-scoped tool
    needs an org_unit_id, and this is where you get one. Also answers "what am I
    taking this term?"

    By default returns only currently-active course offerings; pass
    include_inactive=true to also see past terms.
    """
    return await t_courses.list_courses(_ctx(), include_inactive=include_inactive)


# --- content ---------------------------------------------------------------


@server.tool()
@_guard
async def get_course_content(org_unit_id: int, max_depth: int = 10) -> dict[str, Any]:
    """Return the Content section tree for a course -- modules (folders), topics (files, links, pages), and file types. Read-only.

    Use this to see WHAT MATERIALS EXIST in a course, or to locate a specific
    file before reading it.

    This returns STRUCTURE ONLY, not file contents. To read a file's text call
    read_content_file; to search across files by meaning call
    search_course_materials (much cheaper than opening files one by one).
    """
    return await t_content.get_course_content(_ctx(), org_unit_id, max_depth=max_depth)


@server.tool()
@_guard
async def read_content_file(
    org_unit_id: int,
    topic_id: int,
    max_chars: int = 12000,
    start_page: int = 1,
) -> dict[str, Any]:
    """Download one file from a course's Content section and return its extracted text (PDF, DOCX, PPTX, HTML, TXT). Read-only.

    Use this when you know WHICH SPECIFIC FILE you need -- a course outline, one
    lecture deck, an assignment spec. Get topic_id from get_course_content.

    If you do not already know which file holds the answer, use
    search_course_materials instead: it searches every indexed file at once.

    Long documents are truncated; when they are, `next_start_page` tells you how
    to continue. If you find yourself paginating through a whole document
    hunting for something, switch to search_course_materials.

    For DIAGRAMS, FIGURES, or layout-dependent tables, use get_page_image --
    text extraction loses the picture.
    """
    return await t_content.read_content_file(
        _ctx(), org_unit_id, topic_id, max_chars=max_chars, start_page=start_page
    )


@server.tool()
@_guard
async def get_page_image(
    org_unit_id: int, topic_id: int, page: int, dpi: int | None = None
) -> Any:
    """Render one page of a PDF, or one slide of a PowerPoint, as an image so it can actually be looked at. Read-only.

    Use this when the answer is VISUAL -- a diagram, a graph, a circuit, a worked
    derivation, a table whose layout matters. Text extraction gets the words on a
    slide but loses the figure entirely, so "explain the diagram on slide 14"
    needs this rather than read_content_file.

    Prefer read_content_file when the content is prose: images cost far more
    context than text. One page per call. Page numbers are 1-indexed and often
    come straight from a search citation.

    Requires a vision-capable model in the client.
    """
    png, meta = await t_content.get_page_image(
        _ctx(), org_unit_id, topic_id, page, dpi=dpi
    )
    return [Image(data=png, format="png"), meta]


# --- assignments / quizzes / deadlines -------------------------------------


@server.tool()
@_guard
async def list_assignments(
    org_unit_id: int, include_submitted: bool = True
) -> dict[str, Any]:
    """List a course's assignments with due dates, point values, instructions, and your submission status. Read-only.

    Use this for "what are the assignments in this course?" or "have I submitted
    X?"

    For a deadline view ACROSS ALL COURSES, use get_upcoming_deadlines instead.

    Note: on some Brightspace instances the assignment folder list is
    instructor-only. If so, the response sets degraded=true and falls back to
    calendar-derived due dates without point values or submission status -- read
    the `note` field and tell the user what is missing rather than inventing it.
    """
    return await t_assign.list_assignments(
        _ctx(), org_unit_id, include_submitted=include_submitted
    )


@server.tool()
@_guard
async def list_quizzes(org_unit_id: int, include_completed: bool = True) -> dict[str, Any]:
    """List a course's quizzes and tests with availability windows, due dates, attempt limits, and whether you have taken them. Read-only.

    Use this for "do I have any quizzes coming up?" or "did I already take the
    Week 5 test?"

    Returns QUIZ METADATA ONLY -- never questions or answers.

    Note the three separate dates: start_date (opens), due_date (due), end_date
    (hard close). A quiz can be past due but still open -- that is
    is_past_due_but_open. Do not tell a user a quiz is closed when it is merely
    late.

    Quiz deadlines also appear in get_upcoming_deadlines; use this tool when you
    need attempt status or the availability window.
    """
    return await t_quiz.list_quizzes(
        _ctx(), org_unit_id, include_completed=include_completed
    )


@server.tool()
@_guard
async def get_upcoming_deadlines(
    days_ahead: int = 14, include_submitted: bool = False
) -> dict[str, Any]:
    """Return assignments, quizzes, and other dated items due within a time window, ACROSS ALL your active courses, soonest first. Read-only.

    This is the tool for "what's due this week?", "what's coming up?", or "am I
    forgetting anything?" -- the most common Brightspace question there is.

    Use list_assignments instead when you want the complete assignment list for
    ONE course, including items already past or submitted.

    Every timestamp carries both UTC and a local rendering. Report the LOCAL
    value to the user: deadlines are set in the institution's local timezone, and
    a UTC date can be a day later than the real deadline.
    """
    return await t_assign.get_upcoming_deadlines(
        _ctx(), days_ahead=days_ahead, include_submitted=include_submitted
    )


# --- grades ----------------------------------------------------------------


@server.tool()
@_guard
async def get_grades(org_unit_id: int) -> dict[str, Any]:
    """Return your grades for one course -- each item's score, points possible, weight, and any instructor feedback. Read-only.

    Use this for "what did I get on X?" or "show me my grades in this course."
    For a computed standing and what-if projections, use analyze_grade_summary.

    A blank item means not-yet-marked, NOT zero. Do not treat is_graded=false as
    a zero when summarizing.
    """
    return await t_grades.get_grades(_ctx(), org_unit_id)


@server.tool()
@_guard
async def analyze_grade_summary(
    org_unit_id: int, target_percentage: float | None = None
) -> dict[str, Any]:
    """Compute your current standing in a course: weighted average of graded work, weight completed, weight remaining, and what you would need on remaining items to reach a target. Read-only calculation -- changes nothing on Brightspace.

    Use this for "how am I doing?", "what do I need on the final to get 90?", or
    "which course needs attention?"

    IMPORTANT: always read and relay the `caveats` list. Brightspace exposes
    gradebook numbers but not gradebook rules, so dropped-lowest rules, bonus
    items, and nested category weights can make a naive average wrong. When
    weights are unavailable this tool sets weights_available=false and omits the
    projection rather than guessing -- do not compute one yourself from the
    numbers, and do not claim a letter grade unless letter_estimate is present.
    """
    return await t_grades.analyze_grade_summary(
        _ctx(), org_unit_id, target_percentage=target_percentage
    )


# --- announcements / discussions -------------------------------------------


@server.tool()
@_guard
async def list_announcements(
    org_unit_id: int, limit: int = 20, since: str | None = None
) -> dict[str, Any]:
    """Return recent course announcements with titles, body text, and posting dates, newest first. Read-only.

    Use this for "what did my prof post?", "did I miss any announcements?", or
    catching up on one course.

    For a cross-course catch-up that also covers new files, grades, and forum
    replies, use get_whats_new instead.
    """
    return await t_news.list_announcements(_ctx(), org_unit_id, limit=limit, since=since)


@server.tool()
@_guard
async def list_discussions(org_unit_id: int) -> dict[str, Any]:
    """List a course's discussion forums and their topics, with post counts and last-activity timestamps. Read-only.

    Use this to find WHERE a conversation is happening, then call
    read_discussion_thread to read it.

    To search discussion content by meaning across a whole course, use
    search_course_materials -- indexed forum posts are searchable there.
    """
    return await t_disc.list_discussions(_ctx(), org_unit_id)


@server.tool()
@_guard
async def read_discussion_thread(
    org_unit_id: int, forum_id: int, topic_id: int, max_posts: int = 50
) -> dict[str, Any]:
    """Return the posts in one discussion thread, preserving reply structure, with each post's author ROLE and timestamp. Read-only.

    Use this when a thread likely holds the answer -- instructor clarifications
    in forum replies are often the ONLY place a detail exists, appearing in no
    outline, slide, or announcement.

    Author names are intentionally omitted; posts carry author_role only
    (Instructor / TA / Student). Weight instructor and TA replies above student
    posts, and check posted_at -- a clarification from a previous term can
    contradict the current outline.
    """
    return await t_disc.read_discussion_thread(
        _ctx(), org_unit_id, forum_id, topic_id, max_posts=max_posts
    )


# --- digest ----------------------------------------------------------------


@server.tool()
@_guard
async def get_whats_new(
    since: str | None = None,
    mark_seen: bool = True,
    org_unit_id: int | None = None,
) -> dict[str, Any]:
    """Return everything that changed across your courses since you last checked: new announcements, newly posted files, new grades, new discussion replies, and newly opened assignments or quizzes. Read-only.

    Use this for "what did I miss?", "anything new?", or catching up after time
    away. This is usually the right FIRST tool in a check-in conversation -- it
    surveys everything at once instead of polling each course.

    Tracks a per-course watermark automatically. Pass since=... to override it,
    or mark_seen=false to peek WITHOUT advancing it -- use mark_seen=false if you
    might need to report the same changes again later in the conversation.

    Files reported with indexed=false are not yet searchable; offer to run
    sync_course_materials for them.
    """
    return await t_new.get_whats_new(
        _ctx(), since=since, mark_seen=mark_seen, org_unit_id=org_unit_id
    )


# --- people ----------------------------------------------------------------


@server.tool()
@_guard
async def get_class_list(org_unit_id: int) -> dict[str, Any]:
    """Return the people associated with a course -- instructors and TAs, plus contact info where Brightspace exposes it. Read-only.

    Use this for "who teaches this?" or "who do I email about the assignment?"

    This tool returns course staff by design and never returns the student
    roster in bulk -- student_roster_returned is always false, and student_count
    reports class size instead. That is a deliberate privacy choice, not a
    failure, and it holds even where the API would hand the roster over. Report
    the instructor information and say plainly that the roster is not included
    -- do not imply the course has no students. Read the `note` field.
    """
    return await t_class.get_class_list(_ctx(), org_unit_id)


# --- RAG -------------------------------------------------------------------


@server.tool()
@_guard
async def search_course_materials(
    query: str, org_unit_id: int | None = None, top_k: int = 8
) -> dict[str, Any]:
    """Semantic search across indexed course material -- outlines, lecture slides, assignment specs, readings, and discussion threads. Returns matching passages WITH CITATIONS. Read-only.

    This is the right tool for any question whose answer is INSIDE course
    material: "what's the late penalty in 2C03?", "what does the marking scheme
    say about code style?", "which lecture covered red-black trees?", "did the
    instructor clarify A3?"

    Prefer this over read_content_file when you do not already know which file
    holds the answer.

    Works even when the Brightspace session has expired -- it reads a local index.

    Requires the course to have been indexed via sync_course_materials. Check
    `indexed_courses` in the response: an empty result from an unindexed course
    means "not synced", NOT "not in the materials". Never assert that course
    material lacks something if the course is not in indexed_courses.

    Each citation names the source. For file hits, cite the file and page. For
    discussion hits, cite the role and date ("your instructor said this on
    March 9") rather than presenting a forum post with the authority of the
    outline.
    """
    return await t_search.search_course_materials(
        _ctx(), query, org_unit_id=org_unit_id, top_k=top_k
    )


@server.tool()
@_guard
async def sync_course_materials(org_unit_id: int, force: bool = False) -> dict[str, Any]:
    """Download and index a course's Content files and discussion threads so search_course_materials can search them. Writes ONLY to a local index -- changes nothing on Brightspace.

    Run this once per course, then again when new material is posted.
    Incremental: unchanged files and threads are skipped.

    The first run on a large course can take a few MINUTES and downloads a lot
    of files. Tell the user before starting, and sync ONE COURSE AT A TIME unless
    they explicitly ask for everything.

    Pass force=true to re-index everything, which is also how you recover from
    an embedding-model change.
    """
    return await t_search.sync_course_materials(_ctx(), org_unit_id, force=force)


# --- diagnostics -----------------------------------------------------------


@server.tool()
@_guard
async def get_status(check_session: bool = True) -> dict[str, Any]:
    """Report the server's own state: whether an Brightspace session is active and how old it is, which courses are indexed, the embedding model in use, and whether write mode is enabled. Read-only, and works with no session.

    Use this when a tool has FAILED and you need to know why -- logged out, never
    synced, or genuinely no data -- or when the user asks whether things are set
    up correctly. Read the `advice` list and relay it.

    Pass check_session=false for a purely local report with zero network calls.
    """
    return await t_status.get_status(_ctx(), check_session=check_session)


# --- gated write -----------------------------------------------------------


def _register_writes() -> None:
    """Registered ONLY when AVENUE_MCP_ENABLE_WRITES=1.

    Not registered-and-erroring -- absent from the tool list entirely, so the
    model cannot call it, be talked into calling it, or hallucinate having used
    it.
    """

    @server.tool()
    @_guard
    async def submit_assignment(  # noqa: D401
        org_unit_id: int,
        folder_id: int,
        file_path: str,
        comment: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Submit a file to an Brightspace assignment as your submission, optionally with a comment. THIS WRITES TO BRIGHTSPACE AND CANNOT BE UNDONE.

        TWO-STEP BY DESIGN. Call it first WITHOUT confirm to get a preview
        (course, assignment, due date, file name, size, hash). Show that preview
        to the user, get their explicit go-ahead, and only then call again with
        confirm=true.

        Never pass confirm=true on the user's behalf, on a first call, or because
        it seems implied. If the preview carries a `warning` about being past
        due, surface it before asking.

        Brightspace retains submission history: there is no un-submit.
        """
        return await t_writes.submit_assignment(
            _ctx(),
            org_unit_id,
            folder_id,
            file_path,
            comment=comment,
            confirm=confirm,
        )

    log.warning("WRITE MODE ENABLED: submit_assignment is registered")


def build() -> MCPServer:
    ctx = get_context()
    if ctx.settings.enable_writes:
        _register_writes()
    return server


def run() -> None:
    ctx = get_context()
    # Shared with the CLI rather than a second basicConfig here. This one used to
    # be its own copy, without the third-party cap -- safe only because importing
    # this module installs a root handler first, which made the basicConfig a
    # no-op. See util/logging.py.
    configure_logging(ctx.settings.log_level)
    build()
    # stdio: startup does no network I/O and needs no session.
    server.run(transport="stdio")
