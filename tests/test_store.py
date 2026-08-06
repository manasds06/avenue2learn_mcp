"""Store tests: schema, chunk lifecycle, FTS, and watermarks.

Watermarks get real coverage because off-by-one bugs there silently lose
changes -- the user is told "nothing new" when something was.
"""

from __future__ import annotations

import numpy as np
import pytest

from avenue_mcp.rag.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "index.db")


def file_doc(**over):
    base = {
        "source_type": "file",
        "org_unit_id": 111,
        "course_name": "COMPSCI 2C03",
        "topic_id": 4455,
        "module_path": "Week 1",
        "file_name": "outline.pdf",
        "mime_type": "application/pdf",
        "page_count": 8,
        "content_hash": "abc",
        "title": "Course Outline",
        "last_modified": "2026-01-04T18:22:11Z",
        "indexed_at": "2026-03-01T00:00:00Z",
        "extraction_quality": "ok",
    }
    base.update(over)
    return base


def thread_doc(**over):
    base = {
        "source_type": "discussion",
        "org_unit_id": 111,
        "course_name": "COMPSCI 2C03",
        "forum_id": 90,
        "thread_id": 412,
        "forum_name": "Assignment Q&A",
        "thread_name": "A3 clarifications",
        "max_post_id": 5002,
        "title": "A3 clarifications",
        "last_modified": "2026-03-09T16:45:00Z",
        "indexed_at": "2026-03-10T00:00:00Z",
        "extraction_quality": "ok",
    }
    base.update(over)
    return base


def chunks(n=2, dim=4):
    return [
        {
            "text": f"chunk body {i} late penalty",
            "position": f"p.{i}",
            "token_count": 10,
            "embedding": np.ones(dim, dtype=np.float32) * (i + 1),
        }
        for i in range(n)
    ]


class TestSchema:
    def test_created_and_empty(self, store):
        assert store.is_empty()
        assert store.stats()["documents"] == 0

    def test_schema_version_recorded(self, store):
        assert store.get_meta("schema_version") == "1"

    def test_reopen_is_idempotent(self, tmp_path):
        p = tmp_path / "i.db"
        Store(p)
        again = Store(p)
        assert again.get_meta("schema_version") == "1"


class TestDocuments:
    def test_insert_file_doc(self, store):
        doc_id = store.upsert_document(file_doc())
        assert doc_id > 0
        assert store.get_file_doc(111, 4455) is not None

    def test_upsert_is_stable_on_id(self, store):
        first = store.upsert_document(file_doc())
        second = store.upsert_document(file_doc(content_hash="def"))
        assert first == second
        assert store.get_file_doc(111, 4455)["content_hash"] == "def"

    def test_discussion_doc_has_no_topic_id(self, store):
        """A thread has no content topic -- which is why doc_id is synthetic."""
        store.upsert_document(thread_doc())
        row = store.get_thread_doc(111, 90, 412)
        assert row is not None
        assert row["topic_id"] is None
        assert row["thread_name"] == "A3 clarifications"

    def test_file_and_thread_coexist(self, store):
        a = store.upsert_document(file_doc())
        b = store.upsert_document(thread_doc())
        assert a != b
        assert store.stats()["documents"] == 2


class TestChunks:
    def test_add_and_count(self, store):
        doc_id = store.upsert_document(file_doc())
        assert store.add_chunks(doc_id, 111, chunks(3)) == 3
        assert store.stats()["chunks"] == 3
        assert not store.is_empty()

    def test_reindex_replaces_not_duplicates(self, store):
        doc_id = store.upsert_document(file_doc())
        store.add_chunks(doc_id, 111, chunks(3))
        again = store.upsert_document(file_doc(content_hash="new"))
        store.add_chunks(again, 111, chunks(2))
        assert store.stats()["chunks"] == 2

    def test_delete_course_cascades(self, store):
        doc_id = store.upsert_document(file_doc())
        store.add_chunks(doc_id, 111, chunks(2))
        store.delete_course(111)
        assert store.stats()["chunks"] == 0
        assert store.stats()["documents"] == 0

    def test_clear_all(self, store):
        doc_id = store.upsert_document(file_doc())
        store.add_chunks(doc_id, 111, chunks(2))
        store.clear_all()
        assert store.is_empty()

    def test_hydrate_joins_document_metadata(self, store):
        doc_id = store.upsert_document(file_doc())
        store.add_chunks(doc_id, 111, chunks(1))
        ids = store.load_vectors()[1]
        rows = store.hydrate(ids)
        row = next(iter(rows.values()))
        assert row["course_name"] == "COMPSCI 2C03"
        assert row["file_name"] == "outline.pdf"
        assert row["source_type"] == "file"


class TestVectors:
    def test_roundtrip(self, store):
        doc_id = store.upsert_document(file_doc())
        store.add_chunks(doc_id, 111, chunks(3, dim=4))
        mat, ids = store.load_vectors()
        assert mat.shape == (3, 4)
        assert len(ids) == 3

    def test_course_scope_is_a_hard_filter(self, store):
        """Returning a MATH policy for a COMPSCI question is the worst available
        outcome, so scoping excludes rather than down-weights."""
        a = store.upsert_document(file_doc(org_unit_id=111, topic_id=1))
        b = store.upsert_document(file_doc(org_unit_id=222, topic_id=2))
        store.add_chunks(a, 111, chunks(2))
        store.add_chunks(b, 222, chunks(3))

        _, all_ids = store.load_vectors()
        _, only_111 = store.load_vectors(111)
        _, only_222 = store.load_vectors(222)
        assert len(all_ids) == 5
        assert len(only_111) == 2
        assert len(only_222) == 3
        assert not set(only_111) & set(only_222)

    def test_unknown_course_returns_empty(self, store):
        doc_id = store.upsert_document(file_doc())
        store.add_chunks(doc_id, 111, chunks(2))
        mat, ids = store.load_vectors(999)
        assert ids == []
        assert mat.shape[0] == 0


class TestFTS:
    def test_finds_term(self, store):
        doc_id = store.upsert_document(file_doc())
        store.add_chunks(doc_id, 111, chunks(2))
        assert store.fts_search("penalty", 10)

    def test_natural_language_query_does_not_crash(self, store):
        """Real queries contain characters FTS5 treats as operators."""
        doc_id = store.upsert_document(file_doc())
        store.add_chunks(doc_id, 111, chunks(1))
        for q in ["what's due?", "A3 (part 2)", 'quote " inside', "NOT AND OR", "*"]:
            store.fts_search(q, 5)  # must not raise

    def test_scoped(self, store):
        a = store.upsert_document(file_doc(org_unit_id=111, topic_id=1))
        b = store.upsert_document(file_doc(org_unit_id=222, topic_id=2))
        store.add_chunks(a, 111, chunks(1))
        store.add_chunks(b, 222, chunks(1))
        assert len(store.fts_search("penalty", 10, 111)) == 1

    def test_empty_query(self, store):
        assert store.fts_search("", 10) == []
        assert store.fts_search("!!!", 10) == []


class TestWatermarks:
    def test_absent_initially(self, store):
        assert store.get_watermark(111, "announcements") is None

    def test_set_and_get(self, store):
        store.set_watermark(111, "announcements", "2026-03-01T00:00:00Z")
        assert store.get_watermark(111, "announcements") == "2026-03-01T00:00:00Z"

    def test_per_category_isolation(self, store):
        """'I've read the announcements but not the new grades' must be
        representable -- one global timestamp cannot express it."""
        store.set_watermark(111, "announcements", "2026-03-01T00:00:00Z")
        assert store.get_watermark(111, "grades") is None

    def test_per_course_isolation(self, store):
        """Syncing one course must not silently mark another as seen."""
        store.set_watermark(111, "files", "2026-03-01T00:00:00Z")
        assert store.get_watermark(222, "files") is None

    def test_update_overwrites(self, store):
        store.set_watermark(111, "files", "2026-03-01T00:00:00Z")
        store.set_watermark(111, "files", "2026-03-05T00:00:00Z")
        assert store.get_watermark(111, "files") == "2026-03-05T00:00:00Z"

    def test_survives_index_clear(self, store):
        """Clearing the search index must not reset 'what have I seen'."""
        store.set_watermark(111, "files", "2026-03-01T00:00:00Z")
        store.clear_all()
        assert store.get_watermark(111, "files") == "2026-03-01T00:00:00Z"


class TestEmbedModelGuard:
    def test_records_and_reads(self, store):
        store.record_embed_model("BAAI/bge-small-en-v1.5", 384)
        assert store.embed_model() == "BAAI/bge-small-en-v1.5"
        assert store.get_meta("embed_dim") == "384"

    def test_none_before_first_sync(self, store):
        assert store.embed_model() is None


class TestCourseTracking:
    def test_remember_and_read(self, store):
        store.remember_course(111, "COMPSCI 2C03", "CS-2C03")
        assert store.course_name(111) == "COMPSCI 2C03"

    def test_indexed_courses_reports_counts(self, store):
        doc_id = store.upsert_document(file_doc())
        store.add_chunks(doc_id, 111, chunks(2))
        store.upsert_document(thread_doc())
        rows = store.indexed_courses()
        assert len(rows) == 1
        assert rows[0]["files"] == 1
        assert rows[0]["threads"] == 1
        assert rows[0]["chunks"] == 2

    def test_has_course(self, store):
        assert not store.has_course(111)
        store.upsert_document(file_doc())
        assert store.has_course(111)
