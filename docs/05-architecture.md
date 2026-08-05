# 05 — Architecture

Module layout, data flow, configuration, errors, caching.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Best ecosystem for the RAG half — PDF/DOCX/PPTX parsing and local embeddings are all mature here |
| MCP framework | `mcp` (official Python SDK), FastMCP API | Decorator-based tool registration; reference implementation |
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
    │   ├── content.py       # get_course_content, read_content_file
    │   ├── assignments.py   # list_assignments, get_upcoming_deadlines
    │   ├── grades.py        # get_grades, analyze_grade_summary
    │   ├── announcements.py # list_announcements
    │   ├── classlist.py     # get_class_list
    │   ├── search.py        # search_course_materials, sync_course_materials
    │   └── writes.py        # gated; registered only when writes enabled
    │
    ├── rag/
    │   ├── sync.py          # orchestrates discover → store
    │   ├── extract.py       # per-format text extraction
    │   ├── chunk.py         # structure-aware chunking
    │   ├── embed.py         # fastembed wrapper
    │   └── store.py         # SQLite + vector persistence
    │
    └── util/
        ├── html.py          # HTML → clean text, link extraction
        ├── dates.py         # UTC ↔ America/Toronto
        └── throttle.py      # concurrency + rate limiting
```

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

`AVENUE_MCP_BASE_URL` being configurable is worth a note: nothing in this design is McMaster-specific except the default URL and the login flow's success-detection. The Valence API is the same everywhere. Other D2L schools should mostly work.

### State directory

```
~/.avenue-mcp/
├── session.json          # Playwright storage_state — 0600, treat as a credential
├── index.db              # SQLite: documents, chunks, FTS5, meta
├── vectors.npy           # embedding matrix
├── cache/                # downloaded course files
│   └── {org_unit_id}/{topic_id}/{filename}
└── logs/
    ├── avenue-mcp.log
    └── writes.log        # append-only audit of every write attempt
```

`writes.log` is separate and append-only on purpose. If a submission ever goes out unexpectedly, that file is the record of what happened and when.

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
| Response | memory (LRU) | 300 s | Enrollments, content trees, announcements |
| Files | disk | until changed | Downloaded course files |
| Index | disk | until re-synced | Chunks + vectors |

The response cache exists because `get_upcoming_deadlines` fans out across every active course. Without it, three consecutive "what's due?" questions triple the request count for identical data.

**Never cached:** grades and submission status. Both change in ways the user cares about immediately, and a stale grade is a bad answer. The cost is a couple of extra requests; the alternative is telling someone they got 92 when the prof regraded it to 78.

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

**No background tasks. No polling. No timers.** Every request traces back to a user-initiated tool call. This is a design property worth being able to state plainly, not just an implementation detail — see [`07-risks-and-policy.md`](07-risks-and-policy.md).

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
      └── first network tool call
            ├── load session          → NoSessionError if absent
            ├── negotiate API version → cached
            └── proceed
```

**Startup does no network I/O and requires no session.** An MCP client launches the server at startup and expects it to come up immediately; blocking on a network call — or worse, on a login prompt — makes the client appear broken. Auth is lazy, on first use, with a clear error.

**Write tools are not registered when the flag is off.** Not registered-and-erroring — absent from the tool list entirely. The model can't call a tool it can't see, can't be talked into calling it, and can't hallucinate having used it.

## Testing

| Layer | Approach |
|---|---|
| `rag/extract.py`, `chunk.py` | Pure functions over fixture files. Fast, deterministic, highest coverage. |
| `client/` | Recorded fixtures from Phase 0 probe responses, replayed via `respx` |
| `tools/` | Fake `D2LClient`, no network |
| `auth/` | Manual — real SSO with MFA can't be meaningfully automated |
| End-to-end | Manual against a live account, checklist in [`06-roadmap.md`](06-roadmap.md) |

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
