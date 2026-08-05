# 02 — API Surface

Every Valence route this server depends on. Reference: [`docs.valence.desire2learn.com`](https://docs.valence.desire2learn.com/).

## Conventions

**Base URL.** `https://avenue.mcmaster.ca`

**Path shape.** `/d2l/api/{component}/{version}/{resource}`

- `{component}` is `lp` (Learning Platform — org-wide) or `le` (Learning Environment — in-course).
- `{version}` is negotiated at startup from `GET /d2l/api/versions/`. **Never hardcode it.** The `{v}` placeholder below stands for the negotiated value.

**Auth.** Session cookies on every request; `X-Csrf-Token` additionally on non-GET. See [`01-authentication.md`](01-authentication.md).

**Trailing slashes matter.** Several Valence collection routes are documented with a trailing slash and behave differently without one. Preserve them exactly as written here.

## Status legend

| Marker | Meaning |
|---|---|
| ✅ **Verified** | Confirmed working on a McMaster student account during Phase 0 |
| 🔵 **Expected** | Documented as learner-accessible; not yet probed |
| ⚠️ **Uncertain** | Docs mark it instructor-scope. May 403, may return a filtered view. **Must be probed.** |
| ⛔ **Blocked** | Probed and denied to students |
| 🔒 **Gated** | Write operation. Specced, not shipped in v1. |

**Everything below is currently 🔵 or ⚠️.** Nothing is ✅ until Phase 0 runs. Results land in [`08-api-probe-results.md`](08-api-probe-results.md) and this table gets updated from them — not the other way around.

---

## Bootstrap

### Version discovery

```
GET /d2l/api/versions/
```

**Status:** 🔵 Expected — unauthenticated or lightly authenticated on most instances.

Returns supported version ranges per product component. Call once at startup, cache for the process lifetime, use the result to build every subsequent path.

Shape (abbreviated):

```json
[
  { "ProductCode": "lp", "LatestVersion": "1.xx", "SupportedVersions": ["1.0", "..."] },
  { "ProductCode": "le", "LatestVersion": "1.xx", "SupportedVersions": ["1.0", "..."] }
]
```

Pin to `LatestVersion` per component unless a specific route needs an older one.

### Identity

```
GET /d2l/api/lp/{v}/users/whoami
```

**Status:** 🔵 Expected. **Backs:** internal — session validation, and the identity used to filter "my" data.

Returns the calling user's identifier, name, and profile handle. This is the cheapest end-to-end proof that auth works, and Phase 1's exit criterion.

---

## Courses and enrollment

### My enrollments

```
GET /d2l/api/lp/{v}/enrollments/myenrollments/
```

**Status:** 🔵 Expected. **Backs:** `list_courses`.

The root of everything. Returns the org units the caller is enrolled in, each with its `OrgUnit.Id` (the `orgUnitId` every other route needs), name, code, and type.

Notes:
- **Paged.** Response carries a bookmark; follow it until exhausted. A student with many past terms will have more than one page.
- Includes **inactive and past** enrollments. Filter to current offerings by org-unit type (`Course Offering`) and by term/date, or courses from three years ago will pollute every result.
- The `OrgUnitTypeId` distinguishes course offerings from departments and semesters. Only offerings are useful to us.

### Course details

```
GET /d2l/api/lp/{v}/courses/{orgUnitId}
```

**Status:** 🔵 Expected. **Backs:** `list_courses` (enrichment).

Start/end dates, course code, active flag. Useful for determining "current" more reliably than name-parsing.

---

## Content

The Content section is where course files live, and therefore the input to the RAG layer.

### Content root

```
GET /d2l/api/le/{v}/{orgUnitId}/content/root/
```

**Status:** 🔵 Expected. **Backs:** `get_course_content`, `sync_course_materials`.

Returns the top-level modules of the course's content tree.

### Module structure

```
GET /d2l/api/le/{v}/{orgUnitId}/content/modules/{moduleId}/structure/
```

**Status:** 🔵 Expected. **Backs:** `get_course_content`, `sync_course_materials`.

Children of a module — nested modules and topics. Walk recursively from the root to build the full tree.

**Depth guard required.** Recursive tree-walking against a remote API is a good way to write an accidental infinite loop if the data ever contains a cycle. Cap depth (say 10) and track visited module IDs.

### Topic metadata

```
GET /d2l/api/le/{v}/{orgUnitId}/content/topics/{topicId}
```

**Status:** 🔵 Expected. **Backs:** `read_content_file`.

Title, type, URL, and — importantly — `LastModifiedDate`, which the RAG sync uses to decide whether a file needs re-indexing.

### Download a topic's file

```
GET /d2l/api/le/{v}/{orgUnitId}/content/topics/{topicId}/file
```

**Status:** 🔵 Expected. **Backs:** `read_content_file`, `sync_course_materials`.

Returns raw file bytes, not JSON. Handle accordingly:

- Read `Content-Type` and `Content-Disposition` to determine format and filename.
- **Stream to disk.** Lecture decks are routinely 20–80 MB; buffering in memory across a whole course will hurt.
- **Enforce a size cap** (default 100 MB, configurable) and skip with a logged reason rather than dragging down a sync.
- Not every topic is a file. Link topics and embedded-page topics have no downloadable body — check topic type first and skip cleanly.

---

## Assignments (dropbox)

**This is the section with the most permission uncertainty, and it matters because assignments and deadlines are a headline feature.**

### List assignment folders

```
GET /d2l/api/le/{v}/{orgUnitId}/dropbox/folders/
```

**Status:** ⚠️ **Uncertain — the Valence docs mark this Instructor-scope.**

**Backs:** `list_assignments`, and feeds `get_upcoming_deadlines`.

This is the route that returns assignment names, due dates, point values, and instructions. If a student token can call it, `list_assignments` is straightforward. If it 403s, we need the fallback below.

It is genuinely plausible that this works for students despite the docs' labelling — students obviously *can* see their assignment list in the UI, and the UI is calling this API. But "the UI can" and "this exact route can" are not the same claim, and the docs say Instructor. **Probe before promising.**

#### Fallback if blocked

Derive assignments from two other sources:

1. **Calendar events** (`.../calendar/events/myEvents/`) — assignment due dates surface as calendar items. Gives dates and titles, but not instructions or point values.
2. **Content tree** — assignment links usually appear as topics in Content, giving titles and links.

The fallback is strictly worse: no point values, no submission status, weaker instructions. `list_assignments` would degrade to a deadline list. [`03-mcp-tools.md`](03-mcp-tools.md) must describe whichever reality Phase 0 finds, and the tool description must not claim data the tool cannot return.

### Get one folder

```
GET /d2l/api/le/{v}/{orgUnitId}/dropbox/folders/{folderId}
```

**Status:** ⚠️ Uncertain — same scope question as above.

Full detail for a single assignment, including HTML instructions.

### My submissions

```
GET /d2l/api/le/{v}/{orgUnitId}/dropbox/folders/{folderId}/submissions/mysubmissions/
```

**Status:** 🔵 Expected — **explicitly documented as a Learner route.**

**Backs:** `list_assignments` (submission status).

The `mysubmissions` naming is the tell: D2L built this for students. Returns the caller's own submissions to a folder — timestamps, files, comments.

Note the asymmetry: the *learner* route for submissions is documented, while the *folder listing* is not. That's the single most important thing Phase 0 resolves, because knowing a `folderId` is a precondition for calling this route at all.

### Feedback on my submission

```
GET /d2l/api/le/{v}/{orgUnitId}/dropbox/folders/{folderId}/feedback/{entityType}/{entityId}
```

**Status:** ⚠️ Uncertain — documented as Instructor/Learner, which likely means a learner may read their own.

**Backs:** `list_assignments` (feedback surfacing).

`entityType` is `user` or `group`; `entityId` is the user or group ID. A student reading *their own* feedback is the expected legitimate use.

### 🔒 Submit work — v2, gated

```
POST /d2l/api/le/{v}/{orgUnitId}/dropbox/folders/{folderId}/submissions/mysubmissions/
```

**Status:** 🔒 Gated. Documented as a **Learner** route with `dropbox:folders:write`.

Multipart body: a JSON description part plus file data. Requires `X-Csrf-Token`.

**Not shipped in v1.** Design constraints for when it is — see [`03-mcp-tools.md`](03-mcp-tools.md) and [`07-risks-and-policy.md`](07-risks-and-policy.md): off unless `AVENUE_MCP_ENABLE_WRITES=1`, mandatory dry-run preview, explicit confirmation parameter.

### 🔒 Submission comments — v2, gated

The learner-side comment channel is the **comment field on the submission itself**, sent as part of the multipart submit body — not a separate endpoint. The standalone feedback-posting route (`POST .../feedback/...`) is **instructor-only** and out of reach.

This is a scope correction worth stating plainly: "make comments on assignments" as a student means *attaching a comment to your own submission*, not commenting on the assignment as an instructor would. If the goal was the latter, it isn't achievable on a student account by any route.

---

## Grades

### My grade values

```
GET /d2l/api/le/{v}/{orgUnitId}/grades/values/myGradeValues/
```

**Status:** 🔵 Expected — the `myGradeValues` naming again marks it learner-scoped.

**Backs:** `get_grades`, `analyze_grade_summary`.

Returns the caller's grade values for the course: points earned, points possible, weighted values, display strings.

### Grade objects (the gradebook structure)

```
GET /d2l/api/le/{v}/{orgUnitId}/grades/
```

**Status:** ⚠️ Uncertain.

**Backs:** `analyze_grade_summary`.

Returns grade *item definitions* — names, max points, weights, categories. Values alone are not enough for "what do I need on the final": that needs weights, and weights live here.

If blocked, `analyze_grade_summary` degrades to reporting only what's already graded, without projecting a final standing. Flag that limitation in the tool description rather than computing a number from incomplete weights — a confidently wrong grade projection is worse than none.

---

## Announcements (news)

```
GET /d2l/api/le/{v}/{orgUnitId}/news/
```

**Status:** 🔵 Expected. **Backs:** `list_announcements`.

Course announcements. Body is HTML — strip to text for the model, preserving links.

Filter to non-expired items and sort newest-first by default.

---

## Calendar

```
GET /d2l/api/le/{v}/{orgUnitId}/calendar/events/myEvents/
```

**Status:** 🔵 Expected — `myEvents` is learner-scoped.

**Backs:** `get_upcoming_deadlines`, and the assignment fallback.

Calendar events for the caller in a course, including assignment and quiz due dates. Accepts date-range parameters.

This route is doing double duty: it's the primary source for a unified cross-course deadline view, *and* the insurance policy if the dropbox folder listing turns out to be instructor-only. Worth probing early.

---

## Class list

```
GET /d2l/api/lp/{v}/{orgUnitId}/classlist/
```

**Status:** ⚠️ **Uncertain — documented as Instructor-scope. Realistically likely blocked for students.**

**Backs:** `get_class_list`.

Returns enrolled users with names, usernames, and emails.

Be clear-eyed here: a full student roster with email addresses is exactly the kind of data an institution restricts, and FIPPA obligations make McMaster more likely to lock it down, not less. **The probable outcome is `403`.** That is the correct outcome, and the tool should not fight it.

#### Fallback

If blocked, `get_class_list` degrades to **instructor and TA contact information** rather than a student roster — which is the part a student actually needs anyway ("who do I email about this?"). Sources:

```
GET /d2l/api/lp/{v}/enrollments/orgUnits/{orgUnitId}/users/?roleId={instructorRoleId}
```

**Status:** ⚠️ Uncertain — may carry the same restriction.

If both are blocked, the honest answer is that the tool reports instructor names from course metadata where available and otherwise says it cannot retrieve the roster. [`03-mcp-tools.md`](03-mcp-tools.md) must describe it that way. Do not ship a tool whose description promises a roster it returns an error for — that wastes a tool call and misleads the model every time.

---

## Cross-cutting concerns

### Paging

Valence collection routes use **bookmark paging**: the response includes a `PagingInfo` object with `Bookmark` and `HasMoreItems`. Pass the bookmark as a query parameter to fetch the next page; repeat until `HasMoreItems` is false.

The client implements this once, generically. A per-call-site paging loop is how you end up silently truncating a course list at page one.

**Cap total pages** (default 50) as a runaway guard.

### Rate limiting

Valence does not publish a documented per-user rate limit for session-authenticated calls, which is not the same as there being none. Treat politeness as a requirement, not an optimization:

| Control | Default |
|---|---|
| Max concurrent requests | 4 |
| Minimum inter-request delay | 100 ms |
| Retry on 429/5xx | Exponential backoff, jittered, max 3 attempts |
| Honor `Retry-After` | Yes, when present |
| Background polling | **Never.** All fetches are user-initiated. |

The RAG sync is the one operation that issues many requests at once, and it is therefore the one most likely to look abnormal. Throttle it hardest and make its progress visible.

### Error mapping

| HTTP | Meaning | Client behavior |
|---|---|---|
| `200` + `text/html` | Session expired (login page) | `SessionExpiredError` — see [`01`](01-authentication.md) |
| `302` → login host | Session expired | `SessionExpiredError` |
| `400` | Bad parameters | `InvalidRequestError`, don't retry |
| `401` | Not authenticated | `SessionExpiredError` |
| `403` | Authenticated, not permitted | `PermissionDeniedError`, don't retry |
| `404` | No such org unit / topic / folder | `NotFoundError`, don't retry |
| `429` | Rate limited | Back off, honor `Retry-After`, retry |
| `5xx` | Server-side | Backoff retry, max 3 |

The `403` vs `401` distinction is load-bearing: `403` on a healthy session is a permissions fact to report, not an auth failure to re-login for.

---

## Summary table

| Purpose | Route | Status | Tool |
|---|---|---|---|
| Version discovery | `GET /d2l/api/versions/` | 🔵 | internal |
| Identity | `GET /lp/{v}/users/whoami` | 🔵 | internal |
| My courses | `GET /lp/{v}/enrollments/myenrollments/` | 🔵 | `list_courses` |
| Course details | `GET /lp/{v}/courses/{id}` | 🔵 | `list_courses` |
| Content root | `GET /le/{v}/{id}/content/root/` | 🔵 | `get_course_content` |
| Module structure | `GET /le/{v}/{id}/content/modules/{m}/structure/` | 🔵 | `get_course_content` |
| Topic metadata | `GET /le/{v}/{id}/content/topics/{t}` | 🔵 | `read_content_file` |
| Topic file | `GET /le/{v}/{id}/content/topics/{t}/file` | 🔵 | `read_content_file` |
| Assignment folders | `GET /le/{v}/{id}/dropbox/folders/` | ⚠️ | `list_assignments` |
| One folder | `GET /le/{v}/{id}/dropbox/folders/{f}` | ⚠️ | `list_assignments` |
| My submissions | `GET /le/{v}/{id}/dropbox/folders/{f}/submissions/mysubmissions/` | 🔵 | `list_assignments` |
| My feedback | `GET /le/{v}/{id}/dropbox/folders/{f}/feedback/{et}/{ei}` | ⚠️ | `list_assignments` |
| My grades | `GET /le/{v}/{id}/grades/values/myGradeValues/` | 🔵 | `get_grades` |
| Grade structure | `GET /le/{v}/{id}/grades/` | ⚠️ | `analyze_grade_summary` |
| Announcements | `GET /le/{v}/{id}/news/` | 🔵 | `list_announcements` |
| My calendar | `GET /le/{v}/{id}/calendar/events/myEvents/` | 🔵 | `get_upcoming_deadlines` |
| Class list | `GET /lp/{v}/{id}/classlist/` | ⚠️ | `get_class_list` |
| Submit work | `POST /le/{v}/{id}/dropbox/folders/{f}/submissions/mysubmissions/` | 🔒 | v2 |

**Four ⚠️ rows and they are not evenly important.** The dropbox folder listing is the one to probe first — it gates the assignments feature entirely. The class list is the one most likely to be blocked, and the one where being blocked matters least.
