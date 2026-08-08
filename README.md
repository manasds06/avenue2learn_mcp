# avenue2learn_mcp

An MCP server that gives an AI assistant access to your **D2L Brightspace** courses.

Two universities are supported and measured: **McMaster** (which brands its instance
*Avenue to Learn*) and **Carleton** (which just calls it *Brightspace*). The client and
tool layers are institution-agnostic — a school is a profile in
[`institutions.py`](src/avenue_mcp/institutions.py), not a code path.

Ask your assistant what's due this week, what the marking scheme actually says, or what you
need on the final — and get answers grounded in your real course data instead of guesses.

> **Status: implemented and probed against two live instances.**
> All 17 read tools are built, the server runs over stdio, **358 tests pass**, and the RAG
> pipeline is verified end-to-end with real local embeddings.
>
> **Phase 0's authenticated probe has run on both instances**, on real student accounts.
> What each route actually permits is recorded per institution in
> [`docs/02-api-surface.md`](docs/02-api-surface.md) and enforced in code by
> `institutions.py`; the raw findings are in
> [`08`](docs/08-api-probe-results.md) (McMaster) and
> [`09`](docs/09-carleton-probe-results.md) (Carleton). Phase 0 is not formally *closed* —
> its exit criteria demand every route be ✅ or ⛔, and two are still ⬜ unverified. See
> [Next step](#next-step).
>
> **The most useful thing we learned came from adding the second school.** Four bugs
> surfaced, and every one was the same kind: a *response-shape* assumption that held at
> McMaster, broke elsewhere, and broke **silently** — no error, just a wrong answer. Not
> permissions, which differ loudly. Shape. If you point this at a third instance, expect
> the same class of problem, and see
> [Next step](#next-step). Start at [`docs/00-overview.md`](docs/00-overview.md).
>
> **A fifth bug had a different lesson: the tests agreed with it.** `list_courses` read
> `IsActive`/`StartDate`/`EndDate` off `OrgUnit`, but Valence puts them under `Access`, a
> sibling — so every enrollment looked current and the term filter did nothing (43 courses
> back to Fall 2024). It broke on *both* instances, so the second school never exposed it,
> and the mock nested the fields the same wrong way, so a green suite proved nothing. What
> caught it was reading real tool output. A fixture is a hypothesis about the API; when it
> is copied from the code's assumptions rather than the vendor's schema, it tests only
> self-consistency.

---

## What it does

| | |
|---|---|
| 📚 **Courses** | List your enrollments and browse each course's Content tree |
| 📅 **Deadlines** | Cross-course view of what's due — assignments *and* quizzes, in correct local time |
| 📝 **Assignments** | Due dates, instructions, point values, submission status |
| 📊 **Grades** | Your scores, plus "what do I need on the final for an A-?" |
| 📢 **Announcements** | Recent posts from your instructors |
| 💬 **Discussions** | Forum threads — often the only place an instructor's clarification exists |
| 🆕 **What's new** | Everything posted since you last checked, across every course |
| 🔍 **Semantic search** | Over outlines, slides, specs, **and forum threads** — always **with citations** |
| 🖼️ **Page rendering** | Render a slide or PDF page as an image, so diagrams can actually be seen |
| 🩺 **Diagnostics** | `get_status` — session state, what's indexed, what's enabled, what your instance permits |

Example questions it's built to answer:

- *"What's due in the next two weeks?"*
- *"What did I miss this week?"*
- *"What's the late penalty in my systems course?"* → answered from the actual outline, cited to file and page
- *"Does A3 want the recursive version?"* → found in a forum thread, attributed to your instructor by role, and dated
- *"How am I doing in linear algebra, and what do I need on the final?"*
- *"Explain the diagram on slide 14 of the Week 8 deck"* → renders the actual slide

Two caveats measured rather than assumed, because they change what the answers can say:
**submission status is not readable** on either instance (the documented learner route
403s), and the **calendar returns no events** on either, so deadlines come from the
assignment and quiz routes instead.

## How it works

Four design decisions worth knowing up front:

**Authentication is a real browser login.** D2L's official API path (Valence OAuth)
requires a Brightspace administrator to register an application — students can't. So
instead you log in once through a real browser with your own credentials and MFA, and the
server reuses that session to call the same REST API Brightspace's own frontend uses. You
get exactly your own permissions, nothing more. Sessions last **~6.8 hours** (measured, not
assumed), after which you log in again. → [`docs/01-authentication.md`](docs/01-authentication.md)

Worth knowing what *doesn't* work: you cannot register your own Microsoft Entra app and
exchange a university token for Brightspace access. Entra tokens are scoped to the app that
requested them, and Brightspace won't accept one. The browser has to complete the handshake.

**The server contains no AI model.** It fetches, parses, and returns structured data. All
the reasoning happens in your MCP client. No API keys, works with any client, and a tool
call is a network fetch rather than an inference.

**Course material never leaves your machine.** The search index is built with local
embeddings and stored in local SQLite. Your coursework isn't uploaded anywhere.
→ [`docs/04-rag-design.md`](docs/04-rag-design.md)

**Local search survives an expired session.** The index is on disk, so
`search_course_materials` keeps working when your cookies die. Only the live-data tools
degrade.

```
Claude Code / Desktop
        │  MCP over stdio
   ┌────▼─────────────────────────────┐
   │  avenue-mcp  (Python + FastMCP)  │
   │  tools · client · auth · rag     │
   │  ▲ institution profile selects   │
   │    host, SSO entry, timezone     │
   └────┬───────────────────┬─────────┘
        │ session cookies   │ local index
        ▼                   ▼
  your school's        ~/.avenue-mcp/<institution>/
  Brightspace host       session.json · index.db
```

State is namespaced per institution, so two schools never share a session or an index.

## Read-only by default

Every tool in v1 is read-only. The server **cannot** submit an assignment — not
"shouldn't", cannot.

Submission is designed but deferred: gated behind an explicit environment flag, requiring a
dry-run preview and a deliberate confirmation. When the flag is off, the write tool isn't
registered at all, so the model never sees it.

The reason is simple: **a submission cannot be undone.** Every read operation can be
retried at zero cost; submitting the wrong file is permanent and visible to your
instructor. → [`docs/07-risks-and-policy.md`](docs/07-risks-and-policy.md)

There is exactly one write operation ever contemplated — submitting your own file to your
own assignment, with an optional comment attached. Deliberately absent, permanently:
posting to discussions, retrieving quiz questions, anything instructor-side.

One thing available but withheld: the class list is **readable** on both instances,
returning full names and emails. `get_class_list` does not return the student roster
anyway, on privacy grounds — available is not the same as appropriate. The roster is read
internally for one purpose only, mapping a user id to a *role*, so a forum post can be
attributed to "Instructor" without a name.

## Documentation

| Doc | Contents |
|---|---|
| [`00-overview.md`](docs/00-overview.md) | **Start here.** Goals, glossary, architecture |
| [`01-authentication.md`](docs/01-authentication.md) | How we authenticate, and why the obvious way is closed |
| [`02-api-surface.md`](docs/02-api-surface.md) | Every Valence route, with **measured status per institution** |
| [`03-mcp-tools.md`](docs/03-mcp-tools.md) | The tool catalog |
| [`04-rag-design.md`](docs/04-rag-design.md) | Course-file search, end to end |
| [`05-architecture.md`](docs/05-architecture.md) | Modules, config, errors, caching |
| [`06-roadmap.md`](docs/06-roadmap.md) | Phases and exit criteria |
| [`07-risks-and-policy.md`](docs/07-risks-and-policy.md) | Academic integrity, ToS, data handling |
| [`08-api-probe-results.md`](docs/08-api-probe-results.md) | Phase 0 findings — **McMaster** |
| [`09-carleton-probe-results.md`](docs/09-carleton-probe-results.md) | Phase 0 findings — **Carleton**, and the five silent bugs it exposed |

Kept as two probe documents on purpose. One file holding two instances' results is exactly
where "measured at McMaster" leaks into a Carleton conclusion.

## Setup

```bash
git clone <this repo> && cd avenue2learn_mcp
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/playwright install chromium     # login only
```

On Windows the venv puts executables in `.venv\Scripts\` rather than `.venv/bin/`;
substitute throughout.

**Pick your school.** One process serves one institution:

```bash
export AVENUE_MCP_INSTITUTION=mcmaster    # or: carleton
```

Sign in once — this opens a real browser for your university credentials and 2FA:

```bash
.venv/bin/avenue-mcp login
.venv/bin/avenue-mcp status                # confirm it worked
```

`status` prints which institution and host it's talking to, so a misconfigured profile
shows up immediately rather than as a confusing 404.

Register with your MCP client:

```json
{
  "mcpServers": {
    "brightspace": {
      "command": "/absolute/path/to/avenue2learn_mcp/.venv/bin/avenue-mcp",
      "args": ["serve"],
      "env": { "AVENUE_MCP_INSTITUTION": "mcmaster" }
    }
  }
}
```

Enrolled at two schools? Add **two entries** with different names and different
`AVENUE_MCP_INSTITUTION` values. Their sessions and indexes are already separate on disk.

For a school not in the registry, set `AVENUE_MCP_BASE_URL` and `AVENUE_MCP_LOGIN_URL`
directly. It will run unbranded and report every route as unverified — deliberately, since
inheriting another school's measured capabilities is precisely the lie the capability system
exists to prevent.

Then ask your assistant to `sync_course_materials` for a course, and search becomes
available. First sync downloads the embedding model (~130 MB) once.

Optional, for rendering PowerPoint slides as images: install **LibreOffice**
(`sudo apt install libreoffice-impress`, `brew install --cask libreoffice`, or
`winget install TheDocumentFoundation.LibreOffice`). The server names the right one for
your platform if you skip it. PDF rendering needs nothing extra.

### CLI

| Command | Purpose |
|---|---|
| `avenue-mcp login` | Browser sign-in; persists the session |
| `avenue-mcp status` | Institution, session, and index state (`--offline` skips the network) |
| `avenue-mcp serve` | Run the MCP server over stdio |
| `avenue-mcp logout` | Delete the local session file |
| `avenue-mcp reindex --course ID` | Clear a course's index so it can be rebuilt (`--all` for everything) |

### Verifying

```bash
.venv/bin/python -m pytest -q                   # 358 tests, no network
.venv/bin/python scripts/probe_unauth.py        # live probe, no login needed
.venv/bin/python scripts/smoke_login_chain.py   # login flow up to the password box
.venv/bin/python scripts/smoke_server.py        # server over real stdio
.venv/bin/python scripts/smoke_rag.py           # RAG round-trip, real embeddings
.venv/bin/python scripts/probe.py --course ID   # Phase 0 proper — needs login
```

The suite includes a mock Brightspace ([`tests/test_integration_tools.py`](tests/test_integration_tools.py))
with two independent axes:

- **Permissions** — every tool run in a world where all routes answer, and one where the
  instructor-scope routes return `403`, so the degraded paths are exercised rather than assumed.
- **Response shape** — the same data served McMaster-shaped and Carleton-shaped: `Url`
  present or absent on content topics, a role field present or absent on discussion posts.

The shape axis exists because a McMaster-only mock could not see any of the four bugs the
second school exposed. It is the structural version of that fix, and it demonstrably works:
neutering the three shape fixes turns four of its assertions red on the Carleton shape and
none on McMaster.

## Stack

Python 3.11+ · MCP ≥2.0 (`MCPServer`) · httpx · Playwright (login only) · SQLite + FTS5 ·
fastembed · pymupdf / python-docx / python-pptx · LibreOffice (optional, slide rendering)

Deliberately absent: any LLM SDK.

## Next step

The probe has run on both instances, so what's left is narrower and specific.

**Close Phase 0 properly.** Its exit criteria in
[`docs/06-roadmap.md`](docs/06-roadmap.md) require every route to be ✅ or ⛔ with no
unknowns, and that is not yet true. Two remain ⬜ — unverified, *not* denied, and the
distinction matters:

- McMaster's `discussions/…/topics/` and `…/posts/` — the probed course's forums were
  empty, so nothing below the forum list was ever reached there. Carleton's ✅ must not be
  read across. Re-probe on a McMaster course with live threads.
- `dropbox/…/feedback/…` — unreached on both.

The one exit criterion that will not be met as written is **Step 0a**, which proposed
validating the cookie-session premise by pointing an existing third-party D2L MCP server at
Avenue. It was never run, and it has been superseded: our own authenticated probe answered
the same question directly, and cookies work. Recorded here rather than quietly ticked.

**Probe a third school for *shape*, not just access.** Four of the five bugs found so far
were shape, all silent, and all invisible to the permission axis. A new instance should be
checked for what its payloads *look like* — does the content listing carry `Url`, do posts
carry a role — as a first-class step, not a follow-up. Its measured results then go into
`institutions.py`, and never anywhere else.

**Check fixtures against the vendor schema, not the code.** The fifth bug was neither
permissions nor per-instance shape: `list_courses` read three fields off the wrong object,
and the mock made the same mistake, so a green suite proved only self-consistency. Any
fixture field worth asserting on is worth confirming against
[`docs.valence.desire2learn.com`](https://docs.valence.desire2learn.com/) or a captured
response. Reading real tool output is the cheapest way to find the ones already wrong.

**Known cosmetic issue, deliberately unfixed.** `sync_course_materials` reports quiz-launcher
URLs as "unsupported file type", which is misleading — they aren't files at all. The counts
are correct and every real file is indexed; only the label is wrong. Documented in
[`09`](docs/09-carleton-probe-results.md) rather than silently expanded into scope.

Then v2: assignment submission, still behind its flag. Roadmap in
[`docs/06-roadmap.md`](docs/06-roadmap.md).

## Before you use this

This is a **study aid**, not a homework machine. It helps you find, understand, and
organize material you already have access to.

- Your instructor's AI policy governs. Check your course outline.
- Personal use only — your account, your machine. Don't run it for others or host it.
- This is not a sanctioned integration at any school. Asking your university's IT
  department about official API access is the clean long-term fix.
- Course materials are instructor intellectual property. Don't redistribute them.
- Your classmates are in that roster and those forum threads. The server withholds names
  by design; don't route around it.

Read [`docs/07-risks-and-policy.md`](docs/07-risks-and-policy.md) before enabling anything
that writes.

## Not affiliated with

McMaster University, Carleton University, or D2L. "Avenue to Learn", "Brightspace", and
"D2L" are their respective owners' marks.
