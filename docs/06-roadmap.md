# 06 — Roadmap

Phases, deliverables, and exit criteria. Each phase ends with something demonstrable — no phase is "refactoring" or "research" alone.

## Summary

| Phase | Deliverable | Rough size |
|---|---|---|
| **0a** | Try an existing D2L MCP server against Avenue | 20 min |
| **0** | Probe — what can a student account actually reach? | Half a day |
| **1** | Auth + HTTP client | 1–2 days |
| **2** | Read-only tools (14 of them) | 3–4 days |
| **3** | RAG — files, then discussions, then page rendering | 3–5 days |
| **4** | Polish, caching, keepalive, setup | 1–2 days |
| **5** | Write tools — deferred, gated | Not scoped |

Sizes assume part-time work by one person and are estimates, not commitments. Phases 2 and 3 grew from the original plan when quizzes, discussions, the what's-new digest, page rendering, and diagnostics were added. Phase 3 has the widest range because document extraction always contains one surprise.

---

## Phase 0 — Probe

**Nothing downstream is trustworthy until this runs.**

Four routes in [`02-api-surface.md`](02-api-surface.md) are marked ⚠️ — documented as instructor-scope, unknown for students. Two features hinge on them: assignments (`dropbox/folders/`) and grade projection (`grades/`). Building the tool layer before knowing the answers means designing against guesses.

### Step 0a — the twenty-minute shortcut, before writing any code

**Install an existing D2L MCP server and point it at Avenue.** `RohanMuppa/brightspace-mcp-server` advertises MFA support and "works with any school":

```
npx brightspace-mcp-server@latest
```

| Outcome | What it tells us |
|---|---|
| Lists your courses | The entire cookie-session premise in [`01-authentication.md`](01-authentication.md) is validated. Proceed with confidence. |
| Login fails | McMaster's Entra chain breaks its automation — and *how* it breaks is exactly what our login flow must handle |
| Logs in but returns nothing | Auth works, permissions are tighter than expected. Reprioritize the ⚠️ probes. |

Twenty minutes to de-risk the foundational assumption of the whole project. Do this first.

It is **not** a substitute for the probe: it's read-only, has no semantic search, is TypeScript, and won't tell us which specific ⚠️ routes work. It answers one question — *does this approach work at McMaster at all* — and that question gates everything else.

### Deliverable

A throwaway script — `scripts/probe.py`, not shipped, not polished — that:

1. Runs the Playwright login flow, persists a session.
2. Hits every route in [`02-api-surface.md`](02-api-surface.md) against a real course.
3. Records status code, response shape, and a redacted sample for each.
4. Measures session lifetime (idle and active), **and whether activity extends it** — this gates the keepalive decision.
5. Writes findings into [`08-api-probe-results.md`](08-api-probe-results.md).
6. Saves raw responses to `tests/fixtures/` — these become the test corpus for every later phase.

### Questions it must answer

| Question | Why it matters |
|---|---|
| Does `GET .../dropbox/folders/` work for a student? | Gates the entire assignments feature |
| Does `GET .../grades/` (structure) work? | Gates grade projection |
| Does `GET .../quizzes/` work? | Gates quiz status; deadlines survive via calendar either way |
| **Do assignment *and* quiz due dates appear in `calendar/events/myEvents/`?** | The fallback for **two** features — if not, both lose their safety net at once |
| Do the `discussions/` routes work? | Gates the discussions corpus (expected yes — students post there) |
| Does `GET .../classlist/` work? | Expected no; determines `get_class_list`'s honest shape |
| Does `.../feedback/...` work for own submissions? | Nice-to-have enrichment |
| What `lp` / `le` versions does the instance report? | Client construction |
| What does an expired session return — 401, 302, or HTML 200? | Expiry detection ([`01`](01-authentication.md)) |
| How long does a session live? | Sets user expectations |
| **Does activity extend the session idle timer?** | Gates whether keepalive ships at all |
| Is the full cookie jar needed, or just the documented pair? | Client construction |
| Does McMaster SSO land on Avenue directly or bounce? | Login success-detection |
| Is a browser-like `User-Agent` required? | Client construction |
| Does an existing D2L MCP server authenticate against Avenue? | Step 0a — validates the whole premise |

### Exit criteria

- [ ] **Step 0a run** — existing D2L MCP server tried against Avenue, result recorded
- [ ] Every route in [`02-api-surface.md`](02-api-surface.md) marked ✅ Verified or ⛔ Blocked — no ⚠️ remaining
- [ ] [`08-api-probe-results.md`](08-api-probe-results.md) filled in with real data
- [ ] [`02-api-surface.md`](02-api-surface.md) status column updated **from** those results
- [ ] [`03-mcp-tools.md`](03-mcp-tools.md) revised where contingent tools turned out degraded
- [ ] Fixtures saved, PII redacted
- [ ] Session lifetime measured, **and keepalive viability decided**

**Doc updates flow probe → docs, never the reverse.** If the probe says the class list is blocked, the tool description changes to match. Adjusting the probe's interpretation to preserve a planned feature is the failure mode to avoid.

---

## Phase 1 — Auth and client

The foundation. Everything else calls this.

### Deliverable

- `auth/session.py` — `SessionManager`: login, persist, load, liveness, XSRF
- `auth/login.py` — Playwright flow, success-signal based, not selector based
- `auth/base.py` — `AuthProvider` protocol; `auth/oauth.py` documented stub
- `client/d2l.py` — `D2LClient`: version negotiation, bookmark paging, throttling, retries, error mapping
- `client/models.py` — pydantic models for probe-confirmed response shapes
- `errors.py` — the taxonomy from [`05`](05-architecture.md)
- `__main__.py` — `avenue-mcp login` works end to end

### Exit criteria

- [ ] `avenue-mcp login` completes a real MacID + MFA sign-in and writes `session.json` at `0600`
- [ ] A second process loads that session and calls `whoami` successfully — **no browser**
- [ ] `myenrollments` returns real courses, pages correctly past page one
- [ ] API version negotiated from `/d2l/api/versions/`, not hardcoded anywhere
- [ ] Expired session produces `SessionExpiredError`, not a JSON parse failure
- [ ] A `403` on a healthy session produces `PermissionDeniedError`, not `SessionExpiredError`
- [ ] Throttle enforces concurrency and interval; verified under a burst
- [ ] Client unit tests pass against Phase 0 fixtures

The second criterion is the important one: it proves the browser is a login mechanism and not a runtime dependency.

---

## Phase 2 — Read-only tools

The server becomes useful.

### Deliverable

The live-data tools from [`03-mcp-tools.md`](03-mcp-tools.md) — everything except the RAG pair and `get_page_image`:

`get_status` · `list_courses` · `get_course_content` · `read_content_file` · `list_assignments` · `list_quizzes` · `get_upcoming_deadlines` · `get_grades` · `analyze_grade_summary` · `list_announcements` · `list_discussions` · `read_discussion_thread` · `get_whats_new` · `get_class_list`

Plus `server.py` registration, `rag/watermarks.py` (needed by `get_whats_new`), and `util/` helpers (HTML→text, timezone).

### Order

1. **`get_status`** — first, deliberately. It's trivial, needs no session, and makes every later phase easier to debug.
2. `list_courses` — every other tool needs an `org_unit_id`
3. `list_announcements` — simplest network tool; proves the pattern
4. `get_course_content` + `read_content_file` — also unblocks Phase 3
5. `get_grades` → `analyze_grade_summary`
6. `list_assignments` + `list_quizzes` → `get_upcoming_deadlines`
7. `list_discussions` + `read_discussion_thread`
8. `get_whats_new` — last of the data tools; it composes several of the above
9. `get_class_list` — most likely degraded

### Exit criteria

- [ ] All fourteen registered and callable from Claude Code against a live account
- [ ] `get_status` works with **no session at all** and reports that accurately
- [ ] `list_courses` returns current courses only by default; `include_inactive` works
- [ ] `read_content_file` extracts text from a real PDF, DOCX, and PPTX
- [ ] `read_content_file` truncates at the 12k default and returns a usable `next_start_page`
- [ ] `get_upcoming_deadlines` spans courses, includes **quizzes as well as assignments**, sorted
- [ ] **DST fixtures pass** — a deadline at 11:59 PM Eastern never renders as the next day, on either side of a transition
- [ ] `list_quizzes` distinguishes *past due but still open* from *closed*
- [ ] `read_discussion_thread` preserves reply structure and omits author names
- [ ] `get_whats_new` reports changes correctly, and `mark_seen: false` leaves the watermark untouched
- [ ] Calling `get_whats_new` twice in a row returns the same result the second time (watermark not consumed prematurely)
- [ ] `analyze_grade_summary` computes a correct weighted average against a hand-checked course
- [ ] With weights unavailable, it returns `weights_available: false` and **omits the projection** rather than guessing
- [ ] With `AVENUE_MCP_GRADE_SCALE` unset, it never claims a letter grade
- [ ] Degraded tools' descriptions match what they actually return
- [ ] Every tool returns a typed error with an actionable message on failure
- [ ] Manual pass: "what's due in the next two weeks?" and "what did I miss?" both answer correctly

The grade criteria are called out because that's the one place a plausible wrong number does real damage. A fabricated "you need 74% on the final" is worse than an honest refusal — and per [`05-architecture.md`](05-architecture.md), drop-lowest rules and bonus items mean a naive weighted average is wrong in more courses than you'd expect. Hand-check against a real gradebook, not a synthetic one.

---

## Phase 3 — RAG

The feature that makes this more than a data fetcher.

### Deliverable

Per [`04-rag-design.md`](04-rag-design.md): `rag/sync.py`, `extract.py`, `chunk.py`, `embed.py`, `render.py`, `store.py`, plus `search_course_materials`, `sync_course_materials`, and `get_page_image`.

### Order

1. `store.py` — schema (files **and** discussions), migrations, FTS5, vector persistence
2. `extract.py` — PDF first, then PPTX, DOCX, HTML, TXT
3. `chunk.py` — structure-aware splitting for files
4. `embed.py` — fastembed wrapper, model-identity recording
5. `sync.py` — orchestration, diffing, error collection — **files only at first**
6. Retrieval — vector + FTS5 + RRF fusion
7. `search_course_materials` + `sync_course_materials` — **ship and validate on files alone**
8. **Then** discussions: thread walking, question+reply chunking, role/recency ranking
9. `render.py` + `get_page_image` — PDF path first; PPTX (via LibreOffice) after

Steps 7 and 8 are split on purpose. Files-only RAG is independently useful and much easier to debug; folding discussions in from the start makes "why did retrieval get worse?" a two-variable question.

### Evaluation set

Build this **before** tuning anything — ~15 real questions with known answers and known sources:

| Question | Expected source |
|---|---|
| "What's the late penalty in 2C03?" | `2C03_outline_W26.pdf` p.3 |
| "What's the A3 weight?" | outline, grading section |
| "Which lecture covered red-black trees?" | Week 7 deck |
| "Does A3 want the recursive version?" | **discussion thread, instructor reply** |
| "Explain the diagram on slide 14" | **visual — needs `get_page_image`** |
| … | … |

Include several questions answerable **only** from a discussion thread and several **only** from a figure. Those are precisely the cases justifying the two capabilities added here — and if they don't measurably improve retrieval, that is worth discovering rather than assuming.

Retrieval quality is not assessable by vibes. Without this set, "does hybrid beat vector-only?" and "do discussions help?" are unanswerable and every tuning decision is guesswork dressed as engineering.

### Exit criteria

- [ ] `sync_course_materials` indexes a real course end to end and reports accurate counts
- [ ] Re-sync with no changes completes in seconds and re-indexes nothing
- [ ] `force: true` re-indexes everything
- [ ] PDF, DOCX, PPTX, HTML, TXT all extract with correct position info
- [ ] Scanned/image-only PDFs are **detected and reported**, not silently indexed empty
- [ ] Every result carries a citation with course, file, and page/slide
- [ ] Hybrid retrieval beats vector-only on the eval set — **measured**, not assumed
- [ ] Course scoping is a hard filter: a scoped query **never** returns another course's content
- [ ] Empty results include `indexed_courses` so "not indexed" is distinguishable from "not found"
- [ ] Changing `AVENUE_MCP_EMBED_MODEL` triggers `EmbedModelMismatchError`, not a silently mixed index
- [ ] **`search_course_materials` works with no session** — verified by deleting `session.json` and searching
- [ ] Discussion chunks retrieve for the discussion-only eval questions
- [ ] Discussion citations carry `author_role` and `posted_at`, and **no author names**
- [ ] Instructor replies outrank student speculation on at least one eval question where both match
- [ ] Discussion sync is incremental — re-syncing a thread fetches only new posts
- [ ] `get_page_image` renders a real PDF page legibly at default DPI
- [ ] PPTX rendering works, or fails with a clear "install LibreOffice" message — not obscurely
- [ ] Manual pass: "what's the late policy in [course]?" cites correctly; "explain the diagram on slide N" shows the actual figure

---

## Phase 4 — Polish

Make it something someone else can actually run.

### Deliverable

- Response cache (LRU + TTL) per [`05`](05-architecture.md); grades, submission status, and computed digests excluded
- **Session keepalive** — implemented per [`01`](01-authentication.md), shipped default-off, enabled only if Phase 0 showed sessions extend on activity
- Progress reporting for long syncs
- Error message pass — every user-facing string has a next action
- Logging: structured, level-controlled, **credential-redacted**
- Setup docs: install, Playwright browser install, LibreOffice note for PPTX rendering, first login, MCP client config
- `.gitignore` covering `.avenue-mcp/`, `*.session.json`, `storage_state.json`, fixtures with PII
- `pyproject.toml` complete with entry points

### Exit criteria

- [ ] A fresh clone reaches working tools by following the README alone, with no undocumented steps
- [ ] Cache measurably reduces requests on repeated `get_upcoming_deadlines` calls
- [ ] Grades are **not** cached — verified by regrading and re-querying
- [ ] `get_whats_new` results are not cached — two calls around a watermark advance behave correctly
- [ ] Keepalive starts only after a first tool call and stops on idle — verified by watching request logs
- [ ] With `AVENUE_MCP_KEEPALIVE_MINUTES=0`, **zero** background requests are made
- [ ] Logs contain no cookies, tokens, or session values — grep-verified
- [ ] Long sync reports progress rather than appearing hung
- [ ] Every error message names a concrete next action
- [ ] `git status` is clean after a full login + sync cycle — no stray state files

Two real checks, not formalities. The `git status` one because a session file landing inside the repo is the most plausible way this project leaks a credential. The zero-background-requests one because that claim appears in [`07-risks-and-policy.md`](07-risks-and-policy.md) as a statement about behavior, and a documented property that nobody verified is just a hope.

---

## Phase 5 — Writes (deferred)

**Not part of this build.** Specced in [`03-mcp-tools.md`](03-mcp-tools.md) and [`07-risks-and-policy.md`](07-risks-and-policy.md) so the design exists, gated off so nothing ships by accident.

Prerequisites before this is even considered:

- [ ] Phases 0–4 complete and stable in daily use
- [ ] Read path proven over a full term
- [ ] `POST .../mysubmissions/` verified against a **throwaway test assignment**, never real coursework
- [ ] Dry-run preview implemented and reviewed first
- [ ] Audit log implemented and verified
- [ ] The user makes a deliberate decision to enable it

Reason for the caution, stated plainly: **a submission cannot be undone.** Brightspace keeps submission history. An accidental submission of the wrong file, or of an unfinished draft, is visible to the instructor permanently. The read path has no comparable failure mode — that asymmetry is the whole justification for the gate.

---

## Dependency graph

```
Step 0a (20 min) ──▶ Phase 0 ──▶ Phase 1 ──▶ Phase 2 ──┬──▶ Phase 4 ──▶ Phase 5
                                                        │
                       Phase 2 (content tools) ──▶ Phase 3 ──┘
```

Phase 3 needs `read_content_file` from Phase 2 but not the whole phase — content tools can be built early to unblock RAG work in parallel.

Phase 0 blocks everything, and Step 0a blocks Phase 0 for twenty minutes at effectively zero cost. Between them they determine whether the auth premise holds at all and whether two headline features are buildable as designed.

## What "done" means for v1

A student can open Claude Code and ask:

| Question | Expected behavior |
|---|---|
| "What's due in the next two weeks?" | Correct, cross-course, **assignments and quizzes**, correct Eastern times |
| "What did I miss?" | New announcements, files, grades, and discussion replies since last check |
| "What's the late penalty in 2C03?" | Correct, cited to file and page |
| "Does A3 want the recursive version?" | Found in the discussion thread, attributed to the instructor and dated |
| "How am I doing in MATH 2Z03?" | Real grades; honest about what it can't compute |
| "Explain the diagram on slide 14" | The actual rendered slide, not just the words on it |
| "Is everything set up right?" | `get_status` — session state, what's indexed, what's enabled |

With one daily login (fewer if keepalive proves viable), no API keys, no coursework leaving the machine, working semantic search even when the session has expired, and no ability to submit anything by accident.
