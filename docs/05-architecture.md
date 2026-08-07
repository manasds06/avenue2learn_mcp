# 05 — Architecture

Module layout, data flow, configuration, errors, caching.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ (built and tested on 3.12) | Best ecosystem for the RAG half — PDF/DOCX/PPTX parsing and local embeddings are all mature here |
| MCP framework | `mcp` ≥ 2.0, `MCPServer` API | Decorator-based tool registration; reference implementation |

> **API note discovered during implementation.** MCP 2.x restructured the SDK.
> The server class is now `mcp.server.mcpserver.MCPServer`; the older
> `mcp.server.fastmcp.FastMCP` path **no longer exists**. `Image` is exported
> from the same module. `list_tools()` is async and returns objects with
> `.name` / `.description`; `run(transport="stdio")` is the entry point. Response
> models use snake_case (`server_info`, `is_error`), not camelCase. `pyproject.toml`
> pins `mcp>=2.0.0` accordingly.
| HTTP | `httpx` | Async, HTTP/2, proper cookie-jar handling |
| Browser | `playwright` | Login only. Not used for scraping. |
| Storage | `sqlite3` (stdlib) + FTS5 | Zero-config, single file, full-text search built in |
| Embeddings | `fastembed` | ONNX, CPU-fast, no PyTorch |
| Documents | `pymupdf`, `python-docx`, `python-pptx`, `beautifulsoup4` | |
| Validation | `pydantic` v2 | Tool schemas, config, API response models |
| Packaging | `uv` + `pyproject.toml` | |

**Deliberately absent: any LLM SDK.** The server never calls a model. That keeps it provider-agnostic, key-free, cheap to test, and means a tool call is a network fetch rather than an inference. If you find yourself wanting to add `anthropic` as a dependency, the logic in question belongs in the client instead.

## Layout

```
avenue2learn_mcp/
├── pyproject.toml
├── README.md
├── docs/
└── src/avenue_mcp/
    ├── __init__.py
    ├── __main__.py          # entrypoint: `serve` and `login`
    ├── server.py            # FastMCP instance, tool registration
    ├── config.py            # env-driven settings (pydantic-settings)
    ├── errors.py            # the error taxonomy
    │
    ├── auth/
    │   ├── base.py          # AuthProvider protocol
    │   ├── session.py       # CookieSessionAuth + SessionManager
    │   ├── login.py         # Playwright login flow
    │   └── oauth.py         # documented stub — see 01-authentication.md
    │
    ├── client/
    │   ├── d2l.py           # D2LClient: versioning, paging, retries
    │   ├── paging.py        # bookmark pagination
    │   └── models.py        # pydantic models for API responses
    │
    ├── tools/
    │   ├── courses.py       # list_courses
    │   ├── content.py       # get_course_content, read_content_file, get_page_image
    │   ├── assignments.py   # list_assignments, get_upcoming_deadlines
    │   ├── quizzes.py       # list_quizzes
    │   ├── grades.py        # get_grades, analyze_grade_summary
    │   ├── announcements.py # list_announcements
    │   ├── discussions.py   # list_discussions, read_discussion_thread
    │   ├── whatsnew.py      # get_whats_new  (reads the watermark store)
    │   ├── classlist.py     # get_class_list
    │   ├── search.py        # search_course_materials, sync_course_materials
    │   ├── status.py        # get_status
    │   └── writes.py        # gated; registered only when writes enabled
    │
    ├── rag/
    │   ├── sync.py          # orchestrates discover → store
    │   ├── extract.py       # per-format text extraction
    │   ├── chunk.py         # structure-aware chunking
    │   ├── embed.py         # fastembed wrapper
    │   ├── render.py        # page/slide → image  (get_page_image)
    │   ├── store.py         # SQLite + vector persistence
    │   └── watermarks.py    # per-course, per-category last-seen state
    │
    └── util/
        ├── html.py          # HTML → clean text, link extraction
        ├── dates.py         # UTC ↔ America/Toronto
        └── throttle.py      # concurrency, rate limiting, keepalive
```

Two placements worth explaining:

**`watermarks.py` lives in `rag/`, not `tools/`.** It shares the SQLite file with the index and is read by two consumers — `get_whats_new` for the digest, and discussion sync for incremental fetching. Keeping it next to `store.py` avoids two modules owning the same database.

**`render.py` is in `rag/`, not `tools/`.** It operates on the cached-file corpus, same as extraction. `get_page_image` is a thin tool wrapper over it.

Rules the layout enforces:

- **`tools/` never talks to HTTP directly.** It calls `client/`. Keeps tools testable against a fake client with no network.
- **`client/` knows nothing about MCP.** It's a plain D2L API client, usable from a script or a test.
- **`rag/` never touches the network** except through `client/`. Extraction and chunking are pure functions over bytes — the easiest things in the codebase to test, and worth keeping that way.
- **`auth/` is the only place Playwright appears.** One import, one blast radius.

## Data flow

A representative call — `search_course_materials`:

```
MCP client
  │ tools/call: search_course_materials{query, org_unit_id}
  ▼
server.py ── dispatch ──▶ tools/search.py
                              │
                              ├─▶ rag/embed.py     query → vector
                              ├─▶ rag/store.py     vector scan + FTS5
                              │                     RRF fusion
                              └─▶ citations attached
                              ▼
                        structured JSON ──▶ MCP client
```

No network, no session needed. Contrast with `list_courses`:

```
tools/courses.py
   │
   ├─▶ auth/session.py    require_session() → httpx client w/ cookies
   ├─▶ client/d2l.py      GET myenrollments (paged, throttled, retried)
   ├─▶ client/models.py   validate + normalize
   └─▶ filter to active course offerings
   ▼
structured JSON
```

Every network tool follows that shape: session → client → normalize → return.

## Configuration

Environment variables, loaded via `pydantic-settings`. No config file, no CLI flags for anything that matters — MCP servers are launched by a client with an env block, so env is the only channel that reliably exists.

| Variable | Default | Purpose |
|---|---|---|
| `AVENUE_MCP_BASE_URL` | `https://avenue.mcmaster.ca` | Instance URL. Configurable so another D2L school can use this. |
| `AVENUE_MCP_STATE_DIR` | `~/.avenue-mcp` | Session, cache, index |
| `AVENUE_MCP_ENABLE_WRITES` | `0` | Gates submission tools. **Off.** |
| `AVENUE_MCP_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Changing this invalidates the index |
| `AVENUE_MCP_MAX_FILE_MB` | `100` | Download cap |
| `AVENUE_MCP_MAX_CONCURRENCY` | `4` | Simultaneous requests |
| `AVENUE_MCP_MIN_REQUEST_INTERVAL_MS` | `100` | Politeness floor |
| `AVENUE_MCP_CACHE_TTL_SECONDS` | `300` | In-memory response cache |
| `AVENUE_MCP_LOG_LEVEL` | `INFO` | |
| `AVENUE_MCP_TIMEZONE` | `America/Toronto` | Local rendering of deadlines |
| `AVENUE_MCP_KEEPALIVE_MINUTES` | `0` | Session keepalive interval. **`0` = off** pending Phase 0. |
| `AVENUE_MCP_KEEPALIVE_IDLE_STOP` | `120` | Stop keepalive after this many idle minutes |
| `AVENUE_MCP_INDEX_DISCUSSIONS` | `1` | Include discussion threads in the RAG corpus |
| `AVENUE_MCP_RENDER_DPI` | `120` | Default `get_page_image` resolution |
| `AVENUE_MCP_GRADE_SCALE` | *(unset)* | Path to a letter-grade cutoff table. **Unset means no letter projections.** |

`AVENUE_MCP_BASE_URL` being configurable is worth a note: nothing in this design is McMaster-specific except the default URL and the login flow's success-detection. The Valence API is the same everywhere. Other D2L schools should mostly work.

### `AVENUE_MCP_GRADE_SCALE` — unset on purpose

Letter-grade cutoffs are not in any API, and they are **not hardcoded from memory**. Unset, `analyze_grade_summary` works entirely in percentages and weights; it will not claim a letter grade or say "you need X for an A-".

Supply a path to a small table to enable letter projections:

```json
{ "A+": 90, "A": 85, "A-": 80, "B+": 77, "...": 0 }
```

The right source for that table is the course outline itself, which `search_course_materials` can find — cutoffs vary by faculty and program, so a single hardcoded scale would be confidently wrong for some of a student's own courses.

### The deeper problem with grade projection

Worth recording where implementers will see it, because it's easy to underestimate:

**Brightspace exposes gradebook *numbers*, not gradebook *rules*.** The API gives you weights and points. It does not reliably tell you:

- **Drop-lowest rules** ("best 9 of 10 quizzes")
- **Bonus items** that add without adding to the denominator
- **Nested category weights** where a category's internal distribution differs from its outer weight
- **Ungraded vs. zero** — a blank cell may mean "not marked yet" or "you didn't submit", and those produce opposite projections

A naive weighted average will therefore be **silently wrong in a meaningful fraction of real courses.** The implementation must:

1. Compute over graded items only, never imputing zeros for blanks.
2. Report `graded_weight` and `remaining_weight` explicitly so the user can sanity-check the denominator.
3. Populate `caveats` whenever the gradebook shape suggests a rule the API didn't expose (categories present, item count mismatch, weights not summing to 100).
4. **Refuse rather than guess** when weights are unavailable or don't reconcile.

This is the same principle as the ⚠️ routes: a confidently wrong number is worse than an honest "I can't compute that." Grade math is where that's most true, because the student acts on it.

### State directory

```
~/.avenue-mcp/
├── session.json          # Playwright storage_state — 0600, treat as a credential
├── index.db              # SQLite: documents, chunks, FTS5, watermarks, meta
├── vectors.npy           # embedding matrix
├── cache/                # downloaded course files
│   ├── {org_unit_id}/{topic_id}/{filename}
│   └── renders/          # rendered page images (get_page_image)
└── logs/
    ├── avenue-mcp.log
    └── writes.log        # append-only audit of every write attempt
```

`writes.log` is separate and append-only on purpose. If a submission ever goes out unexpectedly, that file is the record of what happened and when.

The `watermarks` table lives inside `index.db` rather than a separate file — it's read by both `get_whats_new` and discussion sync, and one database avoids two modules disagreeing about the same state. Schema in [`04-rag-design.md`](04-rag-design.md).

## Error taxonomy

```
AvenueMCPError
├── AuthError
│   ├── NoSessionError          not logged in
│   ├── SessionExpiredError     was valid, no longer
│   └── LoginTimeoutError       login window closed early
├── APIError
│   ├── PermissionDeniedError   403 on a healthy session
│   ├── NotFoundError           404
│   ├── InvalidRequestError     400
│   └── UpstreamError           5xx after retries
├── RAGError
│   ├── NotIndexedError         course never synced
│   ├── ExtractionError         couldn't read the file
│   └── EmbedModelMismatchError index built with a different model
└── ConfigError
```

**Every error surfaced to the model carries a next action in plain language.** The model is the one relaying it to a human; an error that says only "403 Forbidden" produces a useless response.

| Error | Message |
|---|---|
| `NoSessionError` | "Not logged in to Avenue. Run `avenue-mcp login` in a terminal, complete MacID sign-in, then retry." |
| `SessionExpiredError` | "Avenue session expired. Run `avenue-mcp login` to sign in again, then retry." |
| `PermissionDeniedError` | "Your Avenue account doesn't have access to this — it may be instructor-only. This isn't a login problem." |
| `NotIndexedError` | "COMPSCI 2C03 hasn't been indexed yet. Run `sync_course_materials` for it first." |
| `EmbedModelMismatchError` | "The search index was built with a different embedding model. Re-run `sync_course_materials` with `force: true`." |

The `SessionExpiredError` / `PermissionDeniedError` split is the important one. Both are "you can't have this," but one is fixed by logging in and the other never will be. Collapsing them sends users into a re-login loop against a permission wall.

## Caching

Three layers, different lifetimes.

| Layer | Where | TTL | Contents |
|---|---|---|---|
| API version | memory | process | `/d2l/api/versions/` result |
| Response | memory (LRU) | 300 s | Enrollments, content trees, announcements, forums |
| Files | disk | until changed | Downloaded course files |
| Renders | disk | until file changes | Rendered page images |
| Index | disk | until re-synced | Chunks + vectors |

The response cache exists because `get_upcoming_deadlines` and `get_whats_new` both fan out across every active course. Without it, three consecutive "what's due?" questions triple the request count for identical data.

**Never cached:** grades and submission status. Both change in ways the user cares about immediately, and a stale grade is a bad answer. The cost is a couple of extra requests; the alternative is telling someone they got 92 when the prof regraded it to 78.

**Also never cached: `get_whats_new` results, distinct from the underlying route responses.** The digest is a diff against a watermark; caching the *diff* would return "3 new announcements" after the watermark already advanced. The individual route responses it reads are cached normally; the computed result is not.

Cache is keyed on the full request path including version and query params, and cleared on session change.

## Concurrency and throttling

One shared `Throttle` in `util/throttle.py`, wrapping every outbound request:

```
asyncio.Semaphore(MAX_CONCURRENCY)     # default 4
  + minimum inter-request interval      # default 100 ms
  + jittered exponential backoff on 429/5xx, max 3 attempts
  + honors Retry-After
```

Global, not per-tool — the constraint is "requests we send to Avenue," and a per-tool limiter lets a fan-out tool blow past it.

`sync_course_materials` is the heaviest caller and uses the same limiter as everything else. It reports progress so a multi-minute run doesn't look like a hang.

**No data polling. No scheduled jobs. No background sync.** Every data request traces back to a user-initiated tool call.

### The one exception: session keepalive

There is exactly one timer in the codebase, and it is bounded on all sides:

| Property | Value |
|---|---|
| What it sends | `GET /d2l/lp/auth/xsrf-tokens` — the liveness probe, nothing else |
| When it starts | After the **first** tool call of a process. Never at startup. |
| When it stops | After `AVENUE_MCP_KEEPALIVE_IDLE_STOP` minutes with no tool calls (default 120), or on shutdown |
| Interval | `AVENUE_MCP_KEEPALIVE_MINUTES`, **default `0` = disabled** |
| What it fetches | Nothing. No course data, no queries. |

It exists because daily re-login is the friction most likely to make the tool unpleasant enough to abandon. It is **off by default in v1** until Phase 0 confirms sessions actually extend on activity.

The line that keeps this consistent with [`07-risks-and-policy.md`](07-risks-and-policy.md): *no polling for data, ever; one lightweight session-extension request during an active working session, which stops on its own.* That's less traffic than an idle Avenue browser tab, which does the same thing automatically. Recording the distinction here so a later reader doesn't see "there's a timer" and conclude the no-polling commitment was quietly dropped.

## Working without a session

A design property, not a fallback — and one to protect during refactors.

| Capability | Needs a session? |
|---|---|
| `search_course_materials` | ❌ Local index only |
| `get_status` (`check_session: false`) | ❌ Local state only |
| `get_page_image` on a cached file | ❌ Renders from disk |
| Everything else | ✅ |

A student whose session expires mid-study still has full semantic search over everything they've indexed. Only live-data tools degrade, and they degrade with a clear `SessionExpiredError` rather than a confusing failure.

Implementation consequence: **`tools/search.py` and `tools/status.py` must not call `require_session()` on their read paths.** It's an easy line to add reflexively for consistency, and adding it silently destroys this property.

## Server lifecycle

```
startup
  ├── load config
  ├── open SQLite, run migrations
  ├── verify embed model matches index_meta   → warn, don't crash, on mismatch
  ├── register read tools
  ├── register write tools  ONLY IF AVENUE_MCP_ENABLE_WRITES=1
  └── serve over stdio
      │
      ├── first tool call
      │     ├── if local-only (search / status / cached render) → serve, no session
      │     └── if network:
      │           ├── load session          → NoSessionError if absent
      │           ├── negotiate API version → cached
      │           ├── start keepalive       if enabled
      │           └── proceed
      │
      └── idle > KEEPALIVE_IDLE_STOP → stop keepalive
```

**Startup does no network I/O and requires no session.** An MCP client launches the server at startup and expects it to come up immediately; blocking on a network call — or worse, on a login prompt — makes the client appear broken. Auth is lazy, on first use, with a clear error.

**Local-only tools short-circuit before the session check.** `search_course_materials`, `get_status`, and cached renders serve from disk without touching auth — that's what makes the offline behavior above real rather than aspirational.

**Write tools are not registered when the flag is off.** Not registered-and-erroring — absent from the tool list entirely. The model can't call a tool it can't see, can't be talked into calling it, and can't hallucinate having used it.

## Testing

| Layer | Approach |
|---|---|
| `rag/extract.py`, `chunk.py` | Pure functions over fixture files. Fast, deterministic, highest coverage. |
| `rag/watermarks.py` | Pure over a temp SQLite file — easy and worth doing, since off-by-one watermark bugs silently lose changes |
| `util/dates.py` | **Must include a DST-boundary fixture.** See below. |
| `client/` | Recorded fixtures from Phase 0 probe responses, replayed via `respx` |
| `tools/` | Fake `D2LClient`, no network |
| `auth/` | Manual — real SSO with MFA can't be meaningfully automated |
| End-to-end | Manual against a live account, checklist in [`06-roadmap.md`](06-roadmap.md) |

**The DST fixture is not optional.** Brightspace returns UTC; McMaster deadlines are set in Eastern and land at 11:59 PM local. Depending on DST that's `03:59` or `04:59` UTC *the next day*. A deadline in the week around a DST transition is the case that silently reports the wrong day, and "your assignment is due March 16" for a March 15 deadline is a failure that costs the user real marks. Fixture set: a deadline before the transition, one after, and one inside the transition week, each asserted against its expected local rendering.

Phase 0's captured responses become the fixture corpus. That's a second reason the probe matters: it's not just permission discovery, it's the test data for everything downstream.

`auth/` being manual-only is an honest limitation. An MFA-gated interactive browser login is not something to pretend is unit-testable.

## Deployment

Registered in the MCP client config:

```json
{
  "mcpServers": {
    "avenue": {
      "command": "uv",
      "args": ["run", "--directory", "/home/nela/github/avenue2learn_mcp", "avenue-mcp", "serve"],
      "env": { "AVENUE_MCP_LOG_LEVEL": "INFO" }
    }
  }
}
```

Login is a separate, interactive command run in a terminal:

```
uv run avenue-mcp login
```

Deliberately separate from `serve`. Login needs a visible browser window and a human; the server runs headless under a client. Conflating them produces a server that hangs on a window nobody can see.
