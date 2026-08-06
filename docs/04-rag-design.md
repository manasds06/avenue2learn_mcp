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
  │ 1. DISCOVER         │   content tree → file topics
  │                     │   forums → topics → posts
  ├─────────────────────┤
  │ 2. DIFF             │   files: LastModifiedDate + hash
  │                     │   threads: max_post_id watermark
  ├─────────────────────┤
  │ 3. DOWNLOAD         │   stream to local cache, size-capped
  ├─────────────────────┤
  │ 4. EXTRACT          │   PDF/DOCX/PPTX/HTML/TXT → text + positions
  │                     │   discussion HTML → threaded text
  ├─────────────────────┤
  │ 5. CHUNK            │   files: structure-aware split
  │                     │   threads: question + reply pairs
  ├─────────────────────┤
  │ 6. EMBED            │   local model → vectors
  ├─────────────────────┤
  │ 7. STORE            │   SQLite (metadata + FTS5) + vectors
  └─────────────────────┘
            │
            ▼
  search_course_materials(query)          ← no session required
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
     role / recency adjustments
            ▼
     top-k passages + citations
            │
            └──▶ get_page_image(topic, page)   ← when the answer is visual
```

---

## The corpus: two sources, not one

**Files are the obvious half. Discussion threads are the half that's easy to forget and often more useful.**

| Source | Contains | Why it's in the index |
|---|---|---|
| **Content files** | Outlines, slides, assignment specs, readings | The stated policies and the lecture material |
| **Discussion posts** | Q&A threads, instructor clarifications | Frequently the **only** place a detail exists |

That second row is worth dwelling on. When a student asks "does A3 want the recursive version?", the answer is often an instructor's two-line reply in a forum thread — it is not in the outline, not on a slide, and not in an announcement. A RAG layer over files alone confidently reports that the materials don't say, which is wrong.

Trade-offs that come with indexing discussions:

- **Instructor replies are high-signal; student speculation is not.** Author role is stored with each chunk and used to boost instructor and TA posts in ranking. A student guessing wrong in a thread should not outrank the outline.
- **Threads go stale.** A clarification from last term's forum can contradict this term's outline. Post timestamps are stored and surfaced in citations so the model can weigh recency.
- **Author names are not stored.** Role only. See [`07-risks-and-policy.md`](07-risks-and-policy.md).

Everything below applies to both sources unless noted.

---

## 1. Discover

**Files.** Walk the content tree via `content/root/` and `content/modules/{id}/structure/` (see [`02-api-surface.md`](02-api-surface.md)), collecting topics where `is_downloadable` is true.

Skip link topics and embedded-page topics — no file body to fetch.

Record for each: `topic_id`, title, file name, MIME type, `LastModifiedDate`, and the module path (e.g. `Week 1 - Introduction / Readings`). **The module path is worth keeping**: it's often the only signal that a file belongs to week 3 rather than week 9, and that's frequently what the user is actually asking about.

**Discussions.** Walk `discussions/forums/` → `topics/` → `posts/`. Record per post: `post_id`, `parent_post_id`, author *role*, timestamp, and body HTML. Thread name and forum name become the equivalent of a file name for citation purposes.

### What the corpus does *not* cover

Recorded so the gap is a known limitation rather than a surprise:

- Files attached to **announcements** rather than posted in Content
- Files attached to **discussion posts**
- Assignment instructions themselves (they live in the dropbox API, not as files) — though `list_assignments` returns them directly
- Anything behind an external link (publisher sites, Echo360, YouTube)

None are hard to add later; all are out of scope for v1. If a search comes up empty, this list is the first place to look for why.

## 2. Diff

Re-indexing 42 unchanged PDFs on every sync is slow and pointless. Skip work when nothing changed:

| Signal | Use |
|---|---|
| `LastModifiedDate` from topic metadata | Primary. Cheap — available without downloading. |
| SHA-256 of file bytes | Secondary. Catches re-uploads that reset the timestamp without changing content. |

A file is re-indexed when `LastModifiedDate` is newer than the stored value **or** its hash differs. `force: true` bypasses both.

The hash check requires downloading the file, so the timestamp check runs first and short-circuits. In the common case — nothing changed — a sync is one tree walk and zero downloads.

**Discussions diff differently.** Threads are append-only in practice, so the signal is the highest `post_id` (or latest post timestamp) seen per topic. A sync fetches only posts newer than the stored watermark rather than re-reading whole threads. That same per-topic watermark is what `get_whats_new` reads to report new replies ([`03-mcp-tools.md`](03-mcp-tools.md)) — one store, two consumers.

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

1. **Every structural unit above the orphan floor becomes its own chunk** — a PDF page, a slide, a heading section. This is what makes a citation point at *one page* instead of a range.
2. If a section exceeds the max chunk size, sub-split on paragraph boundaries.
3. If a paragraph still exceeds it, hard-split with overlap.
4. Only fragments *below* the floor (a slide with three words) merge with a neighbour, because those are retrieval noise alone.

> **Second correction from running it.** The first implementation accumulated
> segments up to `target_tokens` (~600) before emitting. On a real four-page
> course outline that produced **one chunk** cited as `p.1–p.4` — useless for the
> single question this pipeline exists to answer. After the fix the same outline
> yields four chunks, and *"what's the late penalty?"* returns **p.3**.
>
> "Structure-aware" has to mean structural boundaries are *splits*, not hints.

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

### Chunking discussion threads

Different shape, different rule. **The unit is a question-and-reply pair, not a post.**

An instructor's reply reading *"Either is fine, but document your choice"* is meaningless in isolation — embedded alone it retrieves for nothing, and if it did retrieve it would answer nothing. Chunked together with the question it answers, it becomes exactly the passage a student needs.

So: walk the `parent_post_id` tree and chunk a root post together with its direct replies, splitting only when a thread exceeds the max size. Very long threads split on reply-subtree boundaries rather than mid-exchange.

Context header for a discussion chunk:

```
[COMPSCI 2C03 — Discussion: A3 clarifications — Instructor reply, 2026-03-09]

Q (Student): Does Q3 want the recursive version?
A (Instructor): Either is fine, but document your choice.
```

Role and date go in the header deliberately — both are things the model should weigh, since an instructor answer outranks a student guess and a reply from last term may contradict this term's outline.

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
-- One row per indexed source: a content file OR a discussion thread.
CREATE TABLE documents (
    doc_id          INTEGER PRIMARY KEY,
    source_type     TEXT NOT NULL,      -- 'file' | 'discussion'
    org_unit_id     INTEGER NOT NULL,
    course_name     TEXT NOT NULL,
    -- files
    topic_id        INTEGER,            -- content topic id
    module_path     TEXT,
    file_name       TEXT,
    mime_type       TEXT,
    page_count      INTEGER,
    content_hash    TEXT,
    -- discussions
    forum_id        INTEGER,
    forum_name      TEXT,
    thread_id       INTEGER,
    thread_name     TEXT,
    max_post_id     INTEGER,            -- append-only watermark
    -- common
    title           TEXT,
    last_modified   TEXT,
    indexed_at      TEXT,
    extraction_quality TEXT,
    UNIQUE (source_type, org_unit_id, topic_id, thread_id)
);

CREATE TABLE chunks (
    chunk_id    INTEGER PRIMARY KEY,
    doc_id      INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    org_unit_id INTEGER NOT NULL,
    position    TEXT,          -- "p.3" / "slide 14" / "§2.1" / "posts 5001-5004"
    author_role TEXT,          -- discussions only: Instructor / TA / Student
    posted_at   TEXT,          -- discussions only: recency signal
    text        TEXT NOT NULL,
    token_count INTEGER
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, content='chunks', content_rowid='chunk_id'
);

-- Per-course, per-category watermarks backing get_whats_new.
CREATE TABLE watermarks (
    org_unit_id INTEGER NOT NULL,
    category    TEXT NOT NULL,   -- announcements | files | grades | discussions | deadlines
    last_seen   TEXT NOT NULL,   -- ISO timestamp
    PRIMARY KEY (org_unit_id, category)
);

CREATE TABLE index_meta (
    key TEXT PRIMARY KEY, value TEXT   -- embed_model, embed_dim, schema_version
);
```

Two schema notes worth the space:

**`documents` is keyed on a synthetic `doc_id`, not `topic_id`.** A discussion thread has no content topic, so a `topic_id` primary key can't represent it. Getting this wrong early forces a migration once discussions land.

**`watermarks` is per-course *and* per-category.** A single global "last seen" timestamp breaks immediately: sync one course and not another, and the un-synced one either floods the next digest or silently loses changes. Splitting by category means "I've read the announcements but not the new grades" is representable, which is the actual user state.

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

### Ranking adjustments after fusion

**Penalties only. Nothing may exceed the 1.0 baseline that file content sits at.**

| Signal | Adjustment | Why |
|---|---|---|
| Chunk is an **instructor/TA** discussion post | 1.0 — parity with files | Authoritative, but not *more* authoritative than the outline |
| Chunk is a **student-only** discussion post | ×0.92 | Speculation shouldn't outrank the outline |
| Discussion post older than ~2 terms | ×0.95 | Last term's clarification may contradict this term's rules |

> **This is a correction, found by actually running it.** The first implementation
> *boosted* instructor posts ×1.25. That reads as "small" and is not: RRF scores
> are inherently tiny and tightly packed (`1/(60+rank)`), so a 25% multiplier
> reliably flips rank 1 and rank 2. In the smoke run it pushed an unrelated forum
> thread above the course outline for the question *"what's the late penalty?"* —
> precisely the cross-corpus contamination this design set out to prevent.
>
> The goal was only ever *"an instructor's reply outranks a classmate's guess."*
> That is achieved by demoting student posts, with instructor and TA posts at
> parity with files. No adjustment can now promote a discussion above a file; it
> can only demote speculation.

The lesson generalizes: a multiplicative tweak on a score scale you haven't
inspected is not a small change. The eval set is what decides whether any of
these help at all.

### Citations

Files and discussions cite differently, and the shape says which:

```json
{
  "source_type": "file",
  "course_name": "COMPSCI 2C03",
  "file_name": "2C03_outline_W26.pdf",
  "page": 3,
  "module_path": "Week 1 - Introduction",
  "topic_id": 4455
}
```

```json
{
  "source_type": "discussion",
  "course_name": "COMPSCI 2C03",
  "forum_name": "Assignment Q&A",
  "thread_name": "A3 clarifications",
  "author_role": "Instructor",
  "posted_at": "2026-03-09T16:45:00.000Z",
  "forum_id": 90,
  "thread_id": 412
}
```

`topic_id` lets the model chain into `read_content_file` or `get_page_image`; `forum_id` + `thread_id` chain into `read_discussion_thread` for the surrounding conversation.

**`author_role` and `posted_at` are in the citation, not just used internally for ranking** — so the model can tell the user *"your instructor said this in a forum reply on March 9"* rather than presenting a forum guess with the same authority as the outline. Provenance is part of the answer here, not metadata.

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
- `get_whats_new` reports newly posted files with `indexed: false`, so the model can proactively say "there's a new Week 9 deck — want me to index it?" That closes the freshness loop *on demand* without a background job.

**Local search works with no session.** The index is on disk and needs no network, so `search_course_materials` keeps answering after an Avenue session expires. This is a deliberate property, not an accident: a student mid-study-session whose cookies died can still search everything they've indexed, and only the live-data tools degrade. Stated in [`03-mcp-tools.md`](03-mcp-tools.md) and [`05-architecture.md`](05-architecture.md) so it doesn't get refactored away.

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

Discussions add little: a course's forums are a few hundred KB of text, so indexing them is seconds. The cost is API round-trips walking forums → topics → posts, which is throttled like everything else.

---

## Page rendering — the visual escape hatch

Text extraction has a hard ceiling: **it gets the words and loses the picture.** A slide reading "Red-Black Tree Rotations" with a diagram beneath it extracts to five words and no information. The student's actual question is about the diagram.

So `get_page_image` ([`03-mcp-tools.md`](03-mcp-tools.md)) renders a single page or slide to an image the client's model can look at.

| Aspect | Decision |
|---|---|
| Renderer | `pymupdf` for PDF. PPTX converted to PDF first (LibreOffice headless), then rendered. |
| Source | The **locally cached file** — no new API surface, no re-download if already synced |
| Default DPI | 120 — legible without being enormous |
| Granularity | **One page per call**, deliberately |
| Cache | Rendered pages cached under `~/.avenue-mcp/cache/renders/` |

**One page per call is a guardrail, not a limitation.** A tool that could render thirty slides in one go would get used that way, and thirty images is an enormous amount of context for a question that needed one. The constraint forces the model to identify the page first — usually via a search citation, which already carries a page number.

The PPTX path has a real dependency cost: LibreOffice. It should degrade to a clear "install LibreOffice to render slides" error rather than failing obscurely, and PDF rendering — the common case — must work with no extra system dependency.

This composes with search: a citation gives *file plus page*, and this turns that into something viewable. That chain — semantic search finds the slide, rendering shows the diagram — is the thing neither capability does alone.

## Open questions for Phase 3

Empirical, all of them. None should be settled by intuition.

1. Does the module path (`Week 3 / ...`) in the context header improve retrieval enough to justify its token cost?
2. Is 600 tokens the right target for outlines specifically? They may want larger chunks than slides — per-document-type sizing is a plausible refinement.
3. Are PPTX speaker notes signal or noise? Suspicion: high signal, since the slide says "Amortized Analysis" and the notes explain it.
4. How common are scanned PDFs at McMaster? If frequent, OCR moves from out-of-scope to necessary.
5. Does `bge-small` handle CS/math notation adequately, or is `bge-base` worth the size?
6. **Do discussion chunks help or hurt?** They could be the highest-value source or they could flood results with student speculation. Measure with discussions in and out of the index.
7. **Is question+reply the right discussion chunk unit,** or do long multi-turn threads need a different split?
8. **Are the role and recency boosts doing anything measurable,** or are they superstition?

### The eval set

Build this **before** tuning anything — ~15 real questions with known answers and known source locations:

| Question | Expected source |
|---|---|
| "What's the late penalty in 2C03?" | `2C03_outline_W26.pdf` p.3 |
| "What's the A3 weight?" | outline, grading section |
| "Which lecture covered red-black trees?" | Week 7 deck |
| "Does A3 want the recursive version?" | discussion thread, instructor reply |
| … | … |

Include at least a few questions whose answer is *only* in a discussion thread and a few whose answer is *only* visual — those are the cases that justify the two features added here, and if they don't measurably improve, that's worth knowing.

Retrieval quality is not assessable by vibes. Without this set, "does hybrid beat vector-only?" and "do discussions help?" are unanswerable, and every tuning decision is guesswork dressed as engineering.
