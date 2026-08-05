# 08 — API Probe Results

> **STATUS: NOT YET RUN.** This is a template. Every value below is a placeholder until Phase 0 executes.
>
> Nothing in [`02-api-surface.md`](02-api-surface.md) or [`03-mcp-tools.md`](03-mcp-tools.md) should be treated as settled until this file is filled in with real data.

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
| Test course type | TBD (e.g. a CS course with assignments + files) |
| Probe script version | TBD |

---

## A. Bootstrap

### A1 — API version discovery

```
GET /d2l/api/versions/
```

| Field | Value |
|---|---|
| Status | TBD |
| Requires auth? | TBD |
| `lp` LatestVersion | TBD |
| `le` LatestVersion | TBD |

**Response shape:**

```json
TBD
```

**Action:** these become the negotiated versions the client pins at startup.

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

### B2 — XSRF token

```
GET /d2l/lp/auth/xsrf-tokens
```

| Question | Answer |
|---|---|
| Status | TBD |
| Response shape | TBD |
| Field name carrying the token | TBD (expected `referrerToken`) |
| Also present in `localStorage` as `XSRF.Token`? | TBD |
| Required on GET requests? | TBD (expected: no) |

### B3 — Expiry behavior

**Method:** capture a session, wait for expiry (or invalidate by logging out elsewhere), then call a known-good route.

| Question | Answer |
|---|---|
| Status code on expired session | TBD (`401` / `302` / `200`) |
| If `302` — redirect target host | TBD |
| If `200` — `Content-Type` | TBD (`text/html`?) |
| Body content on expiry | TBD |
| **Is a bare status-code check sufficient?** | TBD |

If the answer to the last row is "no," the content-type + redirect heuristic in [`01-authentication.md`](01-authentication.md) is load-bearing and must be implemented exactly as described.

### B4 — Session lifetime

| Measurement | Value |
|---|---|
| XSRF token lifetime | TBD |
| Session lifetime, idle | TBD |
| Session lifetime, with periodic activity | TBD |
| Absolute cap observed | TBD |

**User-facing consequence:** TBD — "expect to log in roughly every ___".

### B5 — SSO flow

| Question | Answer |
|---|---|
| Login URL chain | TBD |
| Intermediate portal before Avenue? | TBD |
| MFA method | TBD |
| Reliable post-login success signal | TBD |
| Typical time to complete login | TBD |

The success-signal row matters: the login flow waits on it rather than on form selectors, so that an SSO redesign doesn't break login.

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
| View grades | TBD | |
| **Grade projection** | TBD | Depends on C12 |
| Announcements | TBD | |
| **Class list** | TBD | Depends on C15/C16 |
| RAG over course files | TBD | Depends on C3–C6 |

### Required doc changes

- [ ] Update status column in [`02-api-surface.md`](02-api-surface.md)
- [ ] Revise `list_assignments` in [`03-mcp-tools.md`](03-mcp-tools.md) if C7 blocked
- [ ] Revise `analyze_grade_summary` if C12 blocked
- [ ] Revise `get_class_list` per C15/C16
- [ ] Record session lifetime in [`01-authentication.md`](01-authentication.md)
- [ ] Record expiry-detection method in [`01-authentication.md`](01-authentication.md)
- [ ] Note any surprises not anticipated by the plan

### Fixtures captured

| Fixture | Route | PII redacted? |
|---|---|---|
| TBD | TBD | TBD |

These become the test corpus for Phases 1–3 ([`05`](05-architecture.md)). Saved under `tests/fixtures/`, gitignored.
