"""SQLite index: metadata, FTS5 keyword search, and vectors.

Two design notes worth the space:

* `documents` is keyed on a synthetic `doc_id`, not `topic_id`. A discussion
  thread has no content topic, so a topic_id primary key cannot represent it.
* Vectors live in a BLOB column rather than a sidecar .npy file. At a few
  thousand chunks a brute-force scan is sub-millisecond, and keeping them in
  the database removes an entire class of file-desync bug.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type        TEXT NOT NULL,          -- 'file' | 'discussion'
    org_unit_id        INTEGER NOT NULL,
    course_name        TEXT NOT NULL,
    -- files
    topic_id           INTEGER,
    module_path        TEXT,
    file_name          TEXT,
    mime_type          TEXT,
    page_count         INTEGER,
    content_hash       TEXT,
    -- discussions
    forum_id           INTEGER,
    forum_name         TEXT,
    thread_id          INTEGER,
    thread_name        TEXT,
    max_post_id        INTEGER,
    -- common
    title              TEXT,
    last_modified      TEXT,
    indexed_at         TEXT,
    extraction_quality TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_file
    ON documents(org_unit_id, topic_id) WHERE source_type = 'file';
CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_thread
    ON documents(org_unit_id, forum_id, thread_id) WHERE source_type = 'discussion';
CREATE INDEX IF NOT EXISTS idx_doc_course ON documents(org_unit_id);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    org_unit_id INTEGER NOT NULL,
    position    TEXT,
    author_role TEXT,
    posted_at   TEXT,
    text        TEXT NOT NULL,
    token_count INTEGER,
    embedding   BLOB
);

CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunk_course ON chunks(org_unit_id);

-- Standalone (not contentless) FTS5: slightly duplicative, far less
-- error-prone than external-content triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(text, chunk_id UNINDEXED, tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS watermarks (
    org_unit_id INTEGER NOT NULL,
    category    TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (org_unit_id, category)
);

CREATE TABLE IF NOT EXISTS courses (
    org_unit_id INTEGER PRIMARY KEY,
    name        TEXT,
    code        TEXT,
    seen_at     TEXT
);

CREATE TABLE IF NOT EXISTS index_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._vec_cache: tuple[np.ndarray, list[int], list[int]] | None = None
        self._init()

    # --- connection -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self.tx() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO index_meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    # --- meta -------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO index_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def embed_model(self) -> str | None:
        return self.get_meta("embed_model")

    def record_embed_model(self, name: str, dim: int) -> None:
        self.set_meta("embed_model", name)
        self.set_meta("embed_dim", str(dim))

    def is_empty(self) -> bool:
        with self.tx() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        return (row["n"] or 0) == 0

    # --- courses ----------------------------------------------------------

    def remember_course(self, org_unit_id: int, name: str | None, code: str | None) -> None:
        from avenue_mcp.util.dates import now_utc, to_utc_iso

        with self.tx() as conn:
            conn.execute(
                "INSERT INTO courses(org_unit_id, name, code, seen_at) VALUES (?,?,?,?) "
                "ON CONFLICT(org_unit_id) DO UPDATE SET "
                "name=excluded.name, code=excluded.code, seen_at=excluded.seen_at",
                (org_unit_id, name, code, to_utc_iso(now_utc())),
            )

    def course_name(self, org_unit_id: int) -> str | None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT name FROM courses WHERE org_unit_id = ?", (org_unit_id,)
            ).fetchone()
        return row["name"] if row else None

    def indexed_courses(self) -> list[dict[str, Any]]:
        with self.tx() as conn:
            rows = conn.execute(
                """
                SELECT d.org_unit_id,
                       MAX(d.course_name)          AS course_name,
                       COUNT(DISTINCT d.doc_id)    AS documents,
                       SUM(CASE WHEN d.source_type='file' THEN 1 ELSE 0 END) AS files,
                       SUM(CASE WHEN d.source_type='discussion' THEN 1 ELSE 0 END) AS threads,
                       MAX(d.indexed_at)           AS indexed_at
                FROM documents d
                GROUP BY d.org_unit_id
                ORDER BY course_name
                """
            ).fetchall()
            counts = {
                r["org_unit_id"]: r["n"]
                for r in conn.execute(
                    "SELECT org_unit_id, COUNT(*) AS n FROM chunks GROUP BY org_unit_id"
                ).fetchall()
            }
        out = []
        for r in rows:
            out.append(
                {
                    "org_unit_id": r["org_unit_id"],
                    "course_name": r["course_name"],
                    "documents": r["documents"],
                    "files": r["files"],
                    "threads": r["threads"],
                    "chunks": counts.get(r["org_unit_id"], 0),
                    "indexed_at": r["indexed_at"],
                }
            )
        return out

    def has_course(self, org_unit_id: int) -> bool:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM documents WHERE org_unit_id = ? LIMIT 1", (org_unit_id,)
            ).fetchone()
        return row is not None

    # --- documents --------------------------------------------------------

    def get_file_doc(self, org_unit_id: int, topic_id: int) -> sqlite3.Row | None:
        with self.tx() as conn:
            return conn.execute(
                "SELECT * FROM documents WHERE source_type='file' "
                "AND org_unit_id=? AND topic_id=?",
                (org_unit_id, topic_id),
            ).fetchone()

    def get_thread_doc(
        self, org_unit_id: int, forum_id: int, thread_id: int
    ) -> sqlite3.Row | None:
        with self.tx() as conn:
            return conn.execute(
                "SELECT * FROM documents WHERE source_type='discussion' "
                "AND org_unit_id=? AND forum_id=? AND thread_id=?",
                (org_unit_id, forum_id, thread_id),
            ).fetchone()

    def upsert_document(self, doc: dict[str, Any]) -> int:
        """Insert or replace a document, clearing its old chunks."""
        cols = (
            "source_type", "org_unit_id", "course_name", "topic_id", "module_path",
            "file_name", "mime_type", "page_count", "content_hash", "forum_id",
            "forum_name", "thread_id", "thread_name", "max_post_id", "title",
            "last_modified", "indexed_at", "extraction_quality",
        )
        values = [doc.get(c) for c in cols]

        with self.tx() as conn:
            existing = None
            if doc.get("source_type") == "file":
                existing = conn.execute(
                    "SELECT doc_id FROM documents WHERE source_type='file' "
                    "AND org_unit_id=? AND topic_id=?",
                    (doc.get("org_unit_id"), doc.get("topic_id")),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT doc_id FROM documents WHERE source_type='discussion' "
                    "AND org_unit_id=? AND forum_id=? AND thread_id=?",
                    (doc.get("org_unit_id"), doc.get("forum_id"), doc.get("thread_id")),
                ).fetchone()

            if existing:
                doc_id = int(existing["doc_id"])
                sets = ", ".join(f"{c} = ?" for c in cols)
                conn.execute(
                    f"UPDATE documents SET {sets} WHERE doc_id = ?", (*values, doc_id)
                )
                self._delete_chunks(conn, doc_id)
            else:
                placeholders = ", ".join("?" for _ in cols)
                cur = conn.execute(
                    f"INSERT INTO documents({', '.join(cols)}) VALUES ({placeholders})",
                    values,
                )
                doc_id = int(cur.lastrowid or 0)

        self._vec_cache = None
        return doc_id

    @staticmethod
    def _delete_chunks(conn: sqlite3.Connection, doc_id: int) -> None:
        ids = [
            r["chunk_id"]
            for r in conn.execute(
                "SELECT chunk_id FROM chunks WHERE doc_id = ?", (doc_id,)
            ).fetchall()
        ]
        if ids:
            marks = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({marks})", ids)
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    def delete_course(self, org_unit_id: int) -> int:
        with self.tx() as conn:
            docs = [
                r["doc_id"]
                for r in conn.execute(
                    "SELECT doc_id FROM documents WHERE org_unit_id = ?", (org_unit_id,)
                ).fetchall()
            ]
            for doc_id in docs:
                self._delete_chunks(conn, doc_id)
            conn.execute("DELETE FROM documents WHERE org_unit_id = ?", (org_unit_id,))
        self._vec_cache = None
        return len(docs)

    def clear_all(self) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM chunks_fts")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM documents")
        self._vec_cache = None

    # --- chunks -----------------------------------------------------------

    def add_chunks(
        self, doc_id: int, org_unit_id: int, chunks: list[dict[str, Any]]
    ) -> int:
        if not chunks:
            return 0
        with self.tx() as conn:
            for ch in chunks:
                emb = ch.get("embedding")
                blob = (
                    np.asarray(emb, dtype=np.float32).tobytes()
                    if emb is not None
                    else None
                )
                cur = conn.execute(
                    "INSERT INTO chunks(doc_id, org_unit_id, position, author_role, "
                    "posted_at, text, token_count, embedding) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        doc_id,
                        org_unit_id,
                        ch.get("position"),
                        ch.get("author_role"),
                        ch.get("posted_at"),
                        ch["text"],
                        ch.get("token_count"),
                        blob,
                    ),
                )
                conn.execute(
                    "INSERT INTO chunks_fts(text, chunk_id) VALUES (?, ?)",
                    (ch["text"], int(cur.lastrowid or 0)),
                )
        self._vec_cache = None
        return len(chunks)

    # --- retrieval --------------------------------------------------------

    def load_vectors(self, org_unit_id: int | None = None) -> tuple[np.ndarray, list[int]]:
        """Return (matrix, chunk_ids). Cached across calls; scoped filtering
        applied as a hard filter, never a ranking preference."""
        if self._vec_cache is None:
            with self.tx() as conn:
                rows = conn.execute(
                    "SELECT chunk_id, org_unit_id, embedding FROM chunks "
                    "WHERE embedding IS NOT NULL ORDER BY chunk_id"
                ).fetchall()
            if not rows:
                self._vec_cache = (np.zeros((0, 0), dtype=np.float32), [], [])
            else:
                vecs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
                dim = len(vecs[0])
                keep = [(r, v) for r, v in zip(rows, vecs) if len(v) == dim]
                mat = np.vstack([v for _, v in keep]).astype(np.float32)
                self._vec_cache = (
                    mat,
                    [int(r["chunk_id"]) for r, _ in keep],
                    [int(r["org_unit_id"]) for r, _ in keep],
                )

        mat, ids, courses = self._vec_cache
        if org_unit_id is None or mat.size == 0:
            return mat, ids
        mask = [i for i, c in enumerate(courses) if c == org_unit_id]
        if not mask:
            return np.zeros((0, mat.shape[1]), dtype=np.float32), []
        return mat[mask], [ids[i] for i in mask]

    def fts_search(
        self, query: str, limit: int, org_unit_id: int | None = None
    ) -> list[tuple[int, float]]:
        """BM25 keyword search. Returns (chunk_id, rank) ascending by rank."""
        match = _fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT f.chunk_id AS chunk_id, bm25(chunks_fts) AS score "
            "FROM chunks_fts f JOIN chunks c ON c.chunk_id = f.chunk_id "
            "WHERE chunks_fts MATCH ?"
        )
        params: list[Any] = [match]
        if org_unit_id is not None:
            sql += " AND c.org_unit_id = ?"
            params.append(org_unit_id)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        try:
            with self.tx() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            log.debug("fts query failed (%s): %r", exc, match)
            return []
        return [(int(r["chunk_id"]), float(r["score"])) for r in rows]

    def hydrate(self, chunk_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not chunk_ids:
            return {}
        marks = ",".join("?" for _ in chunk_ids)
        with self.tx() as conn:
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.text, c.position, c.author_role, c.posted_at,
                       d.source_type, d.org_unit_id, d.course_name, d.topic_id,
                       d.module_path, d.file_name, d.forum_id, d.forum_name,
                       d.thread_id, d.thread_name, d.title, d.indexed_at
                FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
                WHERE c.chunk_id IN ({marks})
                """,
                chunk_ids,
            ).fetchall()
        return {int(r["chunk_id"]): dict(r) for r in rows}

    # --- watermarks -------------------------------------------------------

    def get_watermark(self, org_unit_id: int, category: str) -> str | None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT last_seen FROM watermarks WHERE org_unit_id=? AND category=?",
                (org_unit_id, category),
            ).fetchone()
        return row["last_seen"] if row else None

    def set_watermark(self, org_unit_id: int, category: str, when: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO watermarks(org_unit_id, category, last_seen) VALUES (?,?,?) "
                "ON CONFLICT(org_unit_id, category) DO UPDATE SET last_seen=excluded.last_seen",
                (org_unit_id, category, when),
            )
    def stats(self) -> dict[str, Any]:
        with self.tx() as conn:
            docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        return {
            "documents": docs,
            "chunks": chunks,
            "embed_model": self.embed_model(),
            "schema_version": self.get_meta("schema_version"),
            "path": str(self.path),
        }


def _fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression.

    User queries are natural language, which contains characters FTS5 treats as
    operators. Quoting each token avoids syntax errors on input like
    "what's due?" while keeping useful tokens such as "A3".
    """
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if t]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)
