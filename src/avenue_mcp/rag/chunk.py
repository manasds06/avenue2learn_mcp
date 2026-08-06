"""Chunking.

Structure-aware, not fixed-size. This is the single highest-leverage quality
decision in the pipeline: a naive sliding window splits a course outline's late
policy across two chunks and neither retrieves cleanly for "what's the late
penalty?".

Discussions chunk differently -- the unit is a question-and-reply pair, not a
post. An instructor's "Either is fine, but document your choice" is meaningless
alone; paired with the question it answers, it is exactly the passage a student
needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from avenue_mcp.rag.extract import Segment

TARGET_TOKENS = 600
MAX_TOKENS = 1000
OVERLAP_TOKENS = 80
MIN_TOKENS = 50

# Rough token estimate. Good enough for chunk sizing; we are not billing on it.
CHARS_PER_TOKEN = 4


def est_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _max_chars(tokens: int) -> int:
    return tokens * CHARS_PER_TOKEN


@dataclass
class Chunk:
    text: str                       # embedded text (includes context header)
    body: str                       # text without the header, for display
    position: str
    token_count: int
    author_role: str | None = None
    posted_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "text": self.body,
            "position": self.position,
            "token_count": self.token_count,
            "author_role": self.author_role,
            "posted_at": self.posted_at,
        }


def file_context_header(
    course_name: str, module_path: str | None, title: str | None,
    file_name: str | None, position: str,
) -> str:
    """Prepended before embedding, stripped from the returned text.

    Without it, a chunk saying "10% per day" is semantically identical whether
    it came from COMPSCI 2C03 or MATH 2Z03 -- and cross-course contamination is
    the most damaging retrieval error possible here, because it produces an
    answer that is confidently wrong rather than obviously empty.
    """
    bits = [course_name]
    if module_path:
        bits.append(module_path)
    if title and title != file_name:
        bits.append(title)
    if file_name:
        bits.append(file_name)
    return f"[{' — '.join(b for b in bits if b)}, {position}]"


def discussion_context_header(
    course_name: str, thread_name: str | None, role: str | None, posted: str | None
) -> str:
    bits = [course_name]
    if thread_name:
        bits.append(f"Discussion: {thread_name}")
    if role and role != "Unknown":
        bits.append(f"{role} reply")
    if posted:
        bits.append(posted[:10])
    return f"[{' — '.join(bits)}]"


# --- files ----------------------------------------------------------------


def chunk_segments(
    segments: list[Segment],
    *,
    course_name: str,
    module_path: str | None = None,
    title: str | None = None,
    file_name: str | None = None,
    target_tokens: int = TARGET_TOKENS,
    max_tokens: int = MAX_TOKENS,
) -> list[Chunk]:
    """Split at structural boundaries first, then paragraphs, then hard-split.

    A segment is a structural unit -- a PDF page, a slide, a heading section --
    and each one that carries real content becomes its own chunk so its citation
    points at one page rather than a range.

    Only genuinely tiny fragments (a slide with three words) are merged with a
    neighbour, because those are retrieval noise on their own.

    An earlier version accumulated segments up to `target_tokens` before
    emitting. That turned a four-page course outline into a single chunk cited as
    "p.1-p.4" -- useless for the one question this whole pipeline exists to
    answer ("what's the late penalty?" -> outline, page 3). Structural
    boundaries are now respected as splits, not merely as hints.
    """
    out: list[Chunk] = []
    pending: list[Segment] = []

    def header(pos: str) -> str:
        return file_context_header(course_name, module_path, title, file_name, pos)

    def emit(group: list[Segment]) -> None:
        if not group:
            return
        body = "\n\n".join(s.text.strip() for s in group if s.text.strip()).strip()
        if not body:
            return
        pos = group[0].position if len(group) == 1 else f"{group[0].position}-{group[-1].position}"
        if est_tokens(body) <= max_tokens:
            out.append(_make(body, pos, header(pos)))
            return
        for i, piece in enumerate(_split(body, target_tokens, max_tokens), start=1):
            sub = pos if i == 1 else f"{pos} ({i})"
            out.append(_make(piece, sub, header(sub)))

    for seg in segments:
        if not seg.text.strip():
            continue

        # Anything above the orphan floor is a citable unit in its own right.
        if est_tokens(seg.text) >= MIN_TOKENS:
            emit(pending)
            pending = []
            emit([seg])
            continue

        # Sub-minimum: buffer and merge with adjacent scraps.
        pending.append(seg)
        if sum(est_tokens(s.text) for s in pending) >= target_tokens:
            emit(pending)
            pending = []

    emit(pending)
    return _merge_tiny(out, header)


def _make(body: str, position: str, header: str) -> Chunk:
    embedded = f"{header}\n\n{body}"
    return Chunk(
        text=embedded, body=body, position=position, token_count=est_tokens(body)
    )


def _split(text: str, target_tokens: int, max_tokens: int) -> list[str]:
    """Paragraph-boundary split, hard-splitting only when a paragraph is huge."""
    target = _max_chars(target_tokens)
    hard = _max_chars(max_tokens)
    overlap = _max_chars(OVERLAP_TOKENS)

    paras = [p.strip() for p in text.split("\n\n") if p.strip()] or [text]
    pieces: list[str] = []
    cur = ""

    for para in paras:
        if len(para) > hard:
            if cur:
                pieces.append(cur)
                cur = ""
            start = 0
            while start < len(para):
                end = min(start + hard, len(para))
                if end < len(para):
                    space = para.rfind(" ", start + int(hard * 0.7), end)
                    if space > start:
                        end = space
                pieces.append(para[start:end].strip())
                if end >= len(para):
                    break
                start = max(start + 1, end - overlap)
            continue

        candidate = f"{cur}\n\n{para}".strip() if cur else para
        if len(candidate) > target and cur:
            pieces.append(cur)
            tail = cur[-overlap:] if overlap and len(cur) > overlap else ""
            cur = f"{tail}\n\n{para}".strip() if tail else para
        else:
            cur = candidate

    if cur.strip():
        pieces.append(cur.strip())
    return [p for p in pieces if p.strip()]


def _merge_tiny(chunks: list[Chunk], header_fn: Any) -> list[Chunk]:
    """Fold sub-minimum chunks into a neighbour."""
    if len(chunks) < 2:
        return chunks
    out: list[Chunk] = []
    for ch in chunks:
        if ch.token_count < MIN_TOKENS and out:
            prev = out[-1]
            if prev.token_count + ch.token_count <= MAX_TOKENS:
                body = f"{prev.body}\n\n{ch.body}".strip()
                pos = f"{prev.position}-{ch.position}"
                out[-1] = _make(body, pos, header_fn(pos))
                continue
        out.append(ch)
    return out


# --- discussions ----------------------------------------------------------


def chunk_thread(
    posts: list[dict[str, Any]],
    *,
    course_name: str,
    thread_name: str | None,
    max_tokens: int = MAX_TOKENS,
) -> list[Chunk]:
    """Chunk a thread as question + direct replies.

    Each post dict wants: post_id, parent_post_id, author_role, posted_at, text.
    Author names are never present -- only roles. See docs/07-risks-and-policy.md.
    """
    if not posts:
        return []

    by_id = {p["post_id"]: p for p in posts if p.get("post_id") is not None}
    children: dict[Any, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for p in posts:
        parent = p.get("parent_post_id")
        if parent is not None and parent in by_id:
            children.setdefault(parent, []).append(p)
        else:
            roots.append(p)

    def render(p: dict[str, Any], prefix: str) -> str:
        role = p.get("author_role") or "Unknown"
        text = (p.get("text") or "").strip()
        return f"{prefix} ({role}): {text}" if text else ""

    out: list[Chunk] = []
    for root in roots:
        lines = [render(root, "Q")]
        replies = children.get(root.get("post_id"), [])
        # Instructor and TA replies first: they are the authoritative answer,
        # and if a long thread has to be split they should stay with the question.
        replies.sort(key=lambda r: 0 if r.get("author_role") in ("Instructor", "TA") else 1)

        best_role = root.get("author_role")
        latest = root.get("posted_at")
        for reply in replies:
            lines.append(render(reply, "A"))
            if reply.get("author_role") in ("Instructor", "TA"):
                best_role = reply["author_role"]
            if reply.get("posted_at") and (not latest or reply["posted_at"] > latest):
                latest = reply["posted_at"]

        body = "\n".join(ln for ln in lines if ln).strip()
        if not body:
            continue

        ids = [root.get("post_id")] + [r.get("post_id") for r in replies]
        ids = [i for i in ids if i is not None]
        position = (
            f"posts {min(ids)}-{max(ids)}" if len(ids) > 1 else f"post {ids[0]}"
            if ids else "thread"
        )
        header = discussion_context_header(course_name, thread_name, best_role, latest)

        if est_tokens(body) <= max_tokens:
            out.append(
                Chunk(
                    text=f"{header}\n\n{body}",
                    body=body,
                    position=position,
                    token_count=est_tokens(body),
                    author_role=best_role,
                    posted_at=latest,
                )
            )
        else:
            for i, piece in enumerate(_split(body, TARGET_TOKENS, max_tokens), start=1):
                out.append(
                    Chunk(
                        text=f"{header}\n\n{piece}",
                        body=piece,
                        position=f"{position} ({i})",
                        token_count=est_tokens(piece),
                        author_role=best_role,
                        posted_at=latest,
                    )
                )
    return out
