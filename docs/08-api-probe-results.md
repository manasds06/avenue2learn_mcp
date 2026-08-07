# 08 — API Probe Results

> **STATUS: COMPLETE (2026-08-07).** Both halves have now been run against a
> real McMaster student account on `avenue.cllmcmaster.ca`.
>
> - Credential-free half: `scripts/probe_unauth.py` (2026-08-05)
> - Authenticated half: `avenue-mcp login` → `scripts/probe.py --course <id>` (2026-08-07)

---

## Authenticated probe — 2026-08-07

Account: McMaster student, 21 active course offerings (74 enrollments total).
Probe course: a 200-level SFWRENG offering with real assignments, grades, and announcements.

**Pick a course with actual content.** The probe's default — first active enrollment — landed on a shared first-year MATH shell where `dropbox/folders/`, `grades/`, `quizzes/`, and `discussions/` all returned `[]`. Empty is not the same as permitted, and reading "OK" off that run would have been a false positive on four routes. Pass `--course` explicitly.

### Auth: **cookies work.** `CookieSessionAuth` is the strategy.

| Question | Answer |
|---|---|
| Cookies after login | **25**, including `d2lSessionVal` and `d2lSecureSessionVal` |
| Other cookies | Entra/SAML (`ESTSAUTH*`, `esctx*`, `SimpleSAMLAuthToken`), affinity (`buid`, `uaid`, `x-ms-gateway-slice`) — carry the whole jar |
| Cookie-only `GET users/whoami` | **200 + JSON.** No `Authorization` header needed. |
| XSRF token obtainable | Yes |
| **Verdict** | **`CookieSessionAuth`** — the browser is a login-only step, not a runtime dependency |

This settles the open question in [`01-authentication.md`](01-authentication.md). Neither bearer fallback is needed; do not build them. Expect to re-login roughly daily, not hourly.

### Route results

| Route | Result | Notes |
|---|---|---|
| `users/whoami` | ✅ | |
| `enrollments/myenrollments/` | ✅ | 74 entries, 21 active offerings |
| **`courses/{id}`** | ⛔ **403** | Blocked even on a healthy session. Course names come from `myenrollments` instead. |
| `content/root/`, module structure, topic metadata | ✅ | `LastModifiedDate` present → incremental sync works |
| **`dropbox/folders/`** | ✅ | 4 folders with `DueDate`, `Assessment`, `CustomInstructions` |
| `dropbox/folders/{id}` | ✅ | |
| **`dropbox/.../mysubmissions/`** | ⛔ **403** | *Documented as a Learner route, and blocked anyway.* Submission status is unavailable. |
| **`grades/values/myGradeValues/`** | ✅ | 18 items, with `WeightedNumerator`/`WeightedDenominator` |
| **`grades/`** (structure) | ✅ | 17 objects, **weights present** |
| `news/` | ✅ | 38 announcements |
| `calendar/events/myEvents/` | ✅ | *See the parameter trap below.* Returned 0 events — this course puts nothing on the calendar. |
| `quizzes/` | ✅ | 1 quiz with dates |
| `quizzes/{id}/attempts/` | ⛔ 403 | Quiz *attempt* status unavailable; the quiz itself is readable |
| `discussions/forums/` | ✅ | empty in this course |
| **`classlist/`** | ✅ **works** | **145 users with names, emails, usernames, OrgDefinedIds** — see the privacy note below |
| `enrollments/orgUnits/{id}/users/` | ⛔ 403 | |

### Three traps this probe walked into

**1. `classlist` is under `le`, not `lp`.** Under `lp` it returns `404`, which the tool recorded as "roster unavailable" — a permission conclusion drawn from a wrong path. Corrected, and the route then returned 145 users.

**2. `calendar/events/myEvents/` needs milliseconds in its timestamps.** It requires `startDateTime`/`endDateTime`, and rejects the second-precision form with the *same* 400 it gives for omitting them entirely:

```
...T13:34:45Z      -> 400 Invalid Parameters
...T13:34:45.000Z  -> 200
```

Use `util.dates.to_utc_param()` for outbound query params; `to_utc_iso()` is for rendering and storage.

**3. `403` + `text/html` does NOT mean the session is dead.** The credential-free probe found anonymous requests return 403+HTML, so the client mapped that to `SessionExpiredError`. But `courses/{id}` returns **403 + `text/html` + body `Forbidden`** on a fully live session, while `whoami` returns 200 JSON on that same session. The old heuristic told the user to log in again against a wall that will never move — and because `SessionExpiredError` is an `AuthError` rather than an `APIError`, it also slipped past every `except PermissionDeniedError` degradation arm in `tools/`. The client now defers the 403 and resolves it with a liveness probe.

### ⚠️ Privacy: the roster is available, and is deliberately withheld

`docs/02` predicted `403` here and `docs/07` says a roster is FIPPA-protected personal information. The route **works**: one course returned 145 classmates with full names, McMaster email addresses, usernames, and student-facing IDs.

Both implementations of `get_class_list` were written for the blocked case and would have passed that array straight back — meaning every call shipped a third of a lecture hall's personal information to whatever model provider the MCP client uses.

`get_class_list` now returns course staff in full, `students: []`, and a `student_count`, with a note explaining the omission so an empty array is never read as "no classmates found". This is a deliberate policy choice, pinned by tests, and consistent with `docs/07`'s statement that the tool "is designed to return instructor contacts".

### Field-shape findings (only visible with real data)

**`classlist` reports roles in `ClasslistRoleDisplayName`.** Not `Role`, `RoleName`, `RoleDisplayName`, or `RoleAlias` — none of which are present. Observed: `Instructor` ×1, `TA 1` ×10, `Student` ×134 (`RoleId` 104/106/105). With that field missing from the lookup, every entry normalized to `Unknown`, all 145 landed in `students`, `instructors` came back empty — and the empty-instructors branch then fired the enrollments fallback, whose 403 overwrote the note with "the class list is not available" about a route that had just returned 145 rows. One missing key produced a wrong answer *and* a wrong explanation for it.

**`classlist` leaves `Email` empty for staff.** Populated: `DisplayName`, `FirstName`, `LastName`, `Username`, `Identifier`, `ClasslistRoleDisplayName`, `RoleId`, `IsOnline`. Empty: `Email`, `OrgDefinedId`, `Pronouns`, `LastAccessed`. `Username` is present and McMaster addresses are conventionally `<macid>@mcmaster.ca`, but composing one would be a guess presented as a contact detail, so `get_class_list` reports the gap instead.

**Submission status is not knowable on this instance.** `mysubmissions` 403s, and `list_assignments` defaulted every record to `not_submitted`, upgrading only on a successful read — so a blocked route silently became "you have not submitted this" for every assignment in the course. Now `unknown`, with `submission_status_available: false` and a note telling the model not to assert either way.

**The gradebook's declared weights sum to 120%, not 100%.** `analyze_grade_summary` detects this and refuses to project — correct, since it means the course uses a rule the API does not expose (dropped lowest, bonus items, nested category weights). A readable route is not the same as sound arithmetic.

### Feature viability

| Feature | Viable? | Notes |
|---|---|---|
| List courses | ✅ | Names from `myenrollments`; `courses/{id}` is blocked |
| Browse + read content | ✅ | |
| Assignments | ✅ **full** | Due dates, points, instructions — **but no submission status** (`mysubmissions` 403) |
| Deadlines | ✅ | From `dropbox/folders/`. The calendar was empty in this course, so it is not a reliable primary source here. |
| Grades | ✅ | |
| **Grade projection** | ✅ | Weights present — projection is computable |
| Announcements | ✅ | |
| Quizzes | ✅ dates only | Attempt status blocked |
| Class roster | ⚠️ available, **withheld by policy** | |
| RAG over content | ✅ | |

---

## Credential-free findings (2026-08-05)

> Run `scripts/probe_unauth.py` to reproduce.

## ⚠️ Headline finding: the base URL in the original plan was wrong

**`avenue.mcmaster.ca` is not Brightspace.** It is a static Apache landing page; every `/d2l/*` path returns a generic Apache 404 there. The real Brightspace host is:

```
https://avenue.cllmcmaster.ca
```

confirmed by `GET /d2l/api/versions/` returning live Valence JSON. The landing page gave it away in its own "Browser Check" link.

Consequences, all now applied in code:

| Item | Was | Now |
|---|---|---|
| `AVENUE_MCP_BASE_URL` default | `https://avenue.mcmaster.ca` | `https://avenue.cllmcmaster.ca` |
| Login entry point | same as base URL | `AVENUE_MCP_LOGIN_URL` = `https://avenue.mcmaster.ca/login.php` |
| Login success detection | one host | three-host chain (see B5) |

Had this not been caught, **every single tool would have failed with `NotFoundError`** on first use.

## ⚠️ Second finding: the liveness probe was unsuitable

`GET /d2l/lp/auth/xsrf-tokens` returns **`200` with `application/json` even with no session at all.**

The original design used it as the session liveness probe, which means `is_alive()` would report a dead session as alive, `require_session()` would pass, and every real call after it would fail confusingly — precisely the failure mode the error taxonomy exists to prevent.

**Fixed:** the probe is now `GET /d2l/api/lp/1.0/users/whoami`, which is a real discriminator (anonymous → `403` + HTML; authenticated → `200` + JSON), and `is_alive()` requires *both* a 200 *and* a JSON object body.

## ⚠️ Third finding: `403` needed splitting

An anonymous request to an API route returns **`403` with a `text/html` body** — Brightspace's sign-in wall.

The client mapped every `403` to `PermissionDeniedError`, whose message says *"this is not a login problem, so signing in again will not help."* For a logged-out user that is exactly backwards.

**Fixed:** `403` + HTML → `SessionExpiredError` (auth wall); `403` + JSON → `PermissionDeniedError` (authenticated but genuinely not allowed). Both are pinned by tests in `tests/test_live_findings.py`.

## How to use this document

1. Run `scripts/probe.py` (Phase 0 — see [`06-roadmap.md`](06-roadmap.md)).
2. Fill in every `TBD` below with what actually happened.
3. Update the status column in [`02-api-surface.md`](02-api-surface.md) **from these results**.
4. Revise [`03-mcp-tools.md`](03-mcp-tools.md) for any tool whose backing route turned out blocked.

**Direction matters: probe → docs, never docs → probe.** If a route is blocked, the tool description changes to match reality. Reinterpreting a `403` to preserve a planned feature is the failure mode this document exists to prevent.

## Redaction rules

This file will be committed. Before writing anything into it:

- ❌ No real `orgUnitId` values → use `<ORG_UNIT_ID>`
- ❌ No names, MacIDs, student numbers, or email addresses
- ❌ No cookie values, session tokens, or XSRF tokens
- ❌ No actual grades
- ✅ Response *shapes* and field names — yes, that's the point
- ✅ Status codes, counts, timings — yes

Raw responses go in `tests/fixtures/`, which is **gitignored**. Only the redacted summary lives here.

---

## Run metadata

| Field | Value |
|---|---|
| Date run | TBD |
| Instance | `https://avenue.mcmaster.ca` |
| Account role | Student |
| Courses enrolled (active) | TBD |
| Test course type | TBD (e.g. a CS course with assignments + files + an active forum) |
| Probe script version | TBD |

---

## Step 0a — existing D2L MCP server against Avenue

**Run this first.** Twenty minutes, validates the entire auth premise before any code gets written. See [`06-roadmap.md`](06-roadmap.md).

| Field | Value |
|---|---|
| Server tried | TBD (`RohanMuppa/brightspace-mcp-server`) |
| Version | TBD |
| Login succeeded? | TBD |
| MFA handled automatically? | TBD |
| Listed courses? | TBD |
| Other tools that worked | TBD |
| Failure mode, if any | TBD |

**Verdict:** ⬜ Cookie-session auth works at McMaster · ⬜ Login blocked · ⬜ Auth works but permissions tighter than expected

**What this changes:** TBD — if login failed, describe exactly where in the Entra chain it broke, since our login flow has to handle it.

---

## A. Bootstrap

### A1 — API version discovery ✅ VERIFIED

```
GET https://avenue.cllmcmaster.ca/d2l/api/versions/
```

| Field | Value |
|---|---|
| Status | **200** |
| Requires auth? | **No** — fully unauthenticated |
| Content-Type | `application/json; charset=UTF-8` |
| `lp` LatestVersion | **1.62** (supported … 1.59–1.62) |
| `le` LatestVersion | **1.96** (supported … 1.93–1.96) |
| Product components | 15 (`bas`, `bfp`, `campusLife`, `customization`, …, `le`, `lp`) |
| Browser-like UA required? | **No** — default `httpx` UA also gets 200 |

**Response shape (confirmed live):**

```json
[
  {"ProductCode": "lp", "LatestVersion": "1.62", "SupportedVersions": ["1.0", "…", "1.62"]},
  {"ProductCode": "le", "LatestVersion": "1.96", "SupportedVersions": ["1.0", "…", "1.96"]}
]
```

**Action taken:** version negotiation parses exactly this shape and pins `lp=1.62` / `le=1.96`. The `1.0` fallbacks stay as a safety net — `1.0` is in `SupportedVersions` for both, so a fallback cannot 404.

Worth noting how far a hardcoded guess would have been off: the plan's illustrative `le` version was `1.89`; the instance runs `1.96`. This is the concrete argument for negotiating rather than pinning.

---

### A2 — Identity

```
GET /d2l/api/lp/{v}/users/whoami
```

| Field | Value |
|---|---|
| Status | TBD |
| Fields returned | TBD |

**Response shape (redacted):**

```json
TBD
```

---

## B. Authentication behavior

**The most operationally important section** — it determines how the client detects expiry, which is the difference between a clear error and a confusing JSON parse failure.

### B1 — Cookies

| Question | Answer |
|---|---|
| Cookie names set after login | TBD |
| Is `d2lSessionVal` present? | TBD |
| Is `d2lSecureSessionVal` present? | TBD |
| Other D2L cookies observed | TBD |
| Load-balancer / affinity cookies? | TBD |
| **Full jar required, or just the documented pair?** | TBD |

### B2 — XSRF token ⚠️ PARTIALLY VERIFIED — and it broke an assumption

```
GET https://avenue.cllmcmaster.ca/d2l/lp/auth/xsrf-tokens
```

| Question | Answer |
|---|---|
| Status, **unauthenticated** | **200** |
| Content-Type, unauthenticated | **`application/json; charset=UTF-8`** |
| Field name carrying the token | TBD (needs a session to inspect the body) |
| Also in `localStorage` as `XSRF.Token`? | TBD |
| Required on GET requests? | TBD (still expected: no) |
| **Usable as a liveness probe?** | **NO — see below** |

**This route answers 200 + JSON with no session at all**, so it cannot distinguish a live session from a dead one. The original design used it as exactly that, which would have made `is_alive()` return `True` for an expired session.

**Action taken:** liveness moved to `GET /d2l/api/lp/1.0/users/whoami`, and `is_alive()` now requires a 200 *and* a JSON object body. The XSRF route is still used to fetch the token for the (gated) write path.

### B3 — Unauthenticated / expiry behavior ✅ VERIFIED (no-session case)

**Method:** call API routes on the Brightspace host with an empty cookie jar. This is the *no-session* case; the *expired-session* case still needs a real session to confirm it behaves identically.

| Route | Status | Content-Type |
|---|---|---|
| `/d2l/api/lp/1.0/users/whoami` | **403** | **`text/html; charset=utf-8`** |
| `/d2l/lp/auth/xsrf-tokens` | 200 | `application/json` |
| `/d2l/home` | 200 | `text/html` |

| Question | Answer |
|---|---|
| Status code with no session | **403**, not 401, and **not** a redirect |
| Redirect to SSO? | **No** — 0 hops; the host serves 403/200 directly |
| **Is a bare status-code check sufficient?** | **No** |

**Two things follow, and both are now implemented:**

1. **`403` is ambiguous and must be split by content type.** `403` + HTML is the sign-in wall (→ `SessionExpiredError`); `403` + JSON is a genuine permission denial (→ `PermissionDeniedError`). Mapping all `403`s to permission-denied told logged-out users that signing in would not help.

2. **The redirect-based heuristic is not load-bearing here** — this host does not bounce anonymous API calls to SSO. The redirect and HTML-200 checks stay in the client as cheap defensive breadth (other D2L instances do behave that way), but the *actual* discriminator on this instance is `403` + content type.

**Still TBD:** whether an *expired* session behaves identically to *no* session. If it returns `401` or a `302` instead, the existing checks already cover it.

### B4 — Session lifetime and keepalive viability

| Measurement | Value |
|---|---|
| XSRF token lifetime | TBD |
| Session lifetime, idle | TBD |
| Session lifetime, with periodic activity | TBD |
| Absolute cap observed | TBD |

**User-facing consequence:** TBD — "expect to log in roughly every ___".

#### Keepalive decision

**Method:** capture a session, issue `GET /d2l/lp/auth/xsrf-tokens` every ~20 minutes, and see whether the session outlives its measured idle window.

| Question | Answer |
|---|---|
| **Does activity extend the idle timer?** | TBD |
| If yes — how long does an actively-pinged session survive? | TBD |
| **Is there an absolute cap activity cannot extend?** | TBD |
| Does the ping route itself count as activity? | TBD |

**Verdict:** ⬜ Ship keepalive (set a non-zero default) · ⬜ Keep it default-off · ⬜ Drop it — activity doesn't extend anything

If there's an absolute cap, say so in [`01-authentication.md`](01-authentication.md): keepalive then buys hours, not days, and users should know which.

### B5 — SSO flow ✅ VERIFIED (chain), TBD (completion)

`GET https://avenue.mcmaster.ca/login.php` → **302** to:

```
https://login.microsoftonline.com/44376307-b429-42ad-8c25-28cd496f4772/saml2
  ?SAMLRequest=...
  &RelayState=https%3A%2F%2Favenue.mcmaster.ca%2Flogin.php
```

| Question | Answer |
|---|---|
| Login URL chain | **3 hosts** — `avenue.mcmaster.ca/login.php` → `login.microsoftonline.com/<tenant>/saml2` → back via `RelayState` → `avenue.cllmcmaster.ca` |
| Protocol | **SAML 2.0** (not OIDC) |
| Identity provider | **Microsoft Entra ID**, tenant `44376307-b429-42ad-8c25-28cd496f4772` |
| Intermediate portal before Brightspace? | **Yes** — the static landing page is both entry and SAML return point |
| MFA method | TBD (needs a real sign-in) |
| Reliable post-login success signal | **Session cookies scoped to `avenue.cllmcmaster.ca` + landing on `/d2l/home` there** |
| Typical time to complete | TBD |

**This confirms the correction in [`01-authentication.md`](01-authentication.md):** the IdP is Entra, and the token it issues is a SAML assertion consumed by *Brightspace*, with `RelayState` bringing the browser back. There is no point in that chain where a third-party app's own Entra token could be substituted.

**Action taken:** the login flow now takes a separate `login_url` (the landing page) and detects success on `base_url` (the Brightspace host). Waiting on the wrong host is why a naive implementation appears to hang forever *after* a successful sign-in.

### B6 — Headers ✅ VERIFIED

| Question | Answer |
|---|---|
| Browser-like `User-Agent` required? | **No** |
| Default `httpx` UA rejected? | **No** — `/d2l/api/versions/` returns 200 either way |

A browser-like UA is still sent (harmless, and closer to what the frontend does), but nothing observed depends on it.

### B6 — Headers

| Question | Answer |
|---|---|
| Browser-like `User-Agent` required? | TBD |
| Default `httpx` UA rejected? | TBD |
| Other headers the frontend sends that matter | TBD |

---

## C. Route probe results

Fill one block per route. `⚠️` rows are the ones that determine feature viability.

### C1 — My enrollments 🔵

```
GET /d2l/api/lp/{v}/enrollments/myenrollments/
```

| Field | Value |
|---|---|
| Status | TBD |
| Total enrollments | TBD |
| Pages required | TBD |
| Paging mechanism confirmed | TBD (bookmark?) |
| Org unit types present | TBD |
| Includes past/inactive? | TBD |
| Field carrying `orgUnitId` | TBD |

**Response shape (redacted):**

```json
TBD
```

---

### C2 — Course details 🔵

```
GET /d2l/api/lp/{v}/courses/{orgUnitId}
```

| Field | Value |
|---|---|
| Status | TBD |
| Has start/end dates? | TBD |
| Has an active flag? | TBD |
| Reliable for determining "current"? | TBD |

---

### C3 — Content root 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/content/root/
```

| Field | Value |
|---|---|
| Status | TBD |
| Top-level modules | TBD |
| Includes topics inline, or modules only? | TBD |

---

### C4 — Module structure 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/content/modules/{moduleId}/structure/
```

| Field | Value |
|---|---|
| Status | TBD |
| Max nesting depth observed | TBD |
| Topic types present | TBD (File / Link / Page?) |
| How is a downloadable file identified? | TBD |

---

### C5 — Topic metadata 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/content/topics/{topicId}
```

| Field | Value |
|---|---|
| Status | TBD |
| Has `LastModifiedDate`? | TBD |
| Has file name / MIME type? | TBD |
| Has file size? | TBD |

**`LastModifiedDate` presence is what makes incremental sync possible** ([`04`](04-rag-design.md)). If absent, sync falls back to hashing every file on every run — much slower.

---

### C6 — Topic file download 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/content/topics/{topicId}/file
```

| Field | Value |
|---|---|
| Status | TBD |
| `Content-Type` set correctly? | TBD |
| `Content-Disposition` filename present? | TBD |
| Streaming works? | TBD |
| Largest file in test course | TBD |
| Behavior on a non-file topic | TBD |

---

### C7 — ⚠️ Assignment folders — **CRITICAL**

```
GET /d2l/api/le/{v}/{orgUnitId}/dropbox/folders/
```

**This single result determines whether the assignments feature is buildable as designed.**

| Field | Value |
|---|---|
| **Status** | **TBD** |
| If 403 — error body | TBD |
| If 200 — folders returned | TBD |
| Due dates present? | TBD |
| Point values present? | TBD |
| Instructions (HTML) present? | TBD |
| Are all folders visible, or a filtered set? | TBD |

**Verdict:** ⬜ Works for students · ⬜ Blocked · ⬜ Partial

**If blocked — consequences:**
- `list_assignments` falls back to calendar-derived data
- Loses point values, instructions, submission status
- Tool renamed to `list_assignment_deadlines`, description rewritten
- Update [`03-mcp-tools.md`](03-mcp-tools.md) accordingly

---

### C8 — ⚠️ Single assignment folder

```
GET /d2l/api/le/{v}/{orgUnitId}/dropbox/folders/{folderId}
```

| Field | Value |
|---|---|
| Status | TBD |
| Same permission behavior as C7? | TBD |

---

### C9 — My submissions 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/dropbox/folders/{folderId}/submissions/mysubmissions/
```

| Field | Value |
|---|---|
| Status | TBD |
| Submission timestamps present? | TBD |
| Attached files listed? | TBD |
| Comments present? | TBD |
| **Reachable without C7?** | TBD |

The last row matters: if C7 is blocked, a `folderId` has to come from somewhere else (calendar, content tree) before this route is callable at all.

---

### C10 — ⚠️ Feedback on my submission

```
GET /d2l/api/le/{v}/{orgUnitId}/dropbox/folders/{folderId}/feedback/{entityType}/{entityId}
```

| Field | Value |
|---|---|
| Status (own feedback) | TBD |
| `entityType` value used | TBD |
| Grade present? | TBD |
| Feedback text present? | TBD |

---

### C11 — My grade values 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/grades/values/myGradeValues/
```

| Field | Value |
|---|---|
| Status | TBD |
| Items returned | TBD |
| Points earned / possible present? | TBD |
| Weighted values present? | TBD |
| Feedback text present? | TBD |
| Ungraded items included? | TBD |

**Response shape (redacted — no real grades):**

```json
TBD
```

---

### C12 — ⚠️ Grade structure — **CRITICAL for projections**

```
GET /d2l/api/le/{v}/{orgUnitId}/grades/
```

| Field | Value |
|---|---|
| **Status** | **TBD** |
| **Weights present?** | **TBD** |
| Max points present? | TBD |
| Categories present? | TBD |

**Verdict:** ⬜ Works · ⬜ Blocked

**If blocked:** `analyze_grade_summary` sets `weights_available: false`, omits the projection block entirely, and explains why in `caveats`. **It must not compute a projection from partial weights** — see [`03`](03-mcp-tools.md). A confidently wrong "you need 74% on the final" is the worst output this project could produce.

---

### C13 — Announcements 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/news/
```

| Field | Value |
|---|---|
| Status | TBD |
| Items returned | TBD |
| Body format | TBD (HTML?) |
| Posted date present? | TBD |
| Expired items included? | TBD |

---

### C14 — Calendar / my events 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/calendar/events/myEvents/
```

**Doing double duty: primary deadline source *and* the C7 fallback.**

| Field | Value |
|---|---|
| Status | TBD |
| Events returned | TBD |
| **Do assignment due dates appear here?** | TBD |
| Do quiz due dates appear? | TBD |
| Date-range parameters work? | TBD |
| Enough to identify the source assignment? | TBD |
| Timezone of returned dates | TBD (expected UTC) |

The bolded row is the fallback test. If assignment due dates *don't* appear here and C7 is blocked, the deadline feature has no data source and needs rethinking.

---

### C14b — ⚠️ Quizzes

```
GET /d2l/api/le/{v}/{orgUnitId}/quizzes/
```

| Field | Value |
|---|---|
| **Status** | **TBD** |
| Quizzes returned | TBD |
| `StartDate` present? | TBD |
| `DueDate` present? | TBD |
| `EndDate` present? | TBD |
| Attempts-allowed present? | TBD |

**Verdict:** ⬜ Works · ⬜ Blocked

**Do quiz due dates also appear in C14 (calendar)?** TBD — this is the fallback, and it matters more than the route itself. Deadlines survive a blocked quizzes route only if the calendar carries them.

### C14c — ⚠️ My quiz attempts

```
GET /d2l/api/le/{v}/{orgUnitId}/quizzes/{quizId}/attempts/
```

| Field | Value |
|---|---|
| Status | TBD |
| Own attempts visible? | TBD |
| Scores included where released? | TBD |

### C14d — Discussion forums 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/discussions/forums/
```

| Field | Value |
|---|---|
| Status | TBD |
| Forums returned | TBD |

### C14e — Forum topics 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/discussions/forums/{forumId}/topics/
```

| Field | Value |
|---|---|
| Status | TBD |
| Topics returned | TBD |
| Post counts present? | TBD |
| Last-post timestamp present? | TBD |

### C14f — Thread posts 🔵

```
GET /d2l/api/le/{v}/{orgUnitId}/discussions/forums/{forumId}/topics/{topicId}/posts/
```

**The RAG corpus's second source. Shape matters here.**

| Field | Value |
|---|---|
| Status | TBD |
| Posts returned | TBD |
| Paged? | TBD |
| **`ParentPostId` present?** | TBD — needed to reconstruct the reply tree |
| Body format | TBD (HTML?) |
| **Is author *role* distinguishable from author *name*?** | TBD |
| Field carrying role, if any | TBD |
| Post IDs monotonic? | TBD — determines whether `max_post_id` works as an incremental watermark |

**Redaction reminder: do not paste real post text or author names into this file.** Record field names and shapes only.

Two rows are load-bearing for [`04-rag-design.md`](04-rag-design.md): without `ParentPostId`, question+reply chunking isn't possible and threads must be chunked flat. If **role** is not directly available and only names are, that's a design problem — the index deliberately stores roles and not names, so the role has to be derivable (cross-reference against instructor enrollments, or fall back to no attribution at all).

### C15 — ⚠️ Class list

```
GET /d2l/api/lp/{v}/{orgUnitId}/classlist/
```

| Field | Value |
|---|---|
| **Status** | **TBD** (expected `403`) |
| If 200 — users returned | TBD |
| Emails included? | TBD |
| Roles distinguishable? | TBD |

**Verdict:** ⬜ Works · ⬜ Blocked

**A `403` here is the expected and correct outcome.** Full student rosters with email addresses are restricted personal information under FIPPA. If this *does* return data, note it and handle the data carefully — do not export or persist it ([`07`](07-risks-and-policy.md)).

---

### C16 — ⚠️ Role-filtered enrollments (classlist fallback)

```
GET /d2l/api/lp/{v}/enrollments/orgUnits/{orgUnitId}/users/?roleId={instructorRoleId}
```

| Field | Value |
|---|---|
| Status | TBD |
| Role IDs discoverable? | TBD |
| Instructor names returned? | TBD |
| Instructor emails returned? | TBD |

If both C15 and C16 are blocked, `get_class_list` reports instructor names from course metadata where available and otherwise states plainly that the roster is inaccessible.

---

## D. Cross-cutting behavior

### D1 — Paging

| Question | Answer |
|---|---|
| Mechanism | TBD (bookmark?) |
| Field carrying the cursor | TBD |
| "More items" flag field | TBD |
| Default page size | TBD |
| Configurable? | TBD |
| Routes observed to page | TBD |

### D2 — Rate limiting

| Question | Answer |
|---|---|
| Any `429` observed? | TBD |
| At what request rate? | TBD |
| `Retry-After` present? | TBD |
| `X-RateLimit-*` headers? | TBD |
| Rate that felt safe | TBD |

**Probe politely.** Do not deliberately hammer the API to find the limit — that's the exact behavior [`07`](07-risks-and-policy.md) commits to avoiding. Record what's observed at normal rates.

### D3 — Error shapes

| Status | Body shape | Notes |
|---|---|---|
| `400` | TBD | |
| `403` | TBD | |
| `404` | TBD | |

---

## E. Summary and decisions

**Fill in after the run.**

### Route status roll-up

| Route | Expected | **Actual** |
|---|---|---|
| `/d2l/api/versions/` | 🔵 | TBD |
| `users/whoami` | 🔵 | TBD |
| `enrollments/myenrollments/` | 🔵 | TBD |
| `courses/{id}` | 🔵 | TBD |
| `content/root/` | 🔵 | TBD |
| `content/modules/*/structure/` | 🔵 | TBD |
| `content/topics/{t}` | 🔵 | TBD |
| `content/topics/{t}/file` | 🔵 | TBD |
| **`dropbox/folders/`** | ⚠️ | **TBD** |
| `dropbox/folders/{f}` | ⚠️ | TBD |
| `dropbox/.../mysubmissions/` | 🔵 | TBD |
| `dropbox/.../feedback/...` | ⚠️ | TBD |
| `grades/values/myGradeValues/` | 🔵 | TBD |
| **`grades/`** | ⚠️ | **TBD** |
| `news/` | 🔵 | TBD |
| `calendar/events/myEvents/` | 🔵 | TBD |
| **`quizzes/`** | ⚠️ | **TBD** |
| `quizzes/{q}/attempts/` | ⚠️ | TBD |
| `discussions/forums/` | 🔵 | TBD |
| `discussions/forums/{f}/topics/` | 🔵 | TBD |
| `discussions/.../posts/` | 🔵 | TBD |
| `classlist/` | ⚠️ | TBD |
| `enrollments/orgUnits/*/users/` | ⚠️ | TBD |

### Feature viability

| Feature | Viable as designed? | Notes |
|---|---|---|
| List courses | TBD | |
| Browse content | TBD | |
| Read course files | TBD | |
| **List assignments (full)** | TBD | Depends on C7 |
| Upcoming deadlines | TBD | Depends on C7 or C14 |
| **Quiz status** | TBD | Depends on C14b/C14c |
| Quiz deadlines | TBD | Survives via C14 even if C14b blocked |
| View grades | TBD | |
| **Grade projection** | TBD | Depends on C12 |
| Announcements | TBD | |
| **Discussions as RAG corpus** | TBD | Depends on C14d–C14f |
| Question+reply chunking | TBD | Needs `ParentPostId` (C14f) |
| Role attribution without names | TBD | Needs role derivable (C14f) |
| **Class list** | TBD | Depends on C15/C16 |
| RAG over course files | TBD | Depends on C3–C6 |
| Page rendering | TBD | Local-only; depends only on C6 downloads working |
| **Session keepalive** | TBD | Depends on B4 |

### Required doc changes

- [ ] Update status column in [`02-api-surface.md`](02-api-surface.md), including the priority ordering
- [ ] Revise `list_assignments` in [`03-mcp-tools.md`](03-mcp-tools.md) if C7 blocked
- [ ] Revise `analyze_grade_summary` if C12 blocked
- [ ] Revise `list_quizzes` if C14b/C14c blocked
- [ ] Revise `get_class_list` per C15/C16
- [ ] Adjust discussion chunking in [`04-rag-design.md`](04-rag-design.md) if `ParentPostId` is absent
- [ ] Decide the keepalive default in [`01`](01-authentication.md) and [`05`](05-architecture.md) per B4
- [ ] Record session lifetime in [`01-authentication.md`](01-authentication.md)
- [ ] Record expiry-detection method in [`01-authentication.md`](01-authentication.md)
- [ ] Record the Step 0a result — it's the single most reusable finding here
- [ ] Note any surprises not anticipated by the plan

### Fixtures captured

| Fixture | Route | PII redacted? |
|---|---|---|
| TBD | TBD | TBD |

These become the test corpus for Phases 1–3 ([`05`](05-architecture.md)). Saved under `tests/fixtures/`, gitignored.
