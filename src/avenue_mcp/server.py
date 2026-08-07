"""FastMCP server and tool registration (docs/03, docs/05).

Tool descriptions are the most important text in this project. The client
model picks a tool based almost entirely on its description, so each one says
when to use it, when *not* to (the second most common failure is reaching for
the wrong tool among near-neighbours), and that it's read-only.

Startup does no network I/O and requires no session. An MCP client launches
this at startup and expects it up immediately; blocking on a network call — or
worse, a login prompt — makes the client look broken. Auth is lazy, on first
use, with a clear error.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from avenue_mcp import __version__
from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.config import get_settings
from avenue_mcp.errors import AvenueMCPError
from avenue_mcp.tools import announcements, assignments, classlist, content, courses, grades

log = logging.getLogger(__name__)

mcp = MCPServer(
    "avenue",
    title="Avenue to Learn",
    version=__version__,
    instructions=(
        "Read-only access to the user's own Avenue to Learn (McMaster D2L "
        "Brightspace) account. Start with `list_courses` — every course-scoped "
        "tool needs an org_unit_id from it. Dates are returned as both a UTC "
        "instant and a local Eastern rendering; quote the local one to the user. "
        "This server cannot submit or modify anything on Avenue."
    ),
)

# Every v1 tool is read-only, and says so twice: in prose for the model, and
# as a machine-readable annotation for clients that surface or gate on it.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

_client: D2LClient | None = None


def client() -> D2LClient:
    """Lazily built. Constructing it touches no network and needs no session."""
    global _client
    if _client is None:
        _client = D2LClient()
    return _client


def _fail(exc: AvenueMCPError) -> dict[str, Any]:
    """Render a typed error as a tool result the model can act on.

    Returned rather than raised so the model reliably sees the next action
    instead of a transport-level failure.
    """
    return {
        "error": type(exc).__name__,
        "message": exc.to_text(),
        "ok": False,
    }


@mcp.tool(annotations=READ_ONLY)
async def list_courses(include_inactive: bool = False) -> dict[str, Any]:
    """List your current Avenue to Learn courses with IDs, names, codes, and term dates. Read-only.

    USE THIS FIRST in almost any Avenue task — every other course-scoped tool
    needs an `org_unit_id`, and this is where you get one. Also answers "what
    am I taking this term?"

    By default returns only currently-active course offerings; set
    `include_inactive` to also see past terms.
    """
    try:
        return await courses.list_courses(client(), include_inactive=include_inactive)
    except AvenueMCPError as exc:
        return _fail(exc)


@mcp.tool(annotations=READ_ONLY)
async def get_course_content(org_unit_id: int, max_depth: int = 10) -> dict[str, Any]:
    """Return a course's Content tree — modules (folders) and topics (files, links, pages). Read-only.

    Use this to see WHAT MATERIALS EXIST in a course, or to locate a specific
    file before reading it.

    This returns STRUCTURE ONLY, not file contents. To read a file's text call
    `read_content_file` with a topic_id from this tree. Topics with
    `is_downloadable: false` are links or pages and have no file to read.

    Get `org_unit_id` from `list_courses`.
    """
    try:
        return await content.get_course_content(
            client(), org_unit_id=org_unit_id, max_depth=max_depth
        )
    except AvenueMCPError as exc:
        return _fail(exc)


@mcp.tool(annotations=READ_ONLY)
async def read_content_file(
    org_unit_id: int, topic_id: int, max_chars: int = 50_000
) -> dict[str, Any]:
    """Download one file from a course's Content section and return its extracted text. Read-only.

    Handles PDF, DOCX, PPTX, HTML, and plain text. Extracted text carries page
    or slide markers so you can cite precisely.

    Use this when you know WHICH SPECIFIC FILE you need — a course outline, one
    lecture deck, an assignment spec. Get `topic_id` from `get_course_content`.

    Check `truncated` before answering: a partial document will otherwise be
    treated as complete. If `extraction_quality` is "poor" the file is scanned
    images and its text could not be read — say so rather than answering from
    the little that came through.
    """
    try:
        return await content.read_content_file(
            client(), org_unit_id=org_unit_id, topic_id=topic_id, max_chars=max_chars
        )
    except AvenueMCPError as exc:
        return _fail(exc)


@mcp.tool(annotations=READ_ONLY)
async def list_assignments(
    org_unit_id: int, include_submitted: bool = True
) -> dict[str, Any]:
    """List a course's assignments with due dates, points, instructions, and your submission status. Read-only.

    Use this for "what are the assignments in this course?" or "have I
    submitted X?".

    For a deadline view ACROSS ALL COURSES, use `get_upcoming_deadlines`
    instead — it is one request rather than one per course.

    If `assignments_available` is false, assignment folders are not readable
    from this account; the `note` explains what to do instead. Do not report
    that as "no assignments".
    """
    try:
        return await assignments.list_assignments(
            client(), org_unit_id=org_unit_id, include_submitted=include_submitted
        )
    except AvenueMCPError as exc:
        return _fail(exc)


@mcp.tool(annotations=READ_ONLY)
async def get_upcoming_deadlines(
    days_ahead: int = 14, include_submitted: bool = False
) -> dict[str, Any]:
    """Return dated items due within a window ACROSS ALL your active courses, soonest first. Read-only.

    This is the tool for "what's due this week?", "what's coming up?", or "am I
    forgetting anything?" — the most common Avenue question there is. It needs
    no `org_unit_id`; it covers every active course itself.

    Reflects the course calendar, so items an instructor never put on the
    calendar won't appear. Treat it as a strong answer, not a guarantee of
    completeness.

    Every date comes with both a UTC instant and a local Eastern rendering —
    quote the local one to the user.

    Use `list_assignments` instead for the complete assignment list of ONE
    course, including past and submitted work.
    """
    try:
        return await assignments.get_upcoming_deadlines(
            client(), days_ahead=days_ahead, include_submitted=include_submitted
        )
    except AvenueMCPError as exc:
        return _fail(exc)


@mcp.tool(annotations=READ_ONLY)
async def get_grades(org_unit_id: int) -> dict[str, Any]:
    """Return your grades for one course — each item's score, points possible, weight, and feedback. Read-only.

    Use this for "what did I get on X?" or "show me my grades in this course."

    For a computed standing or a "what do I need on the final" projection, use
    `analyze_grade_summary` instead — do not compute those yourself from this
    output, because weights may be missing and the arithmetic will be wrong.
    """
    try:
        return await grades.get_grades(client(), org_unit_id=org_unit_id)
    except AvenueMCPError as exc:
        return _fail(exc)


@mcp.tool(annotations=READ_ONLY)
async def analyze_grade_summary(
    org_unit_id: int, target_percentage: float | None = None
) -> dict[str, Any]:
    """Compute your current standing in a course, and what you'd need on remaining work to hit a target. Read-only, calculation only — it changes nothing on Avenue.

    Use this for "how am I doing?", "what do I need on the final to get an
    A-?", or "which course needs attention?"

    IMPORTANT: read `caveats` and `weights_available` before answering. When
    weights aren't available this tool deliberately OMITS the projection rather
    than guessing. If there is no `target` block, tell the user it can't be
    computed and why — do not estimate one yourself. A confidently wrong "you
    need 74% on the final" is much worse than an honest refusal.

    Check `average_basis` too: an unweighted average will not match the
    official course grade, and the user should be told that.
    """
    try:
        return await grades.analyze_grade_summary(
            client(), org_unit_id=org_unit_id, target_percentage=target_percentage
        )
    except AvenueMCPError as exc:
        return _fail(exc)


@mcp.tool(annotations=READ_ONLY)
async def list_announcements(
    org_unit_id: int, limit: int = 20, since: str | None = None
) -> dict[str, Any]:
    """Return recent course announcements with titles, body text, and posting dates, newest first. Read-only.

    Use this for "what did my prof post?", "did I miss any announcements?", or
    catching up after time away.

    Covers ONE course. To sweep everything, call `list_courses` first and then
    this per course. `since` takes an ISO date and returns only newer items.
    """
    try:
        return await announcements.list_announcements(
            client(), org_unit_id=org_unit_id, limit=limit, since=since
        )
    except AvenueMCPError as exc:
        return _fail(exc)


@mcp.tool(annotations=READ_ONLY)
async def get_class_list(org_unit_id: int) -> dict[str, Any]:
    """Return the instructors and TAs for a course, with contact info where Avenue exposes it. Read-only.

    Use this for "who teaches this?" or "who do I email about the assignment?"

    This does NOT return a student roster. McMaster restricts full rosters, and
    where records are available this tool still withholds them — a class list
    with names and emails is personal information under FIPPA. `student_count`
    is provided; the list is not. Report that as intended behaviour, not a
    failure.
    """
    try:
        return await classlist.get_class_list(client(), org_unit_id=org_unit_id)
    except AvenueMCPError as exc:
        return _fail(exc)


def build_server() -> FastMCP:
    """Configure logging and return the server.

    Write tools are not registered at all when AVENUE_MCP_ENABLE_WRITES is off
    — absent from the tool list, not registered-and-erroring. The model can't
    call a tool it can't see, can't be talked into it, and can't hallucinate
    having used it.
    """
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings.ensure_dirs()

    if settings.enable_writes:
        log.warning(
            "AVENUE_MCP_ENABLE_WRITES is set, but write tools are not implemented "
            "in v1. No write tool has been registered."
        )

    return mcp


def main() -> None:
    build_server().run()
