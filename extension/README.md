# Brightspace Assistant — browser extension

A Chrome extension that answers questions about your university's Brightspace
courses — deadlines, grades, announcements, and what's actually written in your
course files — using your own browser session and your own LLM key.

Same 17 tools as the Python MCP server in this repo. The difference is who runs
them: that one runs on your machine for you, through Claude Code. This one is
something you can hand to a classmate.

---

## Why an extension and not a website

A web page cannot reach Brightspace on your behalf. Two independent walls, both
verified rather than assumed:

| Cookie | httpOnly | sameSite |
|---|---|---|
| `d2lSessionVal` | **true** | None |
| `d2lSecureSessionVal` | **true** | None |

Both session cookies are **HttpOnly**, so JavaScript cannot read them from any
origin — not ours, not a page on Brightspace itself. And because Brightspace
never returns `Access-Control-Allow-Origin` for another site with
`Allow-Credentials`, a cross-origin `fetch` can *send* the cookies but never
read the response.

An extension is exempt from CORS for hosts it has been granted, and the browser
attaches the cookies itself. **So this code never sees your session cookie.**
There is no credential here to store or leak, which is what makes "we store
nothing" a structural fact rather than a promise.

It also deletes a whole subsystem the Python side needs: no Playwright, no
`session.json`, no MFA automation, no expiry handling. You sign into Brightspace
normally. A 403 means "go sign in".

## What leaves your machine

Worth being precise, because the answer differs by half:

- **Brightspace half — nothing.** Course files are downloaded, parsed, embedded,
  and indexed entirely in your browser. The embedding model ships inside the
  extension; your coursework never reaches an API.
- **LLM half — your questions and the tool results.** Course names, deadlines,
  grades, and retrieved passages go to Google under **your own API key**. Same
  exposure as pasting the material into any chat, and it is the one place this
  is not local.

There is no server of ours anywhere in either path. A test fails if one appears.

---

## Setup

```bash
npm install
npm run fetch-model    # ~33MB embedding model, once (build runs this too)
npm run build
```

Then `chrome://extensions` → Developer mode → **Load unpacked** → pick `dist/`.

In the side panel:

1. **Your school** — grants access to that one host. McMaster and Carleton ship
   with profiles; each user grants only their own.
2. **Model** — any Gemini model id. `gemini-2.5-flash-lite` has the highest free
   limits, and free quotas are per-model, so switching is the fastest fix when
   one is rate-limited.
3. **API key** — free from [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
   Stored in `chrome.storage.local`, never `sync`.

Ask a course to be synced before using file search:

> sync course materials for 759806

Incremental and persistent: unchanged files are skipped, and the index survives
restarts. Re-sync only when new material is posted.

---

## Layout

```
src/
├── institutions.ts     school profiles + measured route capabilities
├── settings.ts         school, model, API key — all chrome.storage.local
├── avenue/             Valence client, errors, dates, normalization
├── tools/              the 17 tools + registry (name, schema, description)
├── rag/                extract · chunk · embed · retrieve · store · sync
├── llm/                JSON Schema → Gemini, and the function-calling loop
└── ui/                 side panel
```

**Where code runs matters here.** The service worker handles live-data tools;
the panel handles the file tools and the LLM loop. A service worker has no
`DOMParser` and no pdf.js worker, and MV3 kills it after ~30s idle — which a
multi-minute sync and a multi-round tool conversation both exceed. `PANEL_TOOLS`
in `tools/registry.ts` is the split, and a test enforces that the worker imports
the registry lazily so it can start at all.

---

## Tests

```bash
npm test        # 56, no network, no credentials
npm run typecheck
```

Three kinds, and the split is deliberate:

- **`guarantees.test.ts`** — the promises made to users, as tests. No cookie
  access anywhere, no `storage.sync` for the key, exactly two reachable hosts
  and neither is ours, no school inherits another's brand.
- **`parity.test.ts`** — reads the Python source directly and fails if a tool
  exists on one side only, or if an honesty rule was lost in translation.
- **`schema.test.ts` / `content.test.ts`** — the two places a silent mistake
  breaks everything at once: Gemini rejecting the whole request over one
  malformed declaration, and the content tree reporting zero files.

Several of these exist because the bug happened. `content.test.ts` pins the
shape of `content/root/` because trusting its embedded stub found **zero files
in a course with 26**, and reported it as "no course materials found" — which
reads as an empty course rather than a broken walk.

---

## Deliberately not built

- **Writing anything.** No submission, no discussion posts. Read-only is
  enforced by there being no write code, not by a flag.
- **Quiz questions or answers.** Metadata only — names, dates, attempt counts.
- **The student roster.** `classlist` works and returns 145 people with emails
  on a single course. It is withheld: that is other people's personal
  information under FIPPA, and the tool exists to return instructor contacts.
- **A shared API key.** Would need a proxy of ours, and that proxy would see
  every user's grades and deadlines. Bring your own key.

See [`../docs/07-risks-and-policy.md`](../docs/07-risks-and-policy.md). An
extension acting as you, in your browser, with your own session, storing
nothing, is a materially different position from a hosted service — but it is
still not a McMaster-sanctioned integration.
