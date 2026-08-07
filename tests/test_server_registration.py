"""Tool registration and the write gate.

The gate matters structurally, not just as a config flag: when writes are off the
tool must be ABSENT from the listing, not registered-and-erroring. A model cannot
call a tool it cannot see, be talked into calling it, or hallucinate having used
it.
"""

from __future__ import annotations

import pytest

EXPECTED_READ_TOOLS = {
    "list_courses",
    "get_course_content",
    "read_content_file",
    "get_page_image",
    "list_assignments",
    "list_quizzes",
    "get_upcoming_deadlines",
    "get_grades",
    "analyze_grade_summary",
    "list_announcements",
    "list_discussions",
    "read_discussion_thread",
    "get_whats_new",
    "get_class_list",
    "search_course_materials",
    "sync_course_materials",
    "get_status",
}


async def tool_names(server) -> set[str]:
    return {t.name for t in await server.list_tools()}


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Point state at a temp dir so tests never touch a real session."""
    monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AVENUE_MCP_ENABLE_WRITES", raising=False)
    from avenue_mcp.config import get_settings

    get_settings.cache_clear()
    import avenue_mcp.context as context

    context.set_context(None)
    yield
    get_settings.cache_clear()
    context.set_context(None)


class TestReadTools:
    async def test_all_read_tools_registered(self, isolated):
        from avenue_mcp.server import build

        names = await tool_names(build())
        missing = EXPECTED_READ_TOOLS - names
        assert not missing, f"missing tools: {sorted(missing)}"

    async def test_seventeen_read_tools(self, isolated):
        from avenue_mcp.server import build

        names = await tool_names(build())
        assert len(EXPECTED_READ_TOOLS & names) == 17

    async def test_write_tool_absent_by_default(self, isolated):
        """Not registered-and-erroring -- absent entirely."""
        from avenue_mcp.server import build

        assert "submit_assignment" not in await tool_names(build())

    async def test_no_discussion_posting_tool_ever(self, isolated):
        from avenue_mcp.server import build

        names = await tool_names(build())
        for forbidden in ("post_discussion", "reply_to_thread", "create_post"):
            assert forbidden not in names

    async def test_no_standalone_comment_tool(self, isolated):
        """A student cannot comment without submitting, so the capability is a
        parameter on submit_assignment, not a tool."""
        from avenue_mcp.server import build

        assert "add_submission_comment" not in await tool_names(build())


class TestDescriptions:
    async def test_every_tool_has_a_substantial_description(self, isolated):
        from avenue_mcp.server import build

        for tool in await build().list_tools():
            assert tool.description, f"{tool.name} has no description"
            assert len(tool.description) > 120, f"{tool.name} description too thin"

    async def test_read_only_is_stated(self, isolated):
        from avenue_mcp.server import build

        for tool in await build().list_tools():
            if tool.name in ("sync_course_materials", "get_page_image"):
                continue
            if tool.name in EXPECTED_READ_TOOLS:
                assert "read-only" in (tool.description or "").lower(), tool.name

    async def test_search_warns_about_unindexed_courses(self, isolated):
        """The empty-result ambiguity is the single most consequential thing for
        the model to understand about this tool."""
        from avenue_mcp.server import build

        tool = next(t for t in await build().list_tools() if t.name == "search_course_materials")
        assert "indexed_courses" in tool.description

    async def test_grade_tool_warns_against_guessing(self, isolated):
        from avenue_mcp.server import build

        tool = next(
            t for t in await build().list_tools() if t.name == "analyze_grade_summary"
        )
        assert "caveats" in tool.description.lower()


class TestWriteGate:
    async def test_registered_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("AVENUE_MCP_ENABLE_WRITES", "1")
        from avenue_mcp.config import get_settings

        get_settings.cache_clear()
        import avenue_mcp.context as context

        context.set_context(None)
        try:
            from avenue_mcp.server import build

            names = await tool_names(build())
            assert "submit_assignment" in names
        finally:
            get_settings.cache_clear()
            context.set_context(None)

    async def test_write_tool_description_demands_two_steps(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("AVENUE_MCP_ENABLE_WRITES", "1")
        from avenue_mcp.config import get_settings

        get_settings.cache_clear()
        import avenue_mcp.context as context

        context.set_context(None)
        try:
            from avenue_mcp.server import build

            tool = next(
                t for t in await build().list_tools() if t.name == "submit_assignment"
            )
            desc = tool.description.lower()
            assert "cannot be undone" in desc
            assert "confirm" in desc
        finally:
            get_settings.cache_clear()
            context.set_context(None)


class TestWritesRefused:
    async def test_submit_refuses_when_disabled(self, isolated):
        from avenue_mcp.context import AppContext
        from avenue_mcp.errors import WritesDisabledError
        from avenue_mcp.tools.writes import submit_assignment

        ctx = AppContext()
        try:
            with pytest.raises(WritesDisabledError):
                await submit_assignment(ctx, 1, 2, "/tmp/x", confirm=True)
        finally:
            await ctx.aclose()
