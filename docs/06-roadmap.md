# 06 — Roadmap

Phases, deliverables, and exit criteria. Each phase ends with something demonstrable — no phase is "refactoring" or "research" alone.

## Summary

| Phase | Deliverable | Rough size |
|---|---|---|
| **0** | Probe — what can a student account actually reach? | Half a day |
| **1** | Auth + HTTP client | 1–2 days |
| **2** | Read-only tools | 2–3 days |
| **3** | RAG | 2–4 days |
| **4** | Polish, caching, setup | 1–2 days |
| **5** | Write tools — deferred, gated | Not scoped |

Sizes assume part-time work by one person and are estimates, not commitments. Phase 3 has the widest range because document extraction always contains one surprise.

---

## Phase 0 — Probe

**Nothing downstream is trustworthy until this runs.**

Two separate unknowns, and the first is bigger than the plan originally treated it:

**The auth mechanism itself is unresolved.** [`01-authentication.md`](01-authentication.md) describes three strategies; the prior art we can inspect uses the one with the worst ergonomics. Whether the browser is needed once a day or once an hour is decided here, and it changes what the README can honestly promise.

**Several routes are marked ⚠️** — unknown for students. Grade projection (`grades/`) hinges on one. Assignments (`dropbox/folders/`) was thought to, but the Instructor-scope claim behind that has been withdrawn ([`02`](02-api-surface.md)) and it's now expected to work.

### Deliverable

A throwaway script — `scripts/probe.py`, not shipped, not polished — that:

1. Runs the Playwright login flow, persists a session.
2. Hits every route in [`02-api-surface.md`](02-api-surface.md) against a real course.
3. Records status code, response shape, and a redacted sample for each.
4. Measures session lifetime (idle and active).
5. Writes findings into [`08-api-probe-results.md`](08-api-probe-results.md).
6. Saves raw responses to `tests/fixtures/` — these become the test corpus for every later phase.

### Questions it must answer

| Question | Why it matters |
|---|---|
| **Which auth strategy works — cookies, minted bearer, or captured bearer?** | **Gates everything.** Decides whether the browser is a one-time login or an hourly runtime dependency ([`01`](01-authentication.md) §B0) |
| Does `GET .../dropbox/folders/` work for a student? | Gates the full assignments feature — though the Instructor-scope claim has been withdrawn, so this is now expected to pass |
| Does `GET .../grades/` (structure) work? | Gates grade projection |
| Does `GET .../classlist/` work? | Expected no; determines `get_class_list`'s honest shape |
| Does `.../feedback/...` work for own submissions? | Nice-to-have enrichment |
| What `lp` / `le` versions does the instance report? | Client construction |
| What does an expired session return — 401, 302, or HTML 200? | Expiry detection ([`01`](01-authentication.md)) |
| How long does a session live? | Sets user expectations |
| Is the full cookie jar needed, or just the documented pair? | Client construction |
| Does McMaster SSO land on Avenue directly or bounce? | Login success-detection |
| Is a browser-like `User-Agent` required? | Client construction |

### Exit criteria

- [ ] **The auth strategy is decided and recorded** ([`08`](08-api-probe-results.md) §B0); the two that lost are deleted from [`01`](01-authentication.md)
- [ ] Every route in [`02-api-surface.md`](02-api-surface.md) marked ✅ Verified or ⛔ Blocked — no ⚠️ remaining
- [ ] [`08-api-probe-results.md`](08-api-probe-results.md) filled in with real data
- [ ] [`02-api-surface.md`](02-api-surface.md) status column updated **from** those results
- [ ] [`03-mcp-tools.md`](03-mcp-tools.md) revised where contingent tools turned out degraded
- [ ] Fixtures saved, PII redacted
- [ ] Session lifetime measured

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

- [ ] `avenue-mcp login` completes a real MacID + MFA sign-in and writes `session.json`
- [ ] **The session file is verifiably owner-only** — `icacls session.json` on Windows, `ls -l` on POSIX. Not "we called `chmod`": `chmod` is a no-op on Windows ([`01`](01-authentication.md)), so this must be checked, not assumed.
- [ ] A second process loads that session and calls `whoami` successfully — **no browser**, *provided Phase 0 found cookie or minted-bearer auth works.* If the probe found only `CapturedBearerAuth` viable, this criterion is retired and the browser-refresh interval is documented instead.
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

The v1 tools from [`03-mcp-tools.md`](03-mcp-tools.md), minus the two RAG ones:

`list_courses` · `get_course_content` · `read_content_file` · `list_assignments` · `get_upcoming_deadlines` · `get_grades` · `analyze_grade_summary` · `list_announcements` · `get_class_list`

Plus `server.py` registration and `util/` helpers (HTML→text, timezone).

### Order

1. `list_courses` — every other tool needs an `org_unit_id`
2. `list_announcements` — simplest end-to-end; proves the tool pattern
3. `get_course_content` + `read_content_file` — also unblocks Phase 3
4. `get_grades` → `analyze_grade_summary`
5. `list_assignments` → `get_upcoming_deadlines`
6. `get_class_list` — last, most likely degraded

### Exit criteria

- [ ] All nine registered and callable from Claude Code against a live account
- [ ] `list_courses` returns current courses only by default; `include_inactive` works
- [ ] `read_content_file` extracts text from a real PDF, DOCX, and PPTX
- [ ] `get_upcoming_deadlines` spans courses, sorted, **and renders Eastern time correctly** — a deadline at 11:59 PM local does not display as the next day
- [ ] `analyze_grade_summary` computes a correct weighted average against a hand-checked course
- [ ] With weights unavailable, it returns `weights_available: false` and **omits the projection** rather than guessing
- [ ] Degraded tools' descriptions match what they actually return
- [ ] Every tool returns a typed error with an actionable message on failure
- [ ] Manual pass: ask Claude "what's due in the next two weeks?" and get a correct answer

The weights criterion is called out because it's the one place a plausible wrong number does real damage. A fabricated "you need 74% on the final" is worse than an honest refusal.

---

## Phase 3 — RAG

The feature that makes this more than a data fetcher.

### Deliverable

Per [`04-rag-design.md`](04-rag-design.md): `rag/sync.py`, `extract.py`, `chunk.py`, `embed.py`, `store.py`, plus the `search_course_materials` and `sync_course_materials` tools.

### Order

1. `store.py` — schema, migrations, FTS5, vector persistence
2. `extract.py` — PDF first, then PPTX, DOCX, HTML, TXT
3. `chunk.py` — structure-aware splitting
4. `embed.py` — fastembed wrapper, model-identity recording
5. `sync.py` — orchestration, diffing, error collection
6. Retrieval — vector + FTS5 + RRF fusion
7. Both tools

### Evaluation set

Before tuning anything, build a small ground-truth set — ~15 real questions with known answers and known source pages:

| Question | Expected source |
|---|---|
| "What's the late penalty in 2C03?" | `2C03_outline_W26.pdf` p.3 |
| "What's the A3 weight?" | outline, grading section |
| "Which lecture covered red-black trees?" | Week 7 deck |
| … | … |

Retrieval quality is not assessable by vibes. Without this set, "does hybrid search beat pure vector?" is unanswerable and every tuning decision is guesswork.

### Exit criteria

- [ ] `sync_course_materials` indexes a real course end to end and reports accurate counts
- [ ] Re-sync with no changes completes in seconds and re-indexes nothing
- [ ] `force: true` re-indexes everything
- [ ] PDF, DOCX, PPTX, HTML, TXT all extract with correct position info
- [ ] Scanned/image-only PDFs are **detected and reported**, not silently indexed empty
- [ ] Every result carries a citation with course, file, and page/slide
- [ ] Hybrid retrieval beats vector-only on the eval set — measured, not assumed
- [ ] Course scoping is a hard filter: a scoped query **never** returns another course's content
- [ ] Empty results include `indexed_courses` so "not indexed" is distinguishable from "not found"
- [ ] Changing `AVENUE_MCP_EMBED_MODEL` triggers `EmbedModelMismatchError`, not a silently mixed index
- [ ] Manual pass: "what's the late policy in [course]?" answers correctly with a citation

---

## Phase 4 — Polish

Make it something someone else can actually run.

### Deliverable

- Response cache (LRU + TTL) per [`05`](05-architecture.md); grades and submission status excluded
- Progress reporting for long syncs
- Error message pass — every user-facing string has a next action
- Logging: structured, level-controlled, **credential-redacted**
- Setup docs: install, Playwright browser install, first login, MCP client config
- `.gitignore` covering `.avenue-mcp/`, `*.session.json`, `storage_state.json`, fixtures with PII
- `pyproject.toml` complete with entry points

### Exit criteria

- [ ] A fresh clone reaches working tools by following the README alone, with no undocumented steps
- [ ] Cache measurably reduces requests on repeated `get_upcoming_deadlines` calls
- [ ] Grades are **not** cached — verified by regrading and re-querying
- [ ] Logs contain no cookies, tokens, or session values — grep-verified
- [ ] Long sync reports progress rather than appearing hung
- [ ] Every error message names a concrete next action
- [ ] `git status` is clean after a full login + sync cycle — no stray state files

The last one is a real check, not a formality. A session file accidentally landing inside the repo is the most plausible way this project leaks a credential.

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
Phase 0 ──▶ Phase 1 ──▶ Phase 2 ──┬──▶ Phase 4 ──▶ Phase 5
                                   │
                    Phase 2 (content tools) ──▶ Phase 3 ──┘
```

Phase 3 needs `read_content_file` from Phase 2 but not the whole phase — content tools can be built early to unblock RAG work in parallel.

Phase 0 blocks everything. It's half a day of work that determines whether two of the headline features are buildable as designed.

## What "done" means for v1

A student can open Claude Code and ask:

- "What's due in the next two weeks?" → correct, cross-course, correct local times
- "What's the late penalty in 2C03?" → correct, cited to a file and page
- "How am I doing in MATH 2Z03?" → real grades, honest about what it can't compute
- "What did my prof announce this week?" → actual announcements
- "Explain slide 14 of the Week 8 deck" → real slide content

with one daily login, no API keys, no coursework leaving the machine, and no ability to submit anything by accident.
