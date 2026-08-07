"""Chunking tests -- pure functions, no network, highest-value coverage."""

from __future__ import annotations

from avenue_mcp.rag.chunk import (
    MAX_TOKENS,
    chunk_segments,
    chunk_thread,
    discussion_context_header,
    est_tokens,
    file_context_header,
)
from avenue_mcp.rag.extract import Segment


def seg(text: str, position: str = "p.1", page: int | None = 1) -> Segment:
    return Segment(text=text, position=position, page=page)


class TestContextHeaders:
    def test_file_header_carries_course(self):
        """Without the course name a chunk saying '10% per day' is identical
        whether it came from 2C03 or MATH 2Z03 -- the worst retrieval error."""
        h = file_context_header("COMPSCI 2C03", "Week 1", "Outline", "outline.pdf", "p.3")
        assert "COMPSCI 2C03" in h
        assert "Week 1" in h
        assert "p.3" in h

    def test_file_header_omits_duplicate_title(self):
        h = file_context_header("C1", None, "x.pdf", "x.pdf", "p.1")
        assert h.count("x.pdf") == 1

    def test_discussion_header_has_role_and_date(self):
        h = discussion_context_header("C1", "A3 help", "Instructor", "2026-03-09T10:00:00Z")
        assert "Instructor" in h and "2026-03-09" in h


class TestFileChunking:
    def test_empty_input(self):
        assert chunk_segments([], course_name="C1") == []

    def test_blank_segments_dropped(self):
        assert chunk_segments([seg("   "), seg("\n")], course_name="C1") == []

    def test_single_short_segment(self):
        out = chunk_segments([seg("Late penalty is 10% per day.")], course_name="C1")
        assert len(out) == 1
        assert "10% per day" in out[0].body

    def test_header_in_embedded_text_not_body(self):
        out = chunk_segments(
            [seg("Late penalty is 10% per day.")],
            course_name="COMPSCI 2C03",
            file_name="outline.pdf",
        )
        assert "COMPSCI 2C03" in out[0].text
        assert "COMPSCI 2C03" not in out[0].body

    def test_short_segments_merge(self):
        """Orphan fragments are retrieval noise; three-word slides should not
        each become their own chunk."""
        segs = [seg(f"Slide {i} title", f"slide {i}", i) for i in range(1, 9)]
        out = chunk_segments(segs, course_name="C1")
        assert len(out) < len(segs)

    def test_large_segment_splits(self):
        big = " ".join(["word"] * 3000)  # well over MAX_TOKENS
        out = chunk_segments([seg(big)], course_name="C1")
        assert len(out) > 1
        assert all(c.token_count <= MAX_TOKENS * 1.4 for c in out)

    def test_position_preserved_for_citation(self):
        out = chunk_segments(
            [seg("A" * 3000, "p.7", 7)], course_name="C1", file_name="f.pdf"
        )
        assert any("p.7" in c.position for c in out)

    def test_all_text_preserved(self):
        segs = [seg(f"Section {i} content here." * 20, f"p.{i}", i) for i in range(1, 6)]
        out = chunk_segments(segs, course_name="C1")
        joined = " ".join(c.body for c in out)
        for i in range(1, 6):
            assert f"Section {i}" in joined

    def test_token_count_recorded(self):
        out = chunk_segments([seg("hello world " * 100)], course_name="C1")
        assert all(c.token_count > 0 for c in out)


class TestThreadChunking:
    def test_empty(self):
        assert chunk_thread([], course_name="C1", thread_name="T") == []

    def test_question_and_reply_stay_together(self):
        """An instructor's 'Either is fine' is meaningless alone; paired with the
        question it is exactly the passage a student needs."""
        posts = [
            {
                "post_id": 1,
                "parent_post_id": None,
                "author_role": "Student",
                "posted_at": "2026-03-09T14:00:00Z",
                "text": "Does Q3 want the recursive version?",
            },
            {
                "post_id": 2,
                "parent_post_id": 1,
                "author_role": "Instructor",
                "posted_at": "2026-03-09T16:00:00Z",
                "text": "Either is fine, but document your choice.",
            },
        ]
        out = chunk_thread(posts, course_name="C1", thread_name="A3")
        assert len(out) == 1
        assert "Q3" in out[0].body
        assert "Either is fine" in out[0].body

    def test_instructor_role_propagates_to_chunk(self):
        posts = [
            {"post_id": 1, "parent_post_id": None, "author_role": "Student",
             "posted_at": "2026-03-09T14:00:00Z", "text": "Question?"},
            {"post_id": 2, "parent_post_id": 1, "author_role": "Instructor",
             "posted_at": "2026-03-09T16:00:00Z", "text": "Answer."},
        ]
        out = chunk_thread(posts, course_name="C1", thread_name="A3")
        assert out[0].author_role == "Instructor"

    def test_latest_timestamp_wins(self):
        posts = [
            {"post_id": 1, "parent_post_id": None, "author_role": "Student",
             "posted_at": "2026-03-01T10:00:00Z", "text": "Q"},
            {"post_id": 2, "parent_post_id": 1, "author_role": "TA",
             "posted_at": "2026-03-05T10:00:00Z", "text": "A"},
        ]
        out = chunk_thread(posts, course_name="C1", thread_name="T")
        assert out[0].posted_at == "2026-03-05T10:00:00Z"

    def test_separate_roots_separate_chunks(self):
        posts = [
            {"post_id": 1, "parent_post_id": None, "author_role": "Student",
             "posted_at": "2026-03-01T10:00:00Z", "text": "First question"},
            {"post_id": 5, "parent_post_id": None, "author_role": "Student",
             "posted_at": "2026-03-02T10:00:00Z", "text": "Unrelated question"},
        ]
        out = chunk_thread(posts, course_name="C1", thread_name="T")
        assert len(out) == 2

    def test_no_author_names_anywhere(self):
        """Roles carry the signal; names would make the index a durable record
        of classmates' opinions."""
        posts = [
            {"post_id": 1, "parent_post_id": None, "author_role": "Student",
             "posted_at": "2026-03-01T10:00:00Z", "text": "Q", "author_name": "Jane Doe"},
        ]
        out = chunk_thread(posts, course_name="C1", thread_name="T")
        assert "Jane" not in out[0].text
        assert "Doe" not in out[0].text

    def test_empty_text_posts_skipped(self):
        posts = [
            {"post_id": 1, "parent_post_id": None, "author_role": "Student",
             "posted_at": "2026-03-01T10:00:00Z", "text": ""},
        ]
        assert chunk_thread(posts, course_name="C1", thread_name="T") == []

    def test_orphan_parent_treated_as_root(self):
        posts = [
            {"post_id": 9, "parent_post_id": 999, "author_role": "Student",
             "posted_at": "2026-03-01T10:00:00Z", "text": "Orphaned reply"},
        ]
        out = chunk_thread(posts, course_name="C1", thread_name="T")
        assert len(out) == 1


class TestTokenEstimate:
    def test_monotonic(self):
        assert est_tokens("a" * 400) > est_tokens("a" * 40)

    def test_never_zero(self):
        assert est_tokens("") >= 1
