# Handoff

Everything that changed since `f361c90` ("Initial buildplan of agent architecture"), written for someone picking this up cold.

At `f361c90` this repo was **design docs only** — no code. It now has a working MCP server: ~7,100 lines of source, ~2,700 lines of tests (276 passing), and ~900 lines of scripts. The docs were also revised in place from what the implementation actually learned.

---

## TL;DR for your first hour

```bash
git clone <repo> && cd avenue2learn_mcp
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/playwright install chromium

.venv/bin/python -m pytest -q          # 276 pass, no network, no credentials
.venv/bin/python scripts/probe_unauth.py   # hits the real Avenue, no login needed
```

Then read [`docs/00-overview.md`](docs/00-overview.md) → [`docs/01-authentication.md`](docs/01-authentication.md). The auth doc is the load-bearing one; if you only read one, read that.

**The single most useful thing you can do next** is the one thing I could not: run `avenue-mcp login` and then `scripts/probe.py`. See [Where to pick up](#where-to-pick-up).

---

## What's new since the buildplan commit

### New: the entire implementation

| Path | What it is | Lines |
|---|---|---|
| `src/avenue_mcp/config.py` | Env-driven settings (`AVENUE_MCP_*`) | ~150 |
| `src/avenue_mcp/errors.py` | Error taxonomy; every error carries a `next_step` hint | ~145 |
| `src/avenue_mcp/context.py` | `AppContext` — owns session, client, store, embedder | ~180 |
| `src/avenue_mcp/server.py` | The 17 MCP tools + gated write tool | ~510 |
| `src/avenue_mcp/__main__.py` | CLI: `serve` / `login` / `status` / `logout` / `reindex` | ~165 |
| `src/avenue_mcp/auth/` | Cookie-session auth, Playwright login, OAuth stub | ~430 |
| `src/avenue_mcp/client/` | Valence REST client + response normalization | ~600 |
| `src/avenue_mcp/rag/` | extract · chunk · embed · retrieve · sync · store · render | ~2,000 |
| `src/avenue_mcp/tools/` | One module per tool group | ~2,200 |
| `src/avenue_mcp/util/` | dates (DST-safe) · html · throttle | ~430 |
| `tests/` | 276 tests across 10 files | ~2,650 |
| `scripts/` | probe + smoke harnesses | ~900 |

### Changed: the docs

1,368 lines added / 152 removed across all nine docs. They were **corrected from live findings**, not just expanded — see below.

---

## Three things the docs got wrong (now fixed)

I probed the real Avenue host without credentials. Three assumptions in the buildplan were wrong, and each would have broken the server.

**1. The base URL was wrong.** `avenue.mcmaster.ca` is a *static Apache landing page* — every `/d2l/*` path 404s there. Real Brightspace is **`avenue.cllmcmaster.ca`** (its own "Browser Check" link gave it away). Live versions: `lp 1.62`, `le 1.96`.

> Left alone, **every tool would have failed with `NotFoundError` on first use.**

**2. The liveness probe couldn't detect a dead session.** `GET /d2l/lp/auth/xsrf-tokens` returns `200` + JSON **with no session at all**. It was the liveness check, so `is_alive()` would report expired sessions as alive. Now probes `users/whoami` and requires 200 *and* a JSON object.

**3. `403` needed splitting.** Anonymous API calls return `403` + **HTML** (the sign-in wall). All `403`s mapped to `PermissionDeniedError`, whose message says *"this is not a login problem, signing in again will not help"* — exactly backwards for a logged-out user. Now: `403`+HTML → `SessionExpiredError`, `403`+JSON → `PermissionDeniedError`.

Also confirmed: SSO is **SAML 2.0** to Microsoft Entra, tenant `44376307-b429-42ad-8c25-28cd496f4772`, across three hosts. Login starts on the landing page and *ends* on the Brightspace host — waiting on the wrong one is why a naive login flow appears to hang after a successful sign-in.

All recorded in [`docs/08-api-probe-results.md`](docs/08-api-probe-results.md), reproducible via `scripts/probe_unauth.py`.

---

## Bugs found and fixed in code review

Two reviewers ran over the finished code. Ten real defects; these are the ones worth knowing about because the fixes look odd out of context.

| Severity | Bug | Why the fix looks the way it does |
|---|---|---|
| **HIGH** | **Arbitrary file write.** `guess_filename()` returned a topic's `Title` verbatim, used as a path component. A topic titled `../../../../.bashrc` writes server-supplied bytes to a chosen path. | `models.safe_filename()` reduces to one component; `D2LClient.download()` additionally refuses to resolve outside the cache root. Don't remove either — they're independent layers. |
| **HIGH** | **Download errors became `ResponseNotRead`.** `_raise_for_status` touched `resp.text` inside a streaming response. | `download()` calls `await resp.aread()` before raising. Looks redundant; isn't. |
| **HIGH** | **`get_whats_new` lost changes permanently.** `SessionExpiredError` is an `AuthError`, not an `APIError`, so `except APIError` missed it; `gather(return_exceptions=True)` discarded it; watermarks advanced anyway. | A watermark now advances **only** for a category that succeeded. Response carries `complete` / `failures`. |
| **MED-HIGH** | **`mark_seen=False` wasn't a peek** — `_grades` wrote per-item watermarks unconditionally. | `mark_seen` is threaded into `_grades`. |
| **MED** | **A quiz sharing a deadline with an assignment vanished** from the deadline view — matched on due-date alone, inherited "submitted", got filtered out. | Title must match; timestamp is only a tie-breaker, and `_different_kind()` blocks quiz↔assignment conflation. |
| **MED** | **Submission status was TTL-cached** — submit, then be told "not_submitted" for 5 minutes. | `cache=False` on `mysubmissions`, and `clear_cache()` after any write. |
| **MED** | **Failed extraction made a course look indexed forever** — document row written before the quality check, so search returned `[]` and the model reported "not in the materials". | The `ExtractionError` is raised **before** `upsert_document`. Order matters. |
| **MED** | **Grade projection silently wrong** — items graded-but-unscored belonged to neither bucket, shrinking the denominator. | Tracked as `unaccounted_weight`; the projection refuses unless the buckets reconcile. |
| **MED** | **`get_class_list` classified everyone "Unknown"** — tried `RoleId` (an integer) first, so instructors landed in the students array. | Name-bearing keys first, `RoleId` last. |
| **MED-LOW** | **Throttle deadlock** — a cancellation during the pacing sleep leaked the semaphore permit; after 4 the client hung forever. | `try/except BaseException: release; raise` around the post-acquire block. |

Plus: session file written world-readable before `chmod` (now `os.open` with `0o600`), render cache keyed on `PYTHONHASHSEED`-randomized `hash()` (now SHA-256), and a non-JSON 200 caching an empty list for the TTL.

All pinned by `tests/test_review_fixes.py` and `tests/test_live_findings.py`.

---

## Design decisions — please don't undo these by accident

Each exists for a specific reason and looks arbitrary otherwise.

**The server contains no LLM.** No `anthropic` dependency, deliberately. Tools return structured data; the MCP client does the reasoning. Adding an LLM SDK here means the logic belongs in the client instead.

**`search_course_materials` and `get_status` must NOT call `require_session()`.** That's what makes local search survive an expired session. It's an easy line to add "for consistency" and doing so silently destroys the property. Pinned by `tests/test_offline.py`.

**Write tools are not registered when the flag is off** — absent from the tool list, not present-and-erroring. A model can't call, be talked into, or hallucinate using a tool it can't see.

**Discussion posts store author *role*, never author names.** Roles carry the authority signal; names would make the index a durable record of classmates' opinions. See [`docs/07-risks-and-policy.md`](docs/07-risks-and-policy.md).

**Grade math refuses rather than guesses.** Brightspace exposes gradebook *numbers* but not *rules* (drop-lowest, bonus items, nested category weights), so a naive weighted average is wrong in real courses. Letter grades are never claimed unless `AVENUE_MCP_GRADE_SCALE` is set — cutoffs vary by faculty.

**Retrieval adjustments are penalties only, never above 1.0.** An earlier version boosted instructor posts ×1.25; RRF scores are tiny and tightly packed (`1/(60+rank)`), so that reliably flipped rank 1 and 2 and pushed forum threads above the course outline. Instructor/TA sit at parity with files; only student speculation is demoted.

**Chunking splits on every structural boundary above the orphan floor.** Accumulating to a token target turned a 4-page outline into one chunk cited `p.1–p.4` — useless for the one question the pipeline exists to answer.

---

## Testing — and one trap to know about

```bash
.venv/bin/python -m pytest -q                   # 276, no network
.venv/bin/python scripts/probe_unauth.py        # live, no login
.venv/bin/python scripts/smoke_login_chain.py   # browser → SAML → MacID form
.venv/bin/python scripts/smoke_server.py        # server over real stdio
.venv/bin/python scripts/smoke_rag.py           # RAG with real embeddings
```

`tests/test_integration_tools.py` runs **every tool twice** — once with all routes permitted, once with the instructor-scope routes returning `403` — so the degraded paths are exercised rather than assumed.

> ### ⚠️ Never hardcode a date in a fixture
>
> This bit twice. Fixtures pinned to `2026-03-15` fell outside the lookback window once that date passed, so `get_whats_new` returned zero changes and its tests asserted `0 == 0` — green no matter how broken the digest was. **Both watermark bugs above lived in code that suite nominally covered.**
>
> Use `_future_deadline()` / `RECENT_UTC` in `tests/test_integration_tools.py`, which are computed from `now`.

---

## Where to pick up

### 1. Log in and run the real probe — the actual blocker

I could not do this: it needs a MacID password and a 2FA approval.

```bash
.venv/bin/avenue-mcp login     # browser opens; MacID + 2FA
.venv/bin/avenue-mcp status    # expect alive=True
.venv/bin/python scripts/probe.py --course ORG_UNIT_ID
```

This resolves six routes marked ⚠️ in [`docs/02-api-surface.md`](docs/02-api-surface.md) and decides whether three tools ship full or degraded:

| Route | Decides |
|---|---|
| `dropbox/folders/` | Whether `list_assignments` gets points + instructions + status, or only dates |
| `grades/` | Whether `analyze_grade_summary` can project at all |
| `quizzes/` | Quiz attempt status (deadlines survive via the calendar regardless) |
| `discussions/.../posts/` | Whether `ParentPostId` and author **role** exist — both are load-bearing for RAG |
| `classlist/` | Expected `403`; that's the correct outcome, not a bug |

**Write findings into [`docs/08-api-probe-results.md`](docs/08-api-probe-results.md), then update [`docs/02`](docs/02-api-surface.md) *from* them.** Direction matters — probe → docs, never the reverse. Reinterpreting a `403` to preserve a planned feature is the failure mode to avoid.

### 2. Watch for this

If `list_courses` comes back **empty** after a successful login, the likely cause is that `avenue.cllmcmaster.ca` isn't the only Brightspace host at McMaster — it's just the one the landing page points at. Check where your browser actually lands after signing in and set `AVENUE_MCP_BASE_URL` accordingly.

### 3. Then

Phases 4–5 in [`docs/06-roadmap.md`](docs/06-roadmap.md). Phase 5 (assignment submission) is deliberately gated off; read [`docs/07-risks-and-policy.md`](docs/07-risks-and-policy.md) before enabling it — submissions cannot be undone.

Known cleanups the design review flagged that I did **not** do (all non-blocking): extract a `tools/_common.py` for `_calendar_events` / `_grade_values` / `list_courses` to kill the function-body imports between tool modules; a shared `rich_text()` helper (the same unwrap appears 7×); unify the two content-tree walks in `tools/content.py` and `rag/sync.py`; and add cache-size bounding — nothing prunes `~/.avenue-mcp/cache/` today.

---

## Safety notes

- `~/.avenue-mcp/session.json` **is** a logged-in Avenue session. Don't commit, sync, or paste it. `.gitignore` covers it twice.
- Nothing is committed yet — `src/`, `tests/`, `scripts/`, `pyproject.toml`, `.gitignore` are all untracked. Check `git status` before your first commit and make sure no state files sneak in.
- This is not a McMaster-sanctioned integration. Personal use, your own account, your own machine. Asking UTS about Valence API access is the clean long-term fix.
