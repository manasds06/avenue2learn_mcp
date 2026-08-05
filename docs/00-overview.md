# 00 — Overview

## The problem

McMaster runs its courses on **Avenue to Learn** (`avenue.mcmaster.ca`), a hosted instance of **D2L Brightspace**. Everything a student needs is in there — course outlines, lecture slides, assignment specs, marking schemes, deadlines, announcements, grades — but it is spread across a dozen courses, buried several clicks deep, and completely invisible to any AI assistant.

The result is the familiar failure mode: you ask an AI for help with an assignment and it has no idea what the assignment actually says, what the marking scheme rewards, or when it's due. You end up copy-pasting the outline into a chat window every single time.

## What we're building

An **MCP server** that exposes Avenue to Learn as a set of tools any MCP client (Claude Code, Claude Desktop, or another) can call. The assistant gets first-class access to your actual course data, so questions like these become answerable:

- "What's due in the next two weeks, across all my courses?"
- "What does the COMPSCI 2C03 marking scheme say about late submissions?"
- "Summarize the announcements I've missed this week."
- "Given my current grades, what do I need on the final to finish with an A-?"
- "Explain the concept on slide 14 of the Week 8 deck."

The last two matter most and are the reason this is more than a scraper. Grade analysis needs structured grade data; answering from slides needs a **RAG layer** that has actually read the files in the course's Content section.

## Goals

1. **Read the whole course surface** — enrollments, content tree, assignments, deadlines, announcements, grades, and (permissions allowing) class lists.
2. **Ground answers in real course files** via retrieval over PDFs, slide decks, and documents pulled from Content, with citations back to file and page.
3. **Work with any MCP client**, not just one. That means the server holds no model and makes no LLM calls.
4. **Be safe by default.** Nothing that mutates state ships enabled in v1.

## Non-goals

- **Not a Brightspace replacement.** No UI, no notifications daemon, no background sync service.
- **Not multi-tenant.** This runs locally, as you, with your session. It is not a hosted service and there is no user-management story.
- **Not an instructor tool.** Grading, feedback authoring, and roster management are out of scope — and mostly out of reach on a student account anyway.
- **Not a homework-completion machine.** See [`07-risks-and-policy.md`](07-risks-and-policy.md). Assignment submission is deliberately deferred and flag-gated.

## Glossary

Brightspace's internal vocabulary leaks into its API, and it's worth knowing before reading the route tables.

| Term | Meaning |
|---|---|
| **D2L** | Desire2Learn, the company. Their LMS product is **Brightspace**. Avenue to Learn is McMaster's branded instance of it. |
| **Valence** | D2L's public REST API and developer platform. All routes live under `/d2l/api/`. |
| **Org unit** (`orgUnitId`) | The generic container for anything in the org hierarchy — a department, a semester, a course offering. For our purposes, **a course is an org unit**, and its numeric `orgUnitId` is the handle you pass to almost every route. |
| **LP** (`/d2l/api/lp/`) | *Learning Platform* — the org-wide layer: users, enrollments, roles, org structure. |
| **LE** (`/d2l/api/le/`) | *Learning Environment* — the in-course layer: content, grades, dropbox, news, discussions, calendar. |
| **Dropbox** | Brightspace's internal name for **Assignments**. The UI says "Assignments"; the API says `dropbox`. Same thing. |
| **News** | The API's name for **Announcements**. |
| **Content topic** | A single item in a course's Content section — a file, a link, or an embedded page. File topics are what the RAG layer downloads. |
| **Content module** | A folder in the Content section. Modules nest; topics are the leaves. |
| **Grade object** | A single gradebook line item (an assignment, a quiz, a category). **Grade value** is your score on one. |
| **XSRF token** | A per-session CSRF token Brightspace requires on non-GET API calls made with cookie auth. See [`01-authentication.md`](01-authentication.md). |

## How it works, in one picture

```
        MCP client  (Claude Code / Claude Desktop)
                    │
                    │  MCP protocol over stdio
                    │
        ┌───────────▼──────────────────────────────────┐
        │  avenue-mcp server  (Python + FastMCP)       │
        │                                              │
        │   tools/    the callable surface             │
        │   client/   D2L REST client                  │
        │   auth/     session capture + refresh        │
        │   rag/      sync · extract · embed · search  │
        └───────────┬──────────────────────────────────┘
                    │
         ┌──────────┴──────────┐
         │                     │
    HTTPS + session       local disk
     cookies + XSRF      SQLite + vectors
         │                     │
         ▼                     ▼
  avenue.mcmaster.ca      ~/.avenue-mcp/
   /d2l/api/{lp,le}/     (cache + index)
```

Two structural decisions are worth stating up front, because everything else follows from them:

**The server contains no model.** It fetches, parses, and returns structured data. All reasoning — summarizing announcements, computing what you need on the final, explaining a slide — happens in the MCP client. This keeps the server small, testable, provider-agnostic, and free of API keys.

**Authentication piggybacks on a real browser session.** The officially sanctioned route (Valence OAuth 2.0) requires a McMaster Brightspace administrator to register an application; students cannot do this themselves. Instead we log in once through a real browser — MacID, password, MFA, all of it — and reuse the resulting session cookies to call the same API endpoints Brightspace's own frontend uses. You get exactly your own permissions, nothing more. Full detail in [`01-authentication.md`](01-authentication.md).

## Roadmap at a glance

| Phase | What lands |
|---|---|
| **0** | Probe script — find out what a student account can actually reach |
| **1** | Auth + HTTP client |
| **2** | Read-only tools (courses, content, assignments, deadlines, grades, announcements) |
| **3** | RAG over course files |
| **4** | Caching, error handling, setup polish |
| **5** | Write operations — deferred, flag-gated, not part of this build |

Phase 0 is not optional ceremony. Several routes we want are documented as instructor-scope, and whether a student token gets a filtered view or a flat `403` determines what [`03-mcp-tools.md`](03-mcp-tools.md) can honestly promise. Details in [`06-roadmap.md`](06-roadmap.md).

## Reading order

| Doc | Read it for |
|---|---|
| [`01-authentication.md`](01-authentication.md) | How we get in, and why the obvious way doesn't work |
| [`02-api-surface.md`](02-api-surface.md) | Every Valence route we depend on |
| [`03-mcp-tools.md`](03-mcp-tools.md) | The tool catalog the AI client sees |
| [`04-rag-design.md`](04-rag-design.md) | Course-file retrieval, end to end |
| [`05-architecture.md`](05-architecture.md) | Module layout, config, errors, caching |
| [`06-roadmap.md`](06-roadmap.md) | Phases and exit criteria |
| [`07-risks-and-policy.md`](07-risks-and-policy.md) | Academic integrity, ToS, data handling |
| [`08-api-probe-results.md`](08-api-probe-results.md) | Phase 0 findings (filled in during the probe) |
