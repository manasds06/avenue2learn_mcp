"""Full RAG round-trip with REAL local embeddings.

This is the last unverified leg: semantic retrieval. It downloads the embedding
model on first run (~130 MB), then indexes a synthetic course and checks that
semantic paraphrase and keyword queries both find the right passage.

Uses a fake D2LClient -- no network, no Avenue session.
"""

import asyncio
import os
import tempfile
from pathlib import Path

os.environ["AVENUE_MCP_STATE_DIR"] = tempfile.mkdtemp(prefix="avenue-rag-")

import pymupdf  # noqa: E402

from avenue_mcp.context import AppContext  # noqa: E402
from avenue_mcp.rag import chunk as chunker  # noqa: E402
from avenue_mcp.rag import extract as extractor  # noqa: E402
from avenue_mcp.tools.search import search_course_materials  # noqa: E402


def make_outline(path: Path) -> None:
    """A realistically wordy outline.

    The first version of this had ~100 characters per page, which the chunker
    correctly merged into a single blob -- producing a useless "p.1-p.4"
    citation and hiding whether retrieval could discriminate between sections at
    all. Real course outlines have paragraphs, so the fixture does too.
    """
    doc = pymupdf.open()
    pages = [
        (
            "COMPSCI 2C03 Course Outline\nWinter 2026\n\n"
            "This course covers the design and analysis of data structures and "
            "algorithms. Topics include asymptotic analysis, sorting and "
            "selection, hash tables, balanced search trees, graph traversal, "
            "shortest paths, and minimum spanning trees. Lectures are held three "
            "times per week and attendance is expected. The teaching team holds "
            "office hours throughout the week; times are posted on the course "
            "homepage and updated as needed during the term."
        ),
        (
            "Grading Scheme\n\n"
            "Assignments are worth 40 percent of the final grade in total, "
            "distributed evenly across four assignments. The midterm test is "
            "worth 25 percent and is written in class. The final exam is worth 35 "
            "percent and is scheduled by the registrar during the examination "
            "period. There is no separate participation component. Marks are "
            "released through the Avenue gradebook, usually within two weeks of "
            "the submission deadline for each item."
        ),
        (
            "Late Policy\n\n"
            "Work submitted after the deadline is docked ten percent for each "
            "day it is overdue, up to a maximum of three days, after which the "
            "submission is no longer accepted and receives a mark of zero. "
            "Weekends count as days for the purposes of this calculation. "
            "Extensions are granted only through the Faculty office with "
            "appropriate documentation; the teaching team cannot grant them "
            "directly. If a technical problem prevents you from submitting, "
            "email the instructor before the deadline rather than afterwards."
        ),
        (
            "Academic Integrity\n\n"
            "All submitted work must be your own. You may discuss general "
            "approaches with classmates, but the code and prose you hand in must "
            "be written by you alone. Use of generative artificial intelligence "
            "tools is permitted for understanding concepts but not for producing "
            "submitted work, and any such use must be disclosed in your report. "
            "Violations are referred to the Office of Academic Integrity under "
            "the McMaster Academic Integrity Policy."
        ),
    ]
    for body in pages:
        page = doc.new_page()
        # insert_textbox wraps; insert_text does not, so long lines would run
        # off the page and vanish from extraction.
        page.insert_textbox(pymupdf.Rect(60, 60, 540, 760), body, fontsize=11)
    doc.save(path)
    doc.close()


async def main() -> int:
    ctx = AppContext()
    print(f"state dir: {ctx.settings.state_dir}")
    print(f"embed model: {ctx.settings.embed_model}")

    # --- build a synthetic indexed course ---------------------------------
    pdf = ctx.settings.cache_dir / "111" / "4455" / "2C03_outline_W26.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    make_outline(pdf)

    ctx.remember_course(111, "COMPSCI 2C03", "CS-2C03")
    result = extractor.extract(pdf)
    print(f"extracted: {len(result.segments)} segment(s), quality={result.quality}")

    chunks = chunker.chunk_segments(
        result.segments,
        course_name="COMPSCI 2C03",
        module_path="Week 1",
        title="Course Outline",
        file_name=pdf.name,
    )
    print(f"chunked: {len(chunks)} chunk(s)")

    doc_id = ctx.store.upsert_document(
        {
            "source_type": "file",
            "org_unit_id": 111,
            "course_name": "COMPSCI 2C03",
            "topic_id": 4455,
            "module_path": "Week 1",
            "file_name": pdf.name,
            "mime_type": "application/pdf",
            "page_count": result.page_count,
            "content_hash": extractor.sha256_file(pdf),
            "title": "Course Outline",
            "indexed_at": "2026-03-01T00:00:00Z",
            "extraction_quality": "ok",
        }
    )

    # Add a discussion thread so both corpora are exercised.
    thread_id = ctx.store.upsert_document(
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
    thread_chunks = chunker.chunk_thread(
        [
            {
                "post_id": 5001, "parent_post_id": None, "author_role": "Student",
                "posted_at": "2026-03-09T14:20:00Z",
                "text": "For question 3, should we implement the recursive version "
                        "or the iterative one?",
            },
            {
                "post_id": 5002, "parent_post_id": 5001, "author_role": "Instructor",
                "posted_at": "2026-03-09T16:45:00Z",
                "text": "Either approach is acceptable, but document which one you "
                        "chose and why in your report.",
            },
        ],
        course_name="COMPSCI 2C03",
        thread_name="A3 clarifications",
    )

    print("\nembedding (first run downloads the model)...")
    for did, chs in ((doc_id, chunks), (thread_id, thread_chunks)):
        vectors = ctx.embedder.embed_documents([c.text for c in chs])
        rows = []
        for i, c in enumerate(chs):
            row = c.to_row()
            row["embedding"] = vectors[i]
            rows.append(row)
        ctx.store.add_chunks(did, 111, rows)
    ctx.store.record_embed_model(ctx.embedder.model_name, ctx.embedder.dim)
    print(f"indexed: {ctx.store.stats()['chunks']} chunk(s), dim={ctx.embedder.dim}")

    # --- queries ----------------------------------------------------------
    # Assertions are on the TOP hit's source, not on "is the text anywhere in
    # the results". The weaker form passed while an unrelated discussion thread
    # was outranking the outline -- exactly the bug it should have caught.
    cases = [
        ("what's the late penalty?", "ten percent", "file", "semantic paraphrase"),
        ("Late Policy", "ten percent", "file", "exact keyword"),
        ("how much is the final exam worth", "35 percent", "file", "semantic, grading"),
        ("should I use recursion for Q3", "Either approach", "discussion", "forum thread"),
    ]

    failures = 0
    for query, expected, want_source, why in cases:
        out = await search_course_materials(ctx, query, org_unit_id=111, top_k=3)
        hits = out["results"]
        if not hits:
            failures += 1
            print(f"\n[FAIL] {query!r}  ({why})\n       (no results)")
            continue

        top = hits[0]
        cite = top["citation"]
        text_ok = expected.lower() in top["text"].lower()
        source_ok = cite["source_type"] == want_source
        ok = text_ok and source_ok
        if not ok:
            failures += 1

        if cite["source_type"] == "file":
            where = f"{cite['file_name']} {cite['position']}"
        else:
            where = f"{cite['thread_name']} ({cite['author_role']}, {cite['posted_on']})"

        print(f"\n[{'PASS' if ok else 'FAIL'}] {query!r}  ({why})")
        print(f"       top: score={top['score']:.4f}  {where}")
        print(f"       text: {top['text'][:110].replace(chr(10), ' ')}...")
        if not source_ok:
            print(f"       -> WRONG SOURCE: wanted {want_source}, got {cite['source_type']}")
        if not text_ok:
            print(f"       -> expected {expected!r} in the top hit")

    # --- scoping ----------------------------------------------------------
    print("\n[scoping] querying a different course id...")
    out = await search_course_materials(ctx, "late penalty", org_unit_id=999, top_k=3)
    assert out["count"] == 0 and "not been indexed" in out["note"]
    print("       correctly reports the course is not indexed")

    print("\n[citations] indexed_courses always present:", out["indexed_courses"])

    await ctx.aclose()
    print("\n" + ("ALL RAG CHECKS PASSED" if failures == 0 else f"{failures} QUERY FAILURE(S)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
