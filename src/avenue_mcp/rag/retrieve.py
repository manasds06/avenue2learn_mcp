"""Hybrid retrieval: vector + keyword, fused with RRF.

Vector search alone fails exactly the queries students ask most -- "Assignment 3
requirements" retrieves Assignment 2 and 4 because their embeddings are nearly
identical, and that is a catastrophic failure for a deadline question. Keyword
search alone fails the paraphrase cases ("late penalty" vs "submissions received
after the deadline"). Neither is sufficient; both together are.

RRF over score normalization: cosine and BM25 live on incomparable scales, and
any normalization is a fudge factor needing per-corpus tuning. RRF uses ranks
only, has one well-understood constant, and is robust without tuning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

from avenue_mcp.rag.embed import Embedder, cosine_scores
from avenue_mcp.rag.store import Store

log = logging.getLogger(__name__)

RRF_K = 60
CANDIDATES = 20

# Light post-fusion adjustments.
#
# These are PENALTIES ONLY, and that is a correction from the first design.
# An earlier version boosted instructor posts by 1.25x, which looked "small"
# but is not: RRF scores are inherently tiny and tightly packed (1/(60+rank)),
# so a 25% multiplier reliably flips rank 1 and rank 2. In practice it pushed
# an unrelated discussion thread above the course outline for an outline
# question -- the exact cross-corpus contamination the design set out to avoid.
#
# The goal was only ever "an instructor's reply outranks a classmate's guess".
# That is achieved by penalizing student posts, with instructor and TA posts
# sitting at the same 1.0 baseline as file content. No adjustment can now
# promote a discussion above a file; it can only demote speculation.
BOOST_INSTRUCTOR = 1.0
PENALTY_STUDENT_POST = 0.92
PENALTY_STALE_POST = 0.95
STALE_AFTER_DAYS = 240


@dataclass
class Result:
    chunk_id: int
    score: float
    text: str
    citation: dict[str, Any]


def search(
    store: Store,
    embedder: Embedder,
    query: str,
    *,
    top_k: int = 8,
    org_unit_id: int | None = None,
    tz_name: str = "America/Toronto",
) -> list[Result]:
    query = (query or "").strip()
    if not query:
        return []

    # --- vector leg -------------------------------------------------------
    vector_ranked: list[int] = []
    try:
        matrix, ids = store.load_vectors(org_unit_id)
        if matrix.size and ids:
            qvec = embedder.embed_query(query)

            # A dimension mismatch means the index was built with a different
            # model than the one now loaded. Skipping the leg is the honest
            # response: scoring it anyway yields an all-zeros array, and
            # argsort over zeros still returns indices -- which would surface
            # arbitrary chunks as if they were matches.
            if matrix.shape[1] != qvec.shape[0]:
                log.warning(
                    "vector dim mismatch (index=%d, model=%d); skipping vector "
                    "search. Re-sync with force=true.",
                    matrix.shape[1],
                    qvec.shape[0],
                )
            else:
                scores = cosine_scores(qvec, matrix)
                if scores.size:
                    order = np.argsort(-scores)[:CANDIDATES]
                    # Drop non-positive similarity: zero cosine is "unrelated",
                    # not "weakly related", and must not become a candidate.
                    vector_ranked = [
                        ids[int(i)] for i in order if float(scores[int(i)]) > 0.0
                    ]
    except Exception as exc:  # noqa: BLE001 -- keyword leg can still answer
        log.warning("vector search unavailable: %s", exc)

    # --- keyword leg ------------------------------------------------------
    keyword_ranked = [cid for cid, _ in store.fts_search(query, CANDIDATES, org_unit_id)]

    if not vector_ranked and not keyword_ranked:
        return []

    fused = _rrf(vector_ranked, keyword_ranked)
    rows = store.hydrate(list(fused))

    scored: list[Result] = []
    for chunk_id, base in fused.items():
        row = rows.get(chunk_id)
        if row is None:
            continue
        scored.append(
            Result(
                chunk_id=chunk_id,
                score=round(base * _adjust(row), 6),
                text=row["text"],
                citation=_citation(row, tz_name),
            )
        )

    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:top_k]


def _rrf(*rankings: list[int]) -> dict[int, float]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    return fused


def _adjust(row: dict[str, Any]) -> float:
    """Role and recency adjustments. Penalties only -- never above 1.0.

    An instructor's reply should outrank a classmate's guess. It should NOT
    outrank the course outline just for being an instructor's reply, which is
    why nothing here can exceed the 1.0 baseline that file content sits at.
    """
    if row.get("source_type") != "discussion":
        return 1.0

    factor = 1.0
    role = row.get("author_role")
    if role in ("Instructor", "TA"):
        factor *= BOOST_INSTRUCTOR  # 1.0 -- parity with files, not promotion
    elif role == "Student":
        factor *= PENALTY_STUDENT_POST

    posted = row.get("posted_at")
    if posted:
        from avenue_mcp.util.dates import days_until, parse_d2l

        dt = parse_d2l(str(posted))
        age = days_until(dt)
        if age is not None and age < -STALE_AFTER_DAYS:
            factor *= PENALTY_STALE_POST
    return factor


def _citation(row: dict[str, Any], tz_name: str) -> dict[str, Any]:
    """Files and discussions cite differently, and the shape says which.

    author_role and posted_at are in the citation rather than only used for
    ranking, so the model can say "your instructor said this on March 9" instead
    of presenting a forum guess with the authority of the outline.
    """
    from avenue_mcp.util.dates import local_date_str, parse_d2l

    if row.get("source_type") == "discussion":
        posted = parse_d2l(str(row.get("posted_at") or "")) if row.get("posted_at") else None
        return {
            "source_type": "discussion",
            "course_name": row.get("course_name"),
            "org_unit_id": row.get("org_unit_id"),
            "forum_name": row.get("forum_name"),
            "thread_name": row.get("thread_name"),
            "author_role": row.get("author_role") or "Unknown",
            "posted_at_utc": row.get("posted_at"),
            "posted_on": local_date_str(posted, tz_name),
            "forum_id": row.get("forum_id"),
            "thread_id": row.get("thread_id"),
            "position": row.get("position"),
        }

    page = _page_from(row.get("position"))
    return {
        "source_type": "file",
        "course_name": row.get("course_name"),
        "org_unit_id": row.get("org_unit_id"),
        "file_name": row.get("file_name"),
        "title": row.get("title"),
        "module_path": row.get("module_path"),
        "position": row.get("position"),
        "page": page,
        "topic_id": row.get("topic_id"),
        "indexed_at": row.get("indexed_at"),
    }


def _page_from(position: str | None) -> int | None:
    """Pull a page/slide number out of 'p.3' / 'slide 14' so get_page_image
    can be called directly from a citation."""
    if not position:
        return None
    digits = ""
    for ch in position:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return int(digits) if digits else None
