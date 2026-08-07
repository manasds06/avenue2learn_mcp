# 07 — Risks and Policy

The uncomfortable section. Written plainly rather than hedged, because a document that gestures vaguely at "please use responsibly" is worth nothing.

---

## 1. Academic integrity

**This is a study aid. It is not a homework machine, and it must not become one.**

### The line

McMaster's *Academic Integrity Policy* defines academic dishonesty as gaining an unearned academic advantage. The relevant category here is **plagiarism / improper collaboration** — submitting work that is not your own.

The line is not about whether AI was involved. It is about **whose work gets submitted**:

| Clearly fine | Clearly not |
|---|---|
| "What's the late penalty in 2C03?" | "Write my A3 and submit it" |
| "Summarize this week's announcements" | "Do the problem set for me" |
| "What's due next week?" | "Generate an essay and upload it" |
| "Explain the concept on slide 14" | "Answer these quiz questions" |
| "What do I need on the final for an A-?" | |
| "What does the marking scheme reward?" | |

Everything in the left column is retrieval and explanation over material you already have access to — the same thing as reading your own outline more efficiently. Everything in the right column is producing work for submission.

### Course-specific rules override this document

**Individual instructors set AI policy for their courses, and those rules govern.** Some McMaster courses prohibit AI assistance entirely; some require disclosure; some permit it freely. A course outline saying "no AI tools" means no AI tools, and this server doesn't change that.

Check your outline. The server can even help you find the clause — `search_course_materials` with "AI policy" or "generative AI" is a legitimate first use.

### How the design enforces this

Not as a disclaimer, but structurally:

| Decision | Effect |
|---|---|
| v1 is read-only | The server *cannot* submit anything. Not "shouldn't" — cannot. |
| Write tools gated behind `AVENUE_MCP_ENABLE_WRITES=1` | Off by default; requires deliberate config |
| Gated tools are **not registered** when disabled | The model never sees them, can't be talked into them, can't hallucinate using them |
| Dry-run required before any submission | Two deliberate steps, never one |
| Explicit `confirm: true`, no default | No accidental truthy coercion |
| Append-only audit log | Every write attempt recorded locally |
| No content generation in the server | It has no model — it retrieves and returns facts |

That last point matters more than it appears. **The server contains no LLM.** It cannot write an essay. It fetches and returns your own course data. Whatever the model in your client does with that is the same question as any other AI use, governed by your course's policy — the server neither enables nor constrains it beyond providing accurate context.

### The honest framing

Better context makes AI assistance *more* useful for legitimate purposes and doesn't meaningfully change the illegitimate ones. A student who wants to cheat can already paste an assignment into a chat window; this project doesn't unlock that. What it unlocks is the assistant knowing your actual deadline, your actual marking scheme, and your actual lecture content — which is exactly the thing that makes it useful for studying rather than for cheating.

**If you enable write mode and submit work you didn't do, that is a policy violation and the tooling is not a defense.** Stated here so it can't be claimed nobody said so.

### Quizzes: metadata only, deliberately

One capability is scoped narrowly on integrity grounds rather than technical ones.

`list_quizzes` returns **names, dates, availability windows, and whether you've attempted** — and nothing else. Quiz *questions* and *answers* are explicitly out of scope, and would remain out of scope even if a route exposed them to students.

The line is clean: knowing *a quiz is due Friday* is calendar information. Retrieving *the quiz's contents* while it's open is the thing academic integrity policies exist to prohibit. Since quizzes are also excluded from the RAG corpus, there is no path by which quiz content reaches the index.

Same reasoning applies to discussion posting: reading a forum thread to find your instructor's clarification is studying; having an AI post in your name to a space your classmates read is not something this server does. See [`02-api-surface.md`](02-api-surface.md) *Routes deliberately not used*.

---

## 2. Terms of service and institutional policy

### What we're doing

Automating access to **your own Avenue account**, using **your own credentials**, receiving **only data you already have permission to see**. The API enforces the same role checks as the web UI — see [`01-authentication.md`](01-authentication.md).

This is materially different from unauthorized access. There is no privilege escalation, no other user's data, no circumvention of an access control. If you can't see a class roster in your browser, this tool can't either.

### What's nonetheless true

**This is not a sanctioned integration.** McMaster UTS has not approved it. The supported path is Valence OAuth with an admin-registered application, and we're not on it — because that path isn't open to students ([`01`](01-authentication.md)).

Realistic considerations:

| Concern | Assessment |
|---|---|
| Automated access may violate an acceptable-use policy | Possible. McMaster's IT policies are worth reading. Automation clauses in university AUPs are typically aimed at scraping, load, and shared credentials — none of which apply here — but "typically" isn't "certainly". |
| Credential sharing | Not applicable. Nothing is shared; the session stays on your machine. |
| Load / abuse | Actively mitigated. See §4. |
| Circumventing access controls | Not applicable. Permissions are enforced server-side and unchanged. |

### The clean fix

**Ask.** UTS or the Avenue support team can say whether student API access is available, whether a Valence application can be registered for personal use, or whether there's an approved alternative. The worst outcome is being told no, at which point you know.

The architecture already accommodates a yes: `AuthProvider` has an OAuth slot ([`01`](01-authentication.md)), and switching would be a config change, not a rewrite.

### Personal use only

Do not distribute this as a service, do not run it for other students, do not host it. A single student automating their own account is a defensible position. Operating an unsanctioned service against a university system for others is not, and it converts a personal-tooling question into an institutional one.

---

## 3. Data handling and privacy

### What's stored locally

| Data | Location | Notes |
|---|---|---|
| Session cookies | `~/.avenue-mcp/session.json` | `0600`. **Equivalent to your logged-in session.** |
| Course files | `~/.avenue-mcp/cache/` | Slides, outlines, readings — instructor-copyrighted |
| Extracted text + vectors | `~/.avenue-mcp/index.db`, `vectors.npy` | Includes **discussion post text**, role-attributed only — see below |
| Rendered page images | `~/.avenue-mcp/cache/renders/` | Derived from cached files |
| Watermarks | `~/.avenue-mcp/index.db` | Timestamps only, no content |
| Grades | Not persisted | Fetched live, never cached ([`05`](05-architecture.md)) |
| Logs | `~/.avenue-mcp/logs/` | Credential-redacted |

### What leaves the machine

**To Avenue:** ordinary API requests as you.

**To your MCP client's model provider:** whatever the tools return — course names, deadlines, grades, announcements, retrieved passages from course files. This is the same exposure as pasting the material into a chat, and it's worth being explicit that it exists.

**To anyone else: nothing.** No telemetry, no analytics, no third-party services. Embeddings run locally precisely so course content never goes to an embedding API ([`04`](04-rag-design.md)).

### Other people's data

Course files are **instructor intellectual property**. Lecture slides, custom problem sets, and original notes are the instructor's work, licensed to enrolled students for personal academic use.

Consequences:

- Fine: caching them locally for your own study.
- Not fine: redistributing them, uploading them anywhere public, sharing your index.
- **Never commit the cache directory.** `.gitignore` covers it; that is not a substitute for not doing it.

If `get_class_list` turns out to work (unlikely — see [`02`](02-api-surface.md)), it returns **other students' names and email addresses**, which is personal information under Ontario's FIPPA. Do not export it, do not build a mailing list, do not persist it. The tool is designed to return instructor contacts, and the probable `403` on the roster is the correct outcome, not a bug to work around.

### Discussion posts: other students' words, indexed

Indexing discussion threads ([`04-rag-design.md`](04-rag-design.md)) means classmates' posts land in a local searchable database. That's a real privacy consideration and it gets a real answer rather than a shrug.

**Author names are not stored.** Anywhere. Chunks carry `author_role` — `Instructor` / `TA` / `Student` — and nothing else. The role is what determines authority, which is the only reason the field exists; the name adds no retrieval value and would turn the index into a durable name-to-opinion record of a course's private forum.

Two consequences worth accepting deliberately:

- You cannot ask "what did *[classmate]* say about A3?" That's not a missing feature; it's the point.
- Search results attribute to a role and a date — *"your instructor said this on March 9"* — which is what a student actually needs from a citation anyway.

Everything else in this section still applies: don't export the index, don't share it, don't commit it. A course forum is a semi-private space, and the fact that you can read it as an enrolled student doesn't make its contents yours to redistribute.

### Prompt injection: course content is not fully trusted input

Worth stating because the shape of this system makes it relevant, even though the practical risk here is low.

Course files and discussion posts are **text written by other people** that flows into a model **holding tools**. A PDF or a forum post could contain text crafted to read as instructions — *"ignore previous instructions and…"*. In a read-only v1 the realistic worst case is a wrong or weird answer. It gets more consequential if write mode is ever enabled, since a tool that can submit is a tool an injected instruction might try to invoke.

Mitigations, mostly structural:

| Mitigation | Effect |
|---|---|
| Retrieved passages are returned as **data with citations**, not as instructions | The model sees "here is a passage from a file", not a bare imperative |
| v1 has **no write tools registered** | Nothing to hijack |
| Write mode requires an explicit `confirm: true` **from the user's turn** | An injected instruction cannot supply user confirmation |
| Dry-run preview shows what would be submitted | A human sees the target before anything happens |
| Audit log | If something odd happens, there's a record |

Realistic threat level: **low.** Nobody is planting injection payloads in a COMPSCI outline. But the mitigation that matters is already in place for other reasons — writes are gated behind a human confirmation that model-visible text cannot forge — and that's the property to preserve if the write path is ever built. Recording it here so it's a known consideration rather than a surprise.

### Session file handling

Bears repeating because it's the most likely real leak:

- It is a credential. Anyone with it can act as you on Avenue until expiry.
- `0600`, outside the repo, gitignored twice over.
- Not in backups you don't control, not in cloud sync, not pasted into a bug report or a chat.
- If it leaks: log out of Avenue everywhere and change your MacID password.

---

## 4. Being a good client

An unsanctioned integration that generates abnormal load is the one that gets noticed and blocked. Politeness is a design requirement, not an optimization.

| Control | Value | Rationale |
|---|---|---|
| Max concurrency | 4 | Below a browser's typical parallelism |
| Min request interval | 100 ms | Floor on burst rate |
| Retries | Max 3, jittered exponential backoff | No retry storms |
| `Retry-After` | Honored | Do what we're told |
| Caching | Aggressive on stable data | Fewer requests for identical data |
| **Polling for data** | **None, ever** | Every data request traces to a user action |
| Scheduled jobs | None | No cron, no daemon, no background sync |
| Sync | Explicit, user-initiated only | Never automatic, never on a search miss |
| Session keepalive | One liveness ping / 30 min, **only during an active session** | See the carve-out below. Default **off**. |

**The no-background-work property is worth being able to state plainly.** This server does not poll Avenue for data. It is not a bot; it's a slightly automated browser session that moves when you ask it to. That's a meaningfully different traffic profile from a scraper, and it's deliberate.

The heaviest operation is the first sync of a course (~40 file downloads over a few minutes, throttled). Even that is well within what normal browsing generates — a student clicking through every file in a course does the same thing, less politely.

### The one carve-out: session keepalive

Honesty requires flagging that there *is* one timer, rather than letting a reader discover it and conclude the paragraph above was marketing.

Sessions expire in roughly a day, and re-logging-in daily is the friction most likely to make this tool annoying enough to abandon. Brightspace extends a session's idle timer on activity, so one cheap request every half hour keeps it alive.

Bounded on every side:

| Property | Value |
|---|---|
| What it sends | `GET /d2l/lp/auth/xsrf-tokens` — a liveness probe, nothing else |
| What it fetches | **Nothing.** No queries, no course data, no content. |
| When it starts | Only after the first real tool call of a process |
| When it stops | After ~2 h idle, or on shutdown |
| Default | **Off** (`AVENUE_MCP_KEEPALIVE_MINUTES=0`) pending Phase 0 |

The distinction that keeps this consistent rather than a loophole: **no polling for data, ever; one session-extension request during a working session, which stops on its own when you walk away.** That is less traffic than an idle Avenue browser tab, which does exactly the same thing without asking anyone.

It ships default-off, and [`06-roadmap.md`](06-roadmap.md) makes "zero background requests when disabled" a verified exit criterion rather than a claim. A documented property nobody checked is just a hope.

---

## 5. Reliability risks

Not ethical, just things that will break.

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Session expires mid-task | **High** — daily | Low | Typed error with a clear next step; login is one command |
| McMaster changes SSO layout | Medium | High — login breaks | Wait on a post-login success signal, not form selectors ([`01`](01-authentication.md)) |
| Brightspace upgrade changes API versions | Medium | Medium | Runtime version negotiation, nothing hardcoded |
| ⚠️ routes turn out blocked | **Medium-high** | Medium | Phase 0 finds out first; documented fallbacks; degraded tools re-described honestly |
| Scanned PDFs extract empty | Medium | Low | Detected and reported, not silently indexed ([`04`](04-rag-design.md)) |
| Rate limiting appears | Low | Medium | Throttle + backoff already in place |
| Access revoked | Low | Total | No mitigation. It's their system. |

That last row is the honest bottom line: **this depends on continued access to a system we don't control, through a mechanism nobody promised us.** It could stop working. Build accordingly — don't make it load-bearing for anything with a deadline attached, and keep knowing how to use Avenue normally.

---

## 6. The submission-safety design

For the record, since Phase 5 is specced but not shipped.

**Why submission is treated differently from every other operation:** it is irreversible. Brightspace retains submission history. Submitting the wrong file, or an unfinished draft, is permanently visible to the instructor. There is no undo. Every read operation in this server can be retried, ignored, or gotten wrong at zero cost; submission cannot.

Required safeguards, all mandatory:

1. **Feature flag.** `AVENUE_MCP_ENABLE_WRITES=1`, default off. Tools **not registered** when off — invisible to the model, not merely erroring.
2. **Dry-run first.** Without `confirm: true`, returns a preview — course, assignment, due date, filename, size, SHA-256 — and submits nothing.
3. **Explicit confirmation.** `confirm` must be literally `true`. No default, no truthiness.
4. **Pre-flight checks.** File exists, under cap, folder accepts submissions, deadline status known.
5. **Late warning.** Past-due status surfaced prominently in the preview.
6. **Audit log.** Every attempt, dry-run or real, appended to `~/.avenue-mcp/logs/writes.log` with timestamp, course, folder, filename, hash.
7. **No auto-retry.** A failed submission is reported, never retried automatically — a retry storm on a submit endpoint could produce duplicates.

The two-step flow is not friction for its own sake. It ensures a human sees *what* is being submitted *where* before it happens, in a workflow where the model is otherwise acting on their behalf.

---

## Summary

**Do:**
- Use it to find, understand, and organize your own course material
- Read your course outlines' AI policies and follow them
- Keep it to your own account, on your own machine
- Ask UTS about sanctioned API access

**Don't:**
- Enable write mode without understanding submissions are irreversible
- Submit work you didn't do — the tooling is not a defense
- Share your session file, your cache, or your index — the index now contains classmates' forum posts
- Run it for anyone else, or host it as a service
- Redistribute instructor course materials
- Treat continued access as guaranteed

**Won't, by design — capabilities deliberately absent:**

| Not built | Why |
|---|---|
| Quiz questions or answers | Integrity line. Metadata only. |
| Posting to discussions | Speaking in your name to classmates |
| Instructor-side feedback or grading | Not reachable, and not a student's business |
| Full student rosters | Restricted personal information; expected `403` is the right answer |
| Author names on indexed posts | Roles carry the signal; names would make the index a record of classmates' opinions |
| Background data polling | One bounded session keepalive, default off, and nothing else |
| Hosted multi-user access | Would make you custodian of other students' live Avenue sessions |

These are decisions, not gaps. A future contributor reading this list should treat each row as a boundary that was reasoned about, and re-open it only deliberately.
