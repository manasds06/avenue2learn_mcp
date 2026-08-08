# avenue2learn_mcp

An MCP server that gives an AI assistant access to **Avenue to Learn**, McMaster University's D2L Brightspace instance.

Ask your assistant what's due this week, what the marking scheme actually says, or what you need on the final — and get answers grounded in your real course data instead of guesses.

> **Status: server implemented; unauthenticated half of Phase 0 done.**
> All 17 read tools are built, the server runs over stdio, **196 tests pass**, and
> the RAG pipeline is verified end-to-end with real local embeddings.
>
> Probing Avenue without credentials already corrected three wrong assumptions —
> including that **Brightspace lives at `avenue.cllmcmaster.ca`**, not
> `avenue.mcmaster.ca` (a static landing page where every `/d2l/*` path 404s).
> Details in [`docs/08-api-probe-results.md`](docs/08-api-probe-results.md).
>
> **Still unvalidated against a logged-in account** — the instructor-scope routes
> in [`docs/02-api-surface.md`](docs/02-api-surface.md) may degrade. Start at
> [`docs/00-overview.md`](docs/00-overview.md).

---

## What it will do

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
| 🩺 **Diagnostics** | `get_status` — session state, what's indexed, what's enabled |

Example questions it's built to answer:

- *"What's due in the next two weeks?"*
- *"What did I miss this week?"*
- *"What's the late penalty in 2C03?"* → answered from the actual outline, cited to file and page
- *"Does A3 want the recursive version?"* → found in a forum thread, attributed to your instructor and dated
- *"How am I doing in MATH 2Z03, and what do I need on the final?"*
- *"Explain the diagram on slide 14 of the Week 8 deck"* → renders the actual slide

## How it works

Three design decisions worth knowing up front:

**Authentication is a real browser login.** D2L's official API path (Valence OAuth) requires a Brightspace administrator to register an application — students can't. So instead you log in once through a real browser with your MacID and MFA, and the server reuses that session to call the same REST API Brightspace's own frontend uses. You get exactly your own permissions, nothing more. → [`docs/01-authentication.md`](docs/01-authentication.md)

Worth knowing what *doesn't* work: you cannot register your own Microsoft Entra app and exchange a MacID token for Avenue access. Entra tokens are scoped to the app that requested them, and Brightspace won't accept one. The browser has to complete the handshake with Avenue — which from your side looks identical anyway.

**The server contains no AI model.** It fetches, parses, and returns structured data. All the reasoning happens in your MCP client. No API keys, works with any client, and a tool call is a network fetch rather than an inference.

**Course material never leaves your machine.** The search index is built with local embeddings and stored in local SQLite. Your coursework isn't uploaded anywhere. → [`docs/04-rag-design.md`](docs/04-rag-design.md)

**Local search survives an expired session.** The index is on disk, so `search_course_materials` keeps working when your Avenue cookies die. Only the live-data tools degrade.

```
Claude Code / Desktop
        │  MCP over stdio
   ┌────▼─────────────────────────────┐
   │  avenue-mcp  (Python + FastMCP)  │
   │  tools · client · auth · rag     │
   └────┬───────────────────┬─────────┘
        │ session cookies   │ local index
        ▼                   ▼
  avenue.mcmaster.ca   ~/.avenue-mcp/
```

## Read-only by default

Every tool in v1 is read-only. The server **cannot** submit an assignment — not "shouldn't", cannot.

Submission is designed but deferred: gated behind an explicit environment flag, requiring a dry-run preview and a deliberate confirmation. When the flag is off, the write tool isn't registered at all, so the model never sees it.

The reason is simple: **a submission cannot be undone.** Every read operation can be retried at zero cost; submitting the wrong file is permanent and visible to your instructor. → [`docs/07-risks-and-policy.md`](docs/07-risks-and-policy.md)

There is exactly one write operation ever contemplated — submitting your own file to your own assignment, with an optional comment attached. Deliberately absent, permanently: posting to discussions, retrieving quiz questions, anything instructor-side.

## Two clients, one set of tools

This repo now holds two implementations of the same 17 tools, for two different
users:

| | **Python MCP server** (`src/`) | **Browser extension** (`extension/`) |
|---|---|---|
| Runs as | An MCP server under Claude Code | A Chrome extension, in your browser |
| Auth | Playwright login → `session.json` | Your existing Brightspace session |
| LLM | Whatever your MCP client uses | Gemini, with your own key |
| For | You | Someone you hand it to |

They are kept honest by `extension/tests/parity.test.ts`, which reads the Python
source and fails if a tool exists on one side only, or if one of the honesty
rules — three-state submission status, the withheld roster, the refused grade
projection — was lost in translation.

The extension exists because a plain web app **cannot** work: Brightspace's
session cookies are `HttpOnly`, so no page's JavaScript can read them, and CORS
blocks reading the response even when the browser sends them. An extension is
exempt from CORS for granted hosts and never has to touch the cookie at all.
See [`extension/README.md`](extension/README.md).

## Documentation

| Doc | Contents |
|---|---|
| [`00-overview.md`](docs/00-overview.md) | **Start here.** Goals, glossary, architecture |
| [`01-authentication.md`](docs/01-authentication.md) | How we authenticate, and why the obvious way is closed |
| [`02-api-surface.md`](docs/02-api-surface.md) | Every Valence route, with permission status |
| [`03-mcp-tools.md`](docs/03-mcp-tools.md) | The tool catalog |
| [`04-rag-design.md`](docs/04-rag-design.md) | Course-file search, end to end |
| [`05-architecture.md`](docs/05-architecture.md) | Modules, config, errors, caching |
| [`06-roadmap.md`](docs/06-roadmap.md) | Phases and exit criteria |
| [`07-risks-and-policy.md`](docs/07-risks-and-policy.md) | Academic integrity, ToS, data handling |
| [`08-api-probe-results.md`](docs/08-api-probe-results.md) | Phase 0 findings *(template — not yet run)* |

## Setup

```bash
git clone <this repo> && cd avenue2learn_mcp
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/playwright install chromium     # login only
```

Sign in once (opens a real browser for MacID + 2FA):

```bash
.venv/bin/avenue-mcp login
.venv/bin/avenue-mcp status                # confirm it worked
```

Register with your MCP client:

```json
{
  "mcpServers": {
    "avenue": {
      "command": "/absolute/path/to/avenue2learn_mcp/.venv/bin/avenue-mcp",
      "args": ["serve"]
    }
  }
}
```

Then ask your assistant to `sync_course_materials` for a course, and search becomes available. First sync downloads the embedding model (~130 MB) once.

Optional: `sudo apt install libreoffice-impress` to render PowerPoint slides as images. PDF rendering needs nothing extra.

### CLI

| Command | Purpose |
|---|---|
| `avenue-mcp login` | Browser sign-in; persists the session |
| `avenue-mcp status` | Session + index state (`--offline` skips the network) |
| `avenue-mcp serve` | Run the MCP server over stdio |
| `avenue-mcp logout` | Delete the local session file |
| `avenue-mcp reindex --course ID` | Clear a course's index so it can be rebuilt |

### Verifying

```bash
.venv/bin/python -m pytest -q                   # 250 tests, no network
.venv/bin/python scripts/probe_unauth.py        # live probe, no login needed
.venv/bin/python scripts/smoke_login_chain.py   # login flow up to the password box
.venv/bin/python scripts/smoke_server.py        # server over real stdio
.venv/bin/python scripts/smoke_rag.py           # RAG round-trip, real embeddings
.venv/bin/python scripts/probe.py --course ID   # Phase 0 proper — needs login
```

The test suite includes a mock Brightspace (`tests/test_integration_tools.py`) that runs every tool in two worlds — one where all routes are permitted, one where the instructor-scope routes return `403` — so the degraded paths are exercised, not just assumed.

## Stack

Python 3.11+ · MCP ≥2.0 (`MCPServer`) · httpx · Playwright (login only) · SQLite + FTS5 · fastembed · pymupdf / python-docx / python-pptx · LibreOffice (optional, slide rendering)

Deliberately absent: any LLM SDK.

## Next step

**Step 0a — twenty minutes, before writing anything.** Install an existing D2L MCP server (`npx brightspace-mcp-server@latest`) and point it at Avenue. If it lists your courses, the cookie-session auth premise is validated. If it fails, *how* it fails tells us what McMaster's Entra chain does to browser automation.

**Then Phase 0 — the probe.** Several API routes are documented as instructor-scope, and whether a student account can reach them determines what the assignment, quiz, and grade-projection features can honestly promise.

Details in [`docs/06-roadmap.md`](docs/06-roadmap.md); the results template is [`docs/08-api-probe-results.md`](docs/08-api-probe-results.md).

## Before you use this

This is a **study aid**, not a homework machine. It helps you find, understand, and organize material you already have access to.

- Your instructor's AI policy governs. Check your course outline.
- Personal use only — your account, your machine. Don't run it for others or host it.
- This is not a McMaster-sanctioned integration. Asking UTS about official API access is the clean long-term fix.
- Course materials are instructor intellectual property. Don't redistribute them.

Read [`docs/07-risks-and-policy.md`](docs/07-risks-and-policy.md) before enabling anything that writes.

## Not affiliated with

McMaster University, D2L, or Desire2Learn. "Avenue to Learn", "Brightspace", and "D2L" are their respective owners' marks.
