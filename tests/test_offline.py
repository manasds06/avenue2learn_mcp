"""Offline behavior: the tools that must work with no session.

A student whose session expires mid-study should keep full semantic search over
everything indexed. This is a design property, not a fallback -- and adding
require_session() to the search path for consistency is exactly how it gets
destroyed, so these tests pin it.
"""

from __future__ import annotations

import numpy as np
import pytest

from avenue_mcp.context import AppContext, set_context


@pytest.fixture()
def offline_ctx(tmp_path, monkeypatch):
    """A context with NO session file at all."""
    monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "state"))
    from avenue_mcp.config import get_settings

    get_settings.cache_clear()
    set_context(None)
    ctx = AppContext()
    assert not ctx.auth.session_present
    yield ctx
    get_settings.cache_clear()
    set_context(None)


def seed(ctx, org_unit_id=111, course="COMPSCI 2C03"):
    ctx.remember_course(org_unit_id, course, "CS")
    doc_id = ctx.store.upsert_document(
        {
            "source_type": "file",
            "org_unit_id": org_unit_id,
            "course_name": course,
            "topic_id": 4455,
            "file_name": "outline.pdf",
            "title": "Course Outline",
            "module_path": "Week 1",
            "content_hash": "h",
            "page_count": 8,
            "indexed_at": "2026-03-01T00:00:00Z",
            "extraction_quality": "ok",
        }
    )
    ctx.store.add_chunks(
        doc_id,
        org_unit_id,
        [
            {
                "text": "Late assignments are penalized 10% per day up to three days.",
                "position": "p.3",
                "token_count": 12,
                "embedding": np.array([1, 0, 0, 0], dtype=np.float32),
            }
        ],
    )
    ctx.store.record_embed_model(ctx.settings.embed_model, 4)


class TestSessionGating:
    async def test_require_session_raises_without_session(self, offline_ctx):
        from avenue_mcp.errors import NoSessionError

        with pytest.raises(NoSessionError):
            await offline_ctx.require_session()

    async def test_network_tool_fails_cleanly(self, offline_ctx):
        from avenue_mcp.errors import NoSessionError
        from avenue_mcp.tools.courses import list_courses

        with pytest.raises(NoSessionError):
            await list_courses(offline_ctx)


class TestStatusWorksOffline:
    async def test_status_no_session_no_raise(self, offline_ctx):
        from avenue_mcp.tools.status import get_status

        result = await get_status(offline_ctx, check_session=False)
        assert result["session"]["present"] is False
        assert result["session"]["alive"] is False

    async def test_status_advises_login(self, offline_ctx):
        from avenue_mcp.tools.status import get_status

        result = await get_status(offline_ctx, check_session=False)
        assert any("login" in a.lower() for a in result["advice"])

    async def test_status_reports_offline_capabilities(self, offline_ctx):
        from avenue_mcp.tools.status import get_status

        result = await get_status(offline_ctx, check_session=False)
        assert "search_course_materials" in result["offline_capable"]

    async def test_status_reports_index_even_offline(self, offline_ctx):
        from avenue_mcp.tools.status import get_status

        seed(offline_ctx)
        result = await get_status(offline_ctx, check_session=False)
        assert result["index"]["course_count"] == 1
        assert result["index"]["chunks"] == 1


class TestSearchWorksOffline:
    async def test_keyword_search_without_session(self, offline_ctx):
        """The core promise: local search survives an expired session."""
        from avenue_mcp.tools.search import search_course_materials

        seed(offline_ctx)
        result = await search_course_materials(offline_ctx, "late penalty")
        assert "error" not in result
        assert result["count"] >= 1
        assert "10% per day" in result["results"][0]["text"]

    async def test_citation_present(self, offline_ctx):
        from avenue_mcp.tools.search import search_course_materials

        seed(offline_ctx)
        cite = (await search_course_materials(offline_ctx, "late penalty"))["results"][0][
            "citation"
        ]
        assert cite["source_type"] == "file"
        assert cite["file_name"] == "outline.pdf"
        assert cite["page"] == 3
        assert cite["course_name"] == "COMPSCI 2C03"

    async def test_indexed_courses_always_returned(self, offline_ctx):
        """So the model can tell 'not in the materials' from 'never synced'."""
        from avenue_mcp.tools.search import search_course_materials

        seed(offline_ctx)
        result = await search_course_materials(offline_ctx, "nonexistent gibberish xyzzy")
        assert result["indexed_courses"] == ["COMPSCI 2C03"]

    async def test_dimension_mismatch_yields_no_phantom_hits(self, offline_ctx):
        """Regression: an index whose vector width disagrees with the loaded
        model must not surface arbitrary chunks.

        cosine_scores returns all-zeros on a width mismatch, and argsort over
        zeros still returns indices -- so an unfiltered vector leg reported
        unrelated chunks as matches for a query with no lexical overlap either.
        """
        from avenue_mcp.tools.search import search_course_materials

        seed(offline_ctx)  # seeds 4-dim vectors
        result = await search_course_materials(
            offline_ctx, "zzzz qqqq vvvv nonexistent gibberish xyzzy"
        )
        assert result["count"] == 0, (
            "gibberish must not match: no keyword overlap, and the vector leg "
            "should be skipped or zero-filtered"
        )

    async def test_zero_similarity_never_a_candidate(self, offline_ctx):
        """Zero cosine means unrelated, not weakly related."""
        import numpy as np

        from avenue_mcp.rag.embed import cosine_scores

        query = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        matrix = np.array([[0.0, 1.0, 0.0, 0.0]], dtype=np.float32)  # orthogonal
        assert float(cosine_scores(query, matrix)[0]) == 0.0

    async def test_empty_index_explains_itself(self, offline_ctx):
        from avenue_mcp.tools.search import search_course_materials

        result = await search_course_materials(offline_ctx, "anything")
        assert result["count"] == 0
        assert result["indexed_courses"] == []
        assert "sync_course_materials" in result["note"]

    async def test_unindexed_course_is_distinguished(self, offline_ctx):
        from avenue_mcp.tools.search import search_course_materials

        seed(offline_ctx)
        result = await search_course_materials(offline_ctx, "late penalty", org_unit_id=999)
        assert result["count"] == 0
        assert "not been indexed" in result["note"]

    async def test_course_scope_excludes_other_courses(self, offline_ctx):
        from avenue_mcp.tools.search import search_course_materials

        seed(offline_ctx, 111, "COMPSCI 2C03")
        seed(offline_ctx, 222, "MATH 2Z03")
        result = await search_course_materials(
            offline_ctx, "late penalty", org_unit_id=111
        )
        for r in result["results"]:
            assert r["citation"]["course_name"] == "COMPSCI 2C03"

    async def test_embed_model_mismatch_raises(self, offline_ctx, monkeypatch):
        from avenue_mcp.errors import EmbedModelMismatchError
        from avenue_mcp.tools.search import search_course_materials

        seed(offline_ctx)
        offline_ctx.store.record_embed_model("some/other-model", 4)
        with pytest.raises(EmbedModelMismatchError):
            await search_course_materials(offline_ctx, "late penalty")


class TestDiscussionCitations:
    async def test_role_and_date_in_citation(self, offline_ctx):
        """So the model can say 'your instructor said this on March 9' rather
        than presenting a forum guess with the authority of the outline."""
        from avenue_mcp.tools.search import search_course_materials

        offline_ctx.remember_course(111, "COMPSCI 2C03", "CS")
        doc_id = offline_ctx.store.upsert_document(
            {
                "source_type": "discussion",
                "org_unit_id": 111,
                "course_name": "COMPSCI 2C03",
                "forum_id": 90,
                "forum_name": "Assignment Q&A",
                "thread_id": 412,
                "thread_name": "A3 clarifications",
                "max_post_id": 5002,
                "title": "A3 clarifications",
                "indexed_at": "2026-03-10T00:00:00Z",
                "extraction_quality": "ok",
            }
        )
        offline_ctx.store.add_chunks(
            doc_id,
            111,
            [
                {
                    "text": "Q (Student): recursive version? A (Instructor): either is fine.",
                    "position": "posts 5001-5002",
                    "token_count": 12,
                    "author_role": "Instructor",
                    "posted_at": "2026-03-09T16:45:00Z",
                    "embedding": np.array([0, 1, 0, 0], dtype=np.float32),
                }
            ],
        )
        offline_ctx.store.record_embed_model(offline_ctx.settings.embed_model, 4)

        result = await search_course_materials(offline_ctx, "recursive version")
        cite = result["results"][0]["citation"]
        assert cite["source_type"] == "discussion"
        assert cite["author_role"] == "Instructor"
        assert cite["posted_on"] == "2026-03-09"
        assert cite["thread_name"] == "A3 clarifications"

    async def test_no_author_name_field(self, offline_ctx):
        from avenue_mcp.tools.search import search_course_materials

        await self.test_role_and_date_in_citation(offline_ctx)
        result = await search_course_materials(offline_ctx, "recursive version")
        cite = result["results"][0]["citation"]
        assert not any("name" in k and "thread" not in k and "forum" not in k
                       and "course" not in k and "file" not in k for k in cite)
