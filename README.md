# avenue2learn_mcp

An MCP server that gives an AI assistant access to **Avenue to Learn**, McMaster University's D2L Brightspace instance.

Ask your assistant what's due this week, what the marking scheme actually says, or what you need on the final — and get answers grounded in your real course data instead of guesses.

> **Status: design complete, implementation not started.**
> This repository currently contains the build plan and research. No server code exists yet. Start at [`docs/00-overview.md`](docs/00-overview.md).

---

## What it will do

| | |
|---|---|
| 📚 **Courses** | List your enrollments and browse each course's Content tree |
| 📅 **Deadlines** | Cross-course view of what's due, in correct local time |
| 📝 **Assignments** | Due dates, instructions, point values, submission status |
| 📊 **Grades** | Your scores, plus "what do I need on the final for an A-?" |
| 📢 **Announcements** | Recent posts from your instructors |
| 🔍 **Search course files** | Semantic search over outlines, slides, and specs — **with citations** |

Example questions it's built to answer:

- *"What's due in the next two weeks?"*
- *"What's the late penalty in 2C03?"* → answered from the actual outline, cited to file and page
- *"How am I doing in MATH 2Z03, and what do I need on the final?"*
- *"Explain the concept on slide 14 of the Week 8 deck"*

## How it works

Three design decisions worth knowing up front:

**Authentication is a real browser login.** D2L's official API path (Valence OAuth) requires a Brightspace administrator to register an application — students can't. So instead you log in once through a real browser with your MacID and MFA, and the server reuses that session to call the same REST API Brightspace's own frontend uses. You get exactly your own permissions, nothing more. → [`docs/01-authentication.md`](docs/01-authentication.md)

**The server contains no AI model.** It fetches, parses, and returns structured data. All the reasoning happens in your MCP client. No API keys, works with any client, and a tool call is a network fetch rather than an inference.

**Course files never leave your machine.** The search index is built with local embeddings and stored in local SQLite. Your coursework isn't uploaded anywhere. → [`docs/04-rag-design.md`](docs/04-rag-design.md)

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

Submission is designed but deferred: gated behind an explicit environment flag, requiring a dry-run preview and a deliberate confirmation. When the flag is off, the write tools aren't registered at all, so the model never sees them.

The reason is simple: **a submission cannot be undone.** Every read operation can be retried at zero cost; submitting the wrong file is permanent and visible to your instructor. → [`docs/07-risks-and-policy.md`](docs/07-risks-and-policy.md)

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

## Planned stack

Python 3.11+ · FastMCP · httpx · Playwright (login only) · SQLite + FTS5 · fastembed · pymupdf / python-docx / python-pptx

Deliberately absent: any LLM SDK.

## Next step

**Phase 0 — the probe.** Several API routes are documented as instructor-scope, and whether a student account can reach them determines what the assignment and grade-projection features can honestly promise. Half a day of work that decides two headline features.

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
