"""Sync orchestration: discover -> diff -> download -> extract -> chunk -> embed -> store.

Always user-initiated. Never on a timer, never triggered by a search miss.

Reports per-file errors rather than failing the whole sync, so one corrupt PDF
does not cost you the other 41 files.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.client import models as m
from avenue_mcp.client.roles import role_map, role_of_post
from avenue_mcp.config import Settings
from avenue_mcp.errors import (
    APIError,
    EmbedModelMismatchError,
    ExtractionError,
    PermissionDeniedError,
)
from avenue_mcp.rag import chunk as chunker
from avenue_mcp.rag import extract as extractor
from avenue_mcp.rag.embed import Embedder
from avenue_mcp.rag.store import Store
from avenue_mcp.util.dates import now_utc, to_utc_iso
from avenue_mcp.util.html import to_text

log = logging.getLogger(__name__)

MAX_TREE_DEPTH = 10


class Syncer:
    def __init__(
        self,
        client: D2LClient,
        store: Store,
        embedder: Embedder,
        settings: Settings,
    ) -> None:
        self.client = client
        self.store = store
        self.embedder = embedder
        self.settings = settings

    # --- entry point ------------------------------------------------------

    async def sync_course(
        self, org_unit_id: int, course_name: str, *, force: bool = False
    ) -> dict[str, Any]:
        started = time.monotonic()
        self._guard_embed_model(force)

        report: dict[str, Any] = {
            "org_unit_id": org_unit_id,
            "course_name": course_name,
            "files_found": 0,
            "files_indexed": 0,
            "files_skipped_unchanged": 0,
            "files_skipped_unsupported": 0,
            "threads_found": 0,
            "threads_indexed": 0,
            "threads_skipped_unchanged": 0,
            "chunks_created": 0,
            "errors": [],
        }

        await self._sync_files(org_unit_id, course_name, report, force=force)

        if self.settings.index_discussions:
            await self._sync_discussions(org_unit_id, course_name, report, force=force)
        else:
            report["discussions"] = "disabled (AVENUE_MCP_INDEX_DISCUSSIONS=0)"

        self.store.record_embed_model(self.embedder.model_name, self.embedder.dim)
        report["duration_seconds"] = round(time.monotonic() - started, 1)
        return report

    def _guard_embed_model(self, force: bool) -> None:
        stored = self.store.embed_model()
        if stored and stored != self.embedder.model_name and not force:
            raise EmbedModelMismatchError(
                f"Index was built with {stored}, configured model is "
                f"{self.embedder.model_name}."
            )
        if stored and stored != self.embedder.model_name and force:
            log.warning("embedding model changed; clearing index")
            self.store.clear_all()

    # --- files ------------------------------------------------------------

    async def _sync_files(
        self, org_unit_id: int, course_name: str, report: dict[str, Any], *, force: bool
    ) -> None:
        try:
            topics = await self.discover_files(org_unit_id)
        except PermissionDeniedError as exc:
            report["errors"].append({"stage": "content", "reason": str(exc)})
            return
        except APIError as exc:
            report["errors"].append({"stage": "content", "reason": str(exc)})
            return

        report["files_found"] = len(topics)

        for topic in topics:
            topic_id = topic["topic_id"]
            name = topic.get("file_name") or topic.get("title") or f"topic {topic_id}"

            if not extractor.is_supported(name):
                report["files_skipped_unsupported"] += 1
                report["errors"].append(
                    {"file_name": name, "reason": "unsupported file type"}
                )
                continue

            # Timestamp check first, and it short-circuits: in the common case
            # (nothing changed) a sync is one tree walk and zero downloads.
            existing = self.store.get_file_doc(org_unit_id, topic_id)
            if existing is not None and not force:
                if topic.get("last_modified") and existing["last_modified"] == topic["last_modified"]:
                    report["files_skipped_unchanged"] += 1
                    continue

            try:
                created = await self._index_file(org_unit_id, course_name, topic, existing, force)
            except ExtractionError as exc:
                report["errors"].append({"file_name": name, "reason": str(exc)})
                continue
            except APIError as exc:
                report["errors"].append({"file_name": name, "reason": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 -- keep going
                log.exception("unexpected error indexing %s", name)
                report["errors"].append({"file_name": name, "reason": f"{type(exc).__name__}: {exc}"})
                continue

            if created is None:
                report["files_skipped_unchanged"] += 1
            else:
                report["files_indexed"] += 1
                report["chunks_created"] += created

    async def discover_files(self, org_unit_id: int) -> list[dict[str, Any]]:
        """Walk the content tree, collecting downloadable file topics.

        Depth-capped and visited-tracked: recursive tree-walking against a
        remote API is a good way to write an accidental infinite loop.
        """
        root = await self.client.get("le", f"{org_unit_id}/content/root/")
        found: list[dict[str, Any]] = []
        seen_modules: set[int] = set()

        async def walk(nodes: Any, path: list[str], depth: int) -> None:
            if depth > MAX_TREE_DEPTH or not isinstance(nodes, list):
                return
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_type = str(m.pick(node, "Type", default="")).lower()
                title = str(m.pick(node, "Title", "Name", default="") or "")

                is_module = node_type == "module" or "Structure" in node or "Modules" in node
                if is_module or m.as_int(m.pick(node, "ModuleId")) is not None:
                    mod_id = m.as_int(m.pick(node, "Id", "ModuleId"))
                    if mod_id is None or mod_id in seen_modules:
                        continue
                    seen_modules.add(mod_id)
                    child_path = path + [title] if title else path

                    structure = m.pick(node, "Structure", "Modules", "Topics")
                    if not isinstance(structure, list):
                        try:
                            structure = await self.client.get(
                                "le", f"{org_unit_id}/content/modules/{mod_id}/structure/"
                            )
                        except APIError as exc:
                            log.debug("module %s unreadable: %s", mod_id, exc)
                            continue
                    await walk(structure, child_path, depth + 1)
                    continue

                # Topic
                topic_id = m.as_int(m.pick(node, "Id", "TopicId"))
                if topic_id is None:
                    continue
                if not m.topic_is_file(node):
                    continue
                file_name = m.guess_filename(node)
                found.append(
                    {
                        "topic_id": topic_id,
                        "title": title or file_name,
                        "file_name": file_name,
                        "mime_type": m.mime_from_name(file_name),
                        "module_path": " / ".join(p for p in path if p) or None,
                        "last_modified": m.pick(node, "LastModifiedDate", "LastModified"),
                    }
                )

        await walk(root if isinstance(root, list) else [root], [], 0)
        # De-duplicate by topic id (a topic can appear in several modules).
        unique: dict[int, dict[str, Any]] = {}
        for t in found:
            unique.setdefault(t["topic_id"], t)
        await self._resolve_missing_filenames(org_unit_id, unique)
        return list(unique.values())

    async def _resolve_missing_filenames(
        self, org_unit_id: int, topics: dict[int, dict[str, Any]]
    ) -> None:
        """Back-fill file names from each topic's detail record when needed.

        Some instances omit `Url` from the module *structure* listing while
        returning it from `content/topics/{id}`. Without it, guess_filename falls
        back to the display title -- which carries no extension -- so
        `extractor.is_supported()` rejects every file and a course with real PDFs
        reports "48 found, 0 indexed, 48 unsupported". Measured on Carleton
        (docs/09); McMaster's listing includes Url, which is why this went
        unnoticed.

        Only topics whose name has no usable extension are fetched, so an
        instance that already returns Url costs nothing.
        """
        pending = [
            (tid, t)
            for tid, t in topics.items()
            if not extractor.is_supported(t.get("file_name") or "")
        ]
        if not pending:
            return

        async def resolve(topic_id: int, topic: dict[str, Any]) -> None:
            try:
                detail = await self.client.get(
                    "le", f"{org_unit_id}/content/topics/{topic_id}"
                )
            except APIError as exc:
                log.debug("topic %s detail unreadable: %s", topic_id, exc)
                return
            if not isinstance(detail, dict):
                return
            # An absolute Url is an external link (a publisher site, a syllabus
            # service), not a file hosted in Brightspace. Downloading it would
            # fetch someone else's HTML, so leave it unsupported.
            url = m.pick(detail, "Url", "Location")
            if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
                return
            name = m.guess_filename(detail)
            if name and extractor.is_supported(name):
                topic["file_name"] = name
                topic["mime_type"] = m.mime_from_name(name)
            if topic.get("last_modified") is None:
                topic["last_modified"] = m.pick(
                    detail, "LastModifiedDate", "LastModified"
                )

        await asyncio.gather(*(resolve(tid, t) for tid, t in pending))

    async def _index_file(
        self,
        org_unit_id: int,
        course_name: str,
        topic: dict[str, Any],
        existing: Any,
        force: bool,
    ) -> int | None:
        topic_id = topic["topic_id"]
        name = topic.get("file_name") or f"topic-{topic_id}"
        dest = self.settings.cache_dir / str(org_unit_id) / str(topic_id) / name

        info = await self.client.download(
            "le", f"{org_unit_id}/content/topics/{topic_id}/file", dest
        )
        actual = Path(info["path"])
        digest = extractor.sha256_file(actual)

        # Secondary check: catches re-uploads that reset the timestamp without
        # changing content.
        if existing is not None and not force and existing["content_hash"] == digest:
            return None

        result = extractor.extract(actual, info.get("mime_type") or topic.get("mime_type"))

        if result.quality != "ok" or not result.segments:
            # Raise BEFORE writing a documents row.
            #
            # Writing it first created a document with zero chunks, which made
            # store.has_course() true -- so the course appeared in
            # `indexed_courses`, search returned [], and the tool description
            # tells the model to read that as "not in the materials". The user
            # was told their outline doesn't mention the late penalty.
            #
            # It also made the failure invisible on re-sync: the row's
            # last_modified matched, so the file counted as "unchanged" and the
            # error was never reported again.
            raise ExtractionError(result.note or f"No text extracted from {name}")

        doc_id = self.store.upsert_document(
            {
                "source_type": "file",
                "org_unit_id": org_unit_id,
                "course_name": course_name,
                "topic_id": topic_id,
                "module_path": topic.get("module_path"),
                "file_name": name,
                "mime_type": info.get("mime_type") or topic.get("mime_type"),
                "page_count": result.page_count,
                "content_hash": digest,
                "title": topic.get("title"),
                "last_modified": topic.get("last_modified"),
                "indexed_at": to_utc_iso(now_utc()),
                "extraction_quality": result.quality,
            }
        )

        chunks = chunker.chunk_segments(
            result.segments,
            course_name=course_name,
            module_path=topic.get("module_path"),
            title=topic.get("title"),
            file_name=name,
        )
        return self._embed_and_store(doc_id, org_unit_id, chunks)

    # --- discussions ------------------------------------------------------

    async def _sync_discussions(
        self, org_unit_id: int, course_name: str, report: dict[str, Any], *, force: bool
    ) -> None:
        try:
            forums = await self.client.get_paged("le", f"{org_unit_id}/discussions/forums/")
        except APIError as exc:
            report["errors"].append({"stage": "discussions", "reason": str(exc)})
            return

        newest_seen: str | None = None
        # Fetched once per course, not per thread. Roster is read for id -> role
        # only; no name ever reaches the index.
        roles = await role_map(self.client, org_unit_id)

        for forum in forums if isinstance(forums, list) else []:
            forum_id = m.as_int(m.pick(forum, "ForumId", "Id"))
            if forum_id is None:
                continue
            forum_name = m.pick(forum, "Name", "Title")

            try:
                topics = await self.client.get_paged(
                    "le", f"{org_unit_id}/discussions/forums/{forum_id}/topics/"
                )
            except APIError as exc:
                report["errors"].append(
                    {"stage": f"forum {forum_id}", "reason": str(exc)}
                )
                continue

            for thread in topics if isinstance(topics, list) else []:
                thread_id = m.as_int(m.pick(thread, "TopicId", "Id"))
                if thread_id is None:
                    continue
                report["threads_found"] += 1
                thread_name = m.pick(thread, "Name", "Title")

                try:
                    posts = await self.client.get_paged(
                        "le",
                        f"{org_unit_id}/discussions/forums/{forum_id}/topics/{thread_id}/posts/",
                    )
                except APIError as exc:
                    report["errors"].append(
                        {"stage": f"thread {thread_id}", "reason": str(exc)}
                    )
                    continue

                normalized = [
                    self._normalize_post(p, roles) for p in posts if isinstance(p, dict)
                ]
                normalized = [p for p in normalized if p.get("text")]
                if not normalized:
                    continue

                max_post = max(
                    (p["post_id"] for p in normalized if p.get("post_id") is not None),
                    default=None,
                )
                for p in normalized:
                    if p.get("posted_at") and (not newest_seen or p["posted_at"] > newest_seen):
                        newest_seen = p["posted_at"]

                # Threads are append-only in practice, so the watermark is the
                # highest post id seen.
                existing = self.store.get_thread_doc(org_unit_id, forum_id, thread_id)
                if (
                    existing is not None
                    and not force
                    and max_post is not None
                    and existing["max_post_id"] == max_post
                ):
                    report["threads_skipped_unchanged"] += 1
                    continue

                doc_id = self.store.upsert_document(
                    {
                        "source_type": "discussion",
                        "org_unit_id": org_unit_id,
                        "course_name": course_name,
                        "forum_id": forum_id,
                        "forum_name": forum_name,
                        "thread_id": thread_id,
                        "thread_name": thread_name,
                        "max_post_id": max_post,
                        "title": thread_name,
                        "last_modified": newest_seen,
                        "indexed_at": to_utc_iso(now_utc()),
                        "extraction_quality": "ok",
                    }
                )
                chunks = chunker.chunk_thread(
                    normalized, course_name=course_name, thread_name=thread_name
                )
                created = self._embed_and_store(doc_id, org_unit_id, chunks)
                report["threads_indexed"] += 1
                report["chunks_created"] += created

        if newest_seen:
            self.store.set_watermark(org_unit_id, "discussions", newest_seen)

    @staticmethod
    def _normalize_post(
        post: dict[str, Any], roles: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Extract only what we keep. Author NAMES are deliberately dropped --
        role carries the signal, and a name would make the index a durable
        record of classmates' opinions.

        `roles` maps user id -> role for instances whose posts carry no role
        field. Without it every indexed post is "Unknown", which strips the
        index of the instructor-vs-classmate distinction that makes forum
        search worth doing. See client/roles.py."""
        body = m.pick(post, "Message", "Body", "Content", default="")
        if isinstance(body, dict):
            body = m.pick(body, "Html", "Text", "Content", default="")
        role = role_of_post(post, roles)
        return {
            "post_id": m.as_int(m.pick(post, "PostId", "Id")),
            "parent_post_id": m.as_int(m.pick(post, "ParentPostId", "ParentId")),
            "author_role": m.normalize_role(role),
            "posted_at": m.pick(post, "DatePosted", "PostingDate", "CreatedDate"),
            "text": to_text(str(body) if body else ""),
        }

    # --- embedding --------------------------------------------------------

    def _embed_and_store(
        self, doc_id: int, org_unit_id: int, chunks: list[chunker.Chunk]
    ) -> int:
        if not chunks:
            return 0
        vectors = self.embedder.embed_documents([c.text for c in chunks])
        rows: list[dict[str, Any]] = []
        for i, ch in enumerate(chunks):
            row = ch.to_row()
            row["embedding"] = vectors[i] if i < len(vectors) else None
            rows.append(row)
        return self.store.add_chunks(doc_id, org_unit_id, rows)
