# 04 — RAG Design

How course files become answerable questions.

## The job

A student asks: *"What's the late penalty in 2C03?"*

The answer is one sentence on page 3 of a PDF called `2C03_outline_W26.pdf`, sitting in the Week 1 module of a course's Content section. Nothing in the API returns it. Nothing in the model's training data contains it. The only way to answer is to have read the file.

That's the job: download course files, extract their text, index it, and make it searchable by meaning — with citations, so the user can verify and the model can't quietly invent a policy.

## Constraints that shape the design

**Everything runs locally.** No embedding API, no vector database service, no course content leaving the machine. This is graded material and personal academic data; the privacy argument is strong on its own, and the practical arguments — free, offline, no key to manage — all point the same way. Anthropic has no embeddings endpoint, so an API key wouldn't help here regardless.

**The corpus is small but dense.** Five courses × ~40 files × ~30 pages is a few thousand pages. That's nothing for a vector index — it fits comfortably in memory and searches in milliseconds. **Optimize for retrieval quality, not scale.** Any competent index will be fast enough; the hard part is returning the *right* passage.

**Queries are specific and jargon-heavy.** "A3", "Lab 7", "the 2C03 midterm", "Dijkstra". These are exactly where pure semantic search is weakest — embeddings blur the difference between "Assignment 2" and "Assignment 3", which is a catastrophic failure for a deadline question. This drives the hybrid retrieval decision below.

**Citations are non-negotiable.** An unattributed passage about a late penalty is worse than no answer: the student can't check it, and if the retrieval was wrong they act on a policy from a different course.

## Pipeline

```
  sync_course_materials(org_unit_id)
            │
            ▼
  ┌─────────────────────┐
  │ 1. DISCOVER         │   walk content tree → list file topics
  ├─────────────────────┤
  │ 2. DIFF             │   skip unchanged (LastModifiedDate + hash)
  ├─────────────────────┤
  │ 3. DOWNLOAD         │   stream to local cache, size-capped
  ├─────────────────────┤
  │ 4. EXTRACT          │   PDF/DOCX/PPTX/HTML/TXT → text + positions
  ├─────────────────────┤
  │ 5. CHUNK            │   structure-aware split, with overlap
  ├─────────────────────┤
  │ 6. EMBED            │   local model → vectors
  ├─────────────────────┤
  │ 7. STORE            │   SQLite (metadata + FTS) + vector index
  └─────────────────────┘
            │
            ▼
  search_course_materials(query)
            │
   ┌────────┴────────┐
   │                 │
 vector           keyword
 search            (FTS5)
   │                 │
   └────────┬────────┘
            ▼
     reciprocal rank fusion
            ▼
     top-k passages + citations
```

---

## 1. Discover

Walk the content tree via `content/root/` and `content/modules/{id}/structure/` (see [`02-api-surface.md`](02-api-surface.md)), collecting topics where `is_downloadable` is true.

Skip link topics and embedded-page topics — no file body to fetch.

Record for each: `topic_id`, title, file name, MIME type, `LastModifiedDate`, and the module path (e.g. `Week 1 - Introduction / Readings`). **The module path is worth keeping**: it's often the only signal that a file belongs to week 3 rather than week 9, and that's frequently what the user is actually asking about.

## 2. Diff

Re-indexing 42 unchanged PDFs on every sync is slow and pointless. Skip work when nothing changed:

| Signal | Use |
|---|---|
| `LastModifiedDate` from topic metadata | Primary. Cheap — available without downloading. |
| SHA-256 of file bytes | Secondary. Catches re-uploads that reset the timestamp without changing content. |

A file is re-indexed when `LastModifiedDate` is newer than the stored value **or** its hash differs. `force: true` bypasses both.

The hash check requires downloading the file, so the timestamp check runs first and short-circuits. In the common case — nothing changed — a sync is one tree walk and zero downloads.

## 3. Download

Stream to `~/.avenue-mcp/cache/{org_unit_id}/{topic_id}/{filename}`.

| Rule | Value | Why |
|---|---|---|
| Stream, don't buffer | — | Lecture decks run 20–80 MB |
| Size cap | 100 MB, configurable | One pathological file shouldn't stall a sync |
| Timeout | 120 s per file | |
| Concurrency | 4 (shared with the global limit) | Politeness — see [`02`](02-api-surface.md) |
| On failure | Log, record in `errors[]`, continue | One bad file must not cost the other 41 |

Keeping the raw files (not just extracted text) means re-chunking with a better strategy later doesn't require re-downloading everything. Cheap insurance.

## 4. Extract

Text plus **position information**, because positions become citations.

| Format | Library | Position unit | Notes |
|---|---|---|---|
| PDF | `pymupdf` | page number | Fast, good layout handling. Falls back to `pdfplumber` on parse failure. |
| DOCX | `python-docx` | paragraph index → section heading | Headings preserved for structure-aware chunking |
| PPTX | `python-pptx` | slide number | Extract slide text **and speaker notes** — notes often carry the actual explanation |
| HTML | `beautifulsoup4` | heading path | Strip nav/scripts; keep link targets |
| TXT / MD | stdlib | line number | |

**Scanned PDFs are a real failure case.** Some course outlines are photocopies. `pymupdf` returns near-empty text for these. Detect it — extracted text under ~100 characters for a multi-page document — and record `extraction_quality: "poor"` in `errors[]` with a clear reason. **Do not silently index an empty document**: it produces a file that appears indexed, is never retrieved, and leaves the user wondering why searching for the outline finds nothing. OCR is explicitly out of scope for v1; the honest failure is better than a silent one.

Unsupported types (`.mp4`, `.zip`, `.xlsx`, images) are skipped and reported in `files_skipped_unsupported`.

## 5. Chunk

**Structure-aware, not fixed-size.** This is the single highest-leverage quality decision in the pipeline.

Course documents are heavily structured — outlines have sections like *Grading*, *Late Policy*, *Academic Integrity*; marking schemes have per-criterion breakdowns. A naive 500-token sliding window splits the late policy across two chunks, and neither retrieves cleanly for "what's the late penalty?" Splitting on section boundaries keeps the answer intact in one passage.

Strategy:

1. Split at structural boundaries first — PDF headings, DOCX heading styles, PPTX slide breaks, HTML `<h1>`–`<h3>`.
2. If a section exceeds the max chunk size, sub-split on paragraph boundaries.
3. If a paragraph still exceeds it, hard-split with overlap.

| Parameter | Default | Rationale |
|---|---|---|
| Target chunk size | ~600 tokens | Enough for a full policy section; small enough to be precise |
| Max chunk size | 1000 tokens | Hard ceiling |
| Overlap | ~80 tokens | Catches answers straddling a boundary |
| Min chunk size | 50 tokens | Below this, merge with a neighbour — orphan fragments are retrieval noise |

**Every chunk carries a context header** prepended before embedding:

```
[COMPSCI 2C03 — Week 1 / Course Outline — 2C03_outline_W26.pdf, p.3]

Late assignments are penalized 10% per day ...
```

This matters more than it looks. Without it, a chunk saying "10% per day" is semantically identical whether it came from 2C03 or MATH 2Z03 — and cross-course contamination is the most damaging retrieval error possible here, because it produces an answer that's confidently wrong rather than obviously empty. The header is stripped from the returned text but included in the embedded text.

## 6. Embed

Local model. No API key, no network, no coursework leaving the machine.

| Choice | Default | Notes |
|---|---|---|
| Runtime | `fastembed` | ONNX, no PyTorch dependency, fast on CPU. `sentence-transformers` as the fallback if a model isn't available in fastembed. |
| Model | `BAAI/bge-small-en-v1.5` | 384-dim, ~130 MB, strong quality-per-byte on English technical text |
| Alternative | `BAAI/bge-base-en-v1.5` | 768-dim, better quality, ~4× the size — configurable for users who want it |

Config exposes `AVENUE_MCP_EMBED_MODEL` so this is swappable without code changes.

**Model identity is recorded with every vector.** Changing the embedding model invalidates the entire index — vectors from different models are not comparable, and mixing them silently produces garbage rankings. On startup, if the configured model differs from the stored one, the server reports that a re-index is required rather than searching a mixed index.

First run downloads model weights (~130 MB), once. Document it in setup so it isn't mistaken for a hang.

## 7. Store

Two structures, one SQLite file at `~/.avenue-mcp/index.db`.

**Metadata + keyword search — SQLite with FTS5**

```sql
CREATE TABLE documents (
    topic_id        INTEGER PRIMARY KEY,
    org_unit_id     INTEGER NOT NULL,
    course_name     TEXT NOT NULL,
    module_path     TEXT,
    title           TEXT,
    file_name       TEXT,
    mime_type       TEXT,
    last_modified   TEXT,
    content_hash    TEXT,
    indexed_at      TEXT,
    extraction_quality TEXT
);

CREATE TABLE chunks (
    chunk_id    INTEGER PRIMARY KEY,
    topic_id    INTEGER NOT NULL REFERENCES documents(topic_id) ON DELETE CASCADE,
    org_unit_id INTEGER NOT NULL,
    position    TEXT,          -- "p.3" / "slide 14" / "§2.1"
    text        TEXT NOT NULL,
    token_count INTEGER
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, content='chunks', content_rowid='chunk_id'
);

CREATE TABLE index_meta (
    key TEXT PRIMARY KEY, value TEXT   -- embed_model, embed_dim, schema_version
);
```

**Vectors**

Given the corpus size (low thousands of chunks), a NumPy array persisted alongside the DB, brute-force cosine-scanned, is genuinely sufficient — sub-millisecond at this scale, zero extra dependencies, nothing to corrupt. `sqlite-vec` is the upgrade path if the corpus ever grows enough to matter, and the storage layer is abstracted so swapping it is contained.

Resisting a heavyweight vector DB here is deliberate. At a few thousand chunks it adds operational surface and dependency weight to solve a problem that doesn't exist.

---

## Retrieval

**Hybrid: vector + keyword, fused.** This is the second high-leverage decision.

Vector search alone fails on exactly the queries students ask most:

| Query | Vector-only failure |
|---|---|
| "Assignment 3 requirements" | Retrieves Assignment 2 and 4 — near-identical embeddings |
| "what's on the midterm" | Misses a doc that says "Test 1" |
| "Dijkstra" | Fine — this is where vectors win |
| "late penalty" | Fine — semantic paraphrase, exactly the vector strength |

Keyword search alone fails the paraphrase cases ("late penalty" vs "submissions received after the deadline"). Neither is sufficient; both together are.

**Fusion: Reciprocal Rank Fusion.**

```
score(chunk) = Σ  1 / (k + rank_in_list)      k = 60
```

RRF over score normalization, because vector cosine scores and FTS5 BM25 scores are on incomparable scales and any normalization is a fudge factor that needs tuning per corpus. RRF only uses ranks, has one well-understood constant, and is robust without tuning.

**Pipeline:**

1. Retrieve top 20 by vector similarity (filtered by `org_unit_id` if scoped).
2. Retrieve top 20 by FTS5 BM25, same filter.
3. Fuse with RRF.
4. Return top `k` (default 8).
5. Attach citations.

**Course scoping is a hard filter, not a ranking boost.** When `org_unit_id` is supplied, other courses are excluded at query time — not down-weighted. Returning a MATH policy for a COMPSCI question is the worst available outcome, and a soft preference doesn't reliably prevent it.

### Citations

Every result carries:

```json
{
  "course_name": "COMPSCI 2C03",
  "file_name": "2C03_outline_W26.pdf",
  "page": 3,
  "module_path": "Week 1 - Introduction",
  "topic_id": 4455
}
```

`topic_id` lets the model chain into `read_content_file` for full context when a passage isn't enough.

### The empty-result problem

An empty result is ambiguous in a way that matters: *"the materials don't say"* and *"you never indexed that course"* are completely different answers to give a user, and confusing them produces a confident "your outline doesn't mention late penalties" when the truth is nothing was ever synced.

So `search_course_materials` returns `indexed_courses` on **every** call, empty or not. The model can then say "COMPSCI 2C03 hasn't been indexed — run `sync_course_materials` first" instead of asserting a fact about content it never saw.

---

## Freshness

The index is a snapshot. A prof posting a revised outline on Tuesday doesn't reach a Monday index.

Deliberate design choice: **no background sync, no auto-refresh, no polling.** Reasons — a background daemon hitting Avenue on a timer is exactly the traffic pattern to avoid ([`02`](02-api-surface.md)); a sync needs a live session, and popping a login window from a background task is hostile; and unattended network activity against a university system is a thing to be able to say plainly you don't do.

Instead:
- `sync_course_materials` is explicit and user-initiated.
- Every document records `indexed_at`; search results can surface staleness.
- The tool description tells the model to suggest a re-sync when materials seem out of date.

## Performance envelope

Rough expectations for one course, ~40 files, ~1200 pages, on a typical laptop:

| Stage | First sync | Re-sync (nothing changed) |
|---|---|---|
| Discover | ~2 s | ~2 s |
| Download | 30–90 s | 0 s |
| Extract | 20–60 s | 0 s |
| Embed | 15–40 s | 0 s |
| **Total** | **~1–3 min** | **~2 s** |

Search: **under 100 ms** — a brute-force scan of a few thousand 384-dim vectors is trivial.

The first-sync number is why `sync_course_materials`' description warns about duration and advises one course at a time. A tool call that silently runs for three minutes reads as a hang.

## Open questions for Phase 3

1. Does the module path (`Week 3 / ...`) improve retrieval enough to justify its context-header cost? Measure against a held-out question set.
2. Is 600 tokens the right target for course outlines specifically? They may want larger chunks than lecture slides — per-document-type chunk sizing is a plausible refinement.
3. Are PPTX speaker notes signal or noise? Suspicion: high signal, since the slide says "Amortized Analysis" and the notes explain it.
4. How common are scanned PDFs at McMaster? If frequent, OCR moves from out-of-scope to necessary.
5. Does `bge-small` handle CS/math notation adequately, or is `bge-base` worth the size?

Each is an empirical question, and none should be answered by guessing. Build a small set of real questions with known answers ("what's the late penalty in 2C03?" → known page) and measure retrieval against it.
