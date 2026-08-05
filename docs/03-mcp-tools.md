# 03 — MCP Tool Catalog

The tools the MCP client sees. This is the server's entire public surface.

## Design principles

**Tool descriptions are the most important text in this project.** The client model decides which tool to call based almost entirely on the description. A vague description means wrong tool calls, redundant calls, or the model answering from memory when it should have looked something up. Every description below gets a *when to use this* clause, not just a *what this does* clause — and says explicitly when **not** to use it, because the second most common failure is a model reaching for the wrong tool among near-neighbours.

**Every v1 tool is read-only, and says so.** The description states it. This lets the model call freely without hedging, and means a misfired tool call costs a round trip and nothing else.

**Tools return structured data, never prose.** No summarizing, no ranking by "importance," no natural-language rendering. The model does that. A tool that pre-summarizes throws away information the model might have needed and makes the server's behavior depend on a formatting choice nobody can see.

**Fail loudly and legibly.** Errors carry an actionable next step in plain language, because the model has to relay it to the user. `PermissionDeniedError` and `SessionExpiredError` are different situations demanding different user actions and must never be collapsed.

**One tool, one job.** `get_upcoming_deadlines` exists as a separate tool from `list_assignments` even though it could be a parameter, because "what's due soon" is the single most common question and giving it a dedicated, obviously-named tool is worth the duplication.

---

## Availability caveat

Three tools depend on routes marked ⚠️ in [`02-api-surface.md`](02-api-surface.md) — `list_assignments`, `analyze_grade_summary`, and `get_class_list`. Their final shape is **contingent on Phase 0 probe results**, and each documents its degraded mode below.

**The rule: a tool's description must never promise data the tool cannot return.** If Phase 0 shows the class list is blocked, `get_class_list` gets renamed and re-described to match what it can actually do. Shipping an aspirational description costs a wasted tool call *every single time* the model reads it and believes it.

---

## v1 — Read-only tools

### `list_courses`

> Lists your current Avenue to Learn courses with their IDs, names, course codes, and term dates. Read-only.
>
> **Use this first** in almost any Avenue task — every other course-scoped tool needs an `org_unit_id`, and this is where you get one. Also use it to answer "what am I taking this term?"
>
> By default returns only currently-active course offerings; pass `include_inactive` to also see past terms.

**Backs:** `GET /lp/{v}/enrollments/myenrollments/`, enriched with `GET /lp/{v}/courses/{id}`

| Input | Type | Default | Notes |
|---|---|---|---|
| `include_inactive` | bool | `false` | Include completed/past courses |

**Output**

```json
{
  "courses": [
    {
      "org_unit_id": 123456,
      "name": "COMPSCI 2C03 - Data Structures and Algorithms",
      "code": "COMPSCI-2C03-C01-202601",
      "start_date": "2026-01-05T05:00:00.000Z",
      "end_date": "2026-04-30T04:00:00.000Z",
      "is_active": true
    }
  ],
  "count": 5
}
```

**Notes.** Filters to org units of type *Course Offering* — enrollments also contain departments and semester containers, which are noise here. Pages through the full enrollment list; a student with several years of history will span multiple pages.

---

### `get_course_content`

> Returns the Content section tree for a course — modules (folders), topics (files, links, pages), file types, and sizes. Read-only.
>
> Use this to see **what materials exist** in a course, or to locate a specific file before reading it. This returns *structure only, not file contents* — to read a file's text, call `read_content_file`; to search across files by meaning, call `search_course_materials`.

**Backs:** `GET /le/{v}/{id}/content/root/` + recursive `.../modules/{m}/structure/`

| Input | Type | Default | Notes |
|---|---|---|---|
| `org_unit_id` | int | required | From `list_courses` |
| `max_depth` | int | `10` | Recursion guard |

**Output**

```json
{
  "org_unit_id": 123456,
  "modules": [
    {
      "id": 987,
      "title": "Week 1 - Introduction",
      "topics": [
        {
          "id": 4455,
          "title": "Course Outline",
          "type": "File",
          "file_name": "2C03_outline_W26.pdf",
          "mime_type": "application/pdf",
          "last_modified": "2026-01-04T18:22:11.000Z",
          "is_downloadable": true
        }
      ],
      "modules": []
    }
  ],
  "topic_count": 42
}
```

**Notes.** `is_downloadable` is false for link and embedded-page topics — the model should not attempt `read_content_file` on those, and the flag is there so it doesn't have to guess.

---

### `read_content_file`

> Downloads one file from a course's Content section and returns its extracted text. Handles PDF, DOCX, PPTX, HTML, and plain text. Read-only.
>
> Use this when you know **which specific file** you need — a course outline, one lecture deck, an assignment spec. Get the `topic_id` from `get_course_content`.
>
> If you don't know which file holds the answer, use `search_course_materials` instead — it searches across every indexed file at once and is much cheaper than reading files one at a time to find something.

**Backs:** `GET /le/{v}/{id}/content/topics/{t}` + `.../file`

| Input | Type | Default | Notes |
|---|---|---|---|
| `org_unit_id` | int | required | |
| `topic_id` | int | required | From `get_course_content` |
| `max_chars` | int | `50000` | Truncation cap |

**Output**

```json
{
  "topic_id": 4455,
  "title": "Course Outline",
  "file_name": "2C03_outline_W26.pdf",
  "mime_type": "application/pdf",
  "page_count": 8,
  "text": "COMPSCI 2C03 ...",
  "truncated": false
}
```

**Notes.** Extracted text carries page/slide markers so the model can cite precisely. `truncated` is explicit rather than silent — a model that doesn't know it got a partial document will confidently answer from the part it received. Files over the configured size cap error rather than hanging.

---

### `list_assignments`

> Lists a course's assignments with due dates, point values, instructions, and your submission status. Read-only.
>
> Use this for "what are the assignments in this course?" or "have I submitted X?" For a deadline view **across all courses**, use `get_upcoming_deadlines` instead.

**Backs:** `GET /le/{v}/{id}/dropbox/folders/` ⚠️ + `.../submissions/mysubmissions/` + `.../feedback/...` ⚠️

| Input | Type | Default | Notes |
|---|---|---|---|
| `org_unit_id` | int | required | |
| `include_submitted` | bool | `true` | Include already-submitted work |

**Output**

```json
{
  "assignments": [
    {
      "folder_id": 778,
      "name": "Assignment 3 - Graph Algorithms",
      "due_date": "2026-03-15T03:59:00.000Z",
      "points_possible": 100,
      "instructions_text": "Implement Dijkstra's ...",
      "submission_status": "submitted",
      "submitted_at": "2026-03-14T22:10:03.000Z",
      "grade": { "points": 92, "feedback_text": "Nice work on part 2 ..." }
    }
  ],
  "count": 4
}
```

**⚠️ Contingent on Phase 0.** If `GET .../dropbox/folders/` is instructor-only, this tool falls back to calendar-derived data and **loses point values, instructions, and submission status** — becoming, in effect, a per-course deadline list. If that happens, the tool gets renamed to `list_assignment_deadlines` and the description rewritten to promise only dates and titles. It must not keep advertising instructions it can no longer fetch.

---

### `get_upcoming_deadlines`

> Returns assignments, quizzes, and other dated items due within a time window, **across all your active courses**, sorted soonest-first. Read-only.
>
> This is the tool for "what's due this week?", "what's coming up?", or "am I forgetting anything?" — the most common Avenue question there is.
>
> Use `list_assignments` instead when you want the complete assignment list for **one** course, including ones already past or submitted.

**Backs:** `GET /le/{v}/{id}/calendar/events/myEvents/` across all active courses, merged with `list_assignments` data where available

| Input | Type | Default | Notes |
|---|---|---|---|
| `days_ahead` | int | `14` | Window size |
| `include_submitted` | bool | `false` | Hide things already handed in |

**Output**

```json
{
  "window_days": 14,
  "generated_at": "2026-03-01T12:00:00Z",
  "deadlines": [
    {
      "course_name": "COMPSCI 2C03",
      "org_unit_id": 123456,
      "title": "Assignment 3 - Graph Algorithms",
      "due_date": "2026-03-15T03:59:00.000Z",
      "days_until": 14,
      "type": "assignment",
      "submission_status": "not_submitted"
    }
  ],
  "count": 7
}
```

**Notes.** Fans out across every active course, so it is the most request-heavy read tool — throttled per [`02-api-surface.md`](02-api-surface.md) and cached briefly. `generated_at` is included so the model can tell the user how fresh the answer is instead of implying live data.

**Timezone matters.** Brightspace returns UTC; McMaster deadlines are set in Eastern time and typically land at 11:59 PM local, which is `03:59` or `04:59` UTC *the next day* depending on DST. Times are returned as UTC ISO-8601 with an explicit local rendering, because a tool that reports "due March 16" for a March 15 deadline is worse than one that reports nothing.

---

### `get_grades`

> Returns your grades for one course — each item's score, points possible, weight, and any instructor feedback. Read-only.
>
> Use this for "what did I get on X?" or "show me my grades in this course." For a computed standing and what-if projections, use `analyze_grade_summary`.

**Backs:** `GET /le/{v}/{id}/grades/values/myGradeValues/` + `GET /le/{v}/{id}/grades/` ⚠️

| Input | Type | Default | Notes |
|---|---|---|---|
| `org_unit_id` | int | required | |

**Output**

```json
{
  "org_unit_id": 123456,
  "course_name": "COMPSCI 2C03",
  "items": [
    {
      "name": "Assignment 3",
      "points_earned": 92,
      "points_possible": 100,
      "weight": 15.0,
      "percentage": 92.0,
      "is_graded": true,
      "feedback_text": "Nice work on part 2 ..."
    },
    { "name": "Final Exam", "is_graded": false, "weight": 40.0 }
  ]
}
```

---

### `analyze_grade_summary`

> Computes your current standing in a course: weighted average of graded work, total weight completed, weight remaining, and what you'd need on remaining items to reach a target grade. Read-only, calculation only — it does not change anything on Avenue.
>
> Use this for "how am I doing?", "what do I need on the final to get an A-?", or "which course needs attention?"
>
> If the course's grade weights aren't available, this returns what's known and says so rather than guessing.

**Backs:** same as `get_grades`

| Input | Type | Default | Notes |
|---|---|---|---|
| `org_unit_id` | int | required | |
| `target_percentage` | float | `null` | If set, computes required average on remaining work |

**Output**

```json
{
  "course_name": "COMPSCI 2C03",
  "graded_weight": 60.0,
  "remaining_weight": 40.0,
  "current_weighted_average": 88.3,
  "weights_available": true,
  "target": {
    "target_percentage": 90.0,
    "required_average_on_remaining": 92.6,
    "achievable": true
  },
  "caveats": []
}
```

**⚠️ Contingent on Phase 0.** If `GET /le/{v}/{id}/grades/` is blocked, weights are unavailable. In that case `weights_available` is `false`, the projection block is omitted entirely, and `caveats` explains why.

**This tool must never fabricate a projection from incomplete weights.** A confidently wrong "you need 74% on the final" is materially worse than "I can't compute that — the weights aren't accessible." Grade math is exactly where a plausible wrong number does real damage.

---

### `list_announcements`

> Returns recent course announcements with titles, body text, and posting dates, newest first. Read-only.
>
> Use this for "what did my prof post?", "did I miss any announcements?", or catching up after time away. Covers one course; call per course or pair with `list_courses` to sweep all of them.

**Backs:** `GET /le/{v}/{id}/news/`

| Input | Type | Default | Notes |
|---|---|---|---|
| `org_unit_id` | int | required | |
| `limit` | int | `20` | Max items |
| `since` | ISO date | `null` | Only items posted after this |

**Output**

```json
{
  "org_unit_id": 123456,
  "announcements": [
    {
      "id": 5566,
      "title": "Midterm room change",
      "body_text": "The midterm has moved to ...",
      "posted_at": "2026-02-28T14:00:00.000Z",
      "links": ["https://avenue.mcmaster.ca/..."]
    }
  ],
  "count": 3
}
```

**Notes.** Announcement bodies are HTML. Converted to clean text with links preserved as a separate array — dumping raw HTML wastes context, and stripping links loses information the user often needs.

---

### `get_class_list`

> Returns the people associated with a course — instructors and TAs, plus their contact info where Avenue exposes it. Read-only.
>
> Use this for "who teaches this?" or "who do I email about the assignment?"
>
> **Note:** McMaster restricts full student rosters. This tool returns instructor and TA information; if a complete class roster is unavailable to your account it will say so explicitly rather than returning a partial list without explanation.

**Backs:** `GET /lp/{v}/{id}/classlist/` ⚠️, falling back to role-filtered enrollments

| Input | Type | Default | Notes |
|---|---|---|---|
| `org_unit_id` | int | required | |

**Output**

```json
{
  "org_unit_id": 123456,
  "instructors": [
    { "name": "Dr. Jane Smith", "role": "Instructor", "email": "smithj@mcmaster.ca" }
  ],
  "students": [],
  "student_roster_available": false,
  "note": "Student roster is not accessible from a student account on this instance."
}
```

**⚠️ Contingent on Phase 0, and the most likely to be blocked.** Full rosters with email addresses are restricted data, and FIPPA obligations make McMaster more likely to lock this down than less. `student_roster_available: false` is the expected outcome, not a failure.

Note the description above is **already written for the restricted case** — it promises instructor info and is upfront about roster limits. If Phase 0 finds rosters available, the description gets *widened*. Writing it optimistically and narrowing later is the wrong order: it produces a tool that lies to the model until someone fixes it.

---

### `search_course_materials`

> Semantic search across the indexed files in your courses — outlines, lecture slides, assignment specs, readings. Returns matching passages **with citations** (course, file name, page or slide number). Read-only.
>
> This is the right tool for any question whose answer is *inside* course materials: "what's the late penalty in 2C03?", "what does the marking scheme say about code style?", "which lecture covered red-black trees?"
>
> Prefer this over `read_content_file` when you don't already know which file holds the answer — it searches everything at once instead of making you open files one by one.
>
> Requires the course to have been indexed first via `sync_course_materials`. If nothing is indexed, this returns an empty result telling you to sync.

**Backs:** the local vector + keyword index. See [`04-rag-design.md`](04-rag-design.md).

| Input | Type | Default | Notes |
|---|---|---|---|
| `query` | string | required | Natural-language question |
| `org_unit_id` | int | `null` | Scope to one course; omit to search all |
| `top_k` | int | `8` | Passages to return |

**Output**

```json
{
  "query": "late submission penalty",
  "results": [
    {
      "text": "Late assignments are penalized 10% per day, to a maximum of three days ...",
      "score": 0.87,
      "citation": {
        "course_name": "COMPSCI 2C03",
        "file_name": "2C03_outline_W26.pdf",
        "page": 3,
        "topic_id": 4455
      }
    }
  ],
  "count": 4,
  "indexed_courses": ["COMPSCI 2C03", "MATH 2Z03"]
}
```

**Notes.** Every result carries a citation — a retrieval tool returning unattributed text is not usable for coursework, since the user can't verify it and the model can't tell them where to look. `indexed_courses` is returned on every call so the model can immediately tell whether an empty result means "not in the materials" or "that course was never synced" — two very different answers to give a user.

---

### `sync_course_materials`

> Downloads and indexes a course's Content files so `search_course_materials` can search them. Reports what was added, updated, skipped, and why. This writes only to a **local** index — it changes nothing on Avenue.
>
> Run this once per course, then again when new materials are posted. Incremental: unchanged files are skipped.
>
> First run on a large course can take a few minutes and downloads a lot of files. Sync one course at a time unless the user asks for everything.

**Backs:** content routes from [`02-api-surface.md`](02-api-surface.md) + the local indexing pipeline

| Input | Type | Default | Notes |
|---|---|---|---|
| `org_unit_id` | int | required | Sync one course |
| `force` | bool | `false` | Re-index even if unchanged |

**Output**

```json
{
  "org_unit_id": 123456,
  "course_name": "COMPSCI 2C03",
  "files_found": 42,
  "files_indexed": 12,
  "files_skipped_unchanged": 28,
  "files_skipped_unsupported": 2,
  "chunks_created": 341,
  "errors": [
    { "file_name": "lecture_video.mp4", "reason": "unsupported file type" }
  ],
  "duration_seconds": 74
}
```

**Notes.** The one heavyweight tool. Always user-initiated — **never** on a timer, never automatically triggered by a search miss. Reports per-file errors rather than failing the whole sync, so one corrupt PDF doesn't cost you the other 41 files.

---

## v2 — Write tools (gated, not shipped)

Disabled unless `AVENUE_MCP_ENABLE_WRITES=1`. When the flag is off the tools are **not registered at all** — the model never sees them, so it can't attempt them and can't hallucinate having used them.

### `submit_assignment` 🔒

Uploads a file to an assignment folder as your submission.

**Backs:** `POST /le/{v}/{id}/dropbox/folders/{f}/submissions/mysubmissions/`

Safety design, all mandatory:

1. **Dry-run first.** Calling without `confirm: true` returns a preview — target course, assignment name, due date, file name, size, hash — and submits nothing.
2. **Explicit confirmation.** `confirm` must be `true`, set deliberately. No default, no truthy coercion.
3. **Pre-flight validation.** File exists, is under the size cap, and the folder accepts submissions.
4. **Deadline warning.** If past due, the preview says so prominently; the model is instructed to surface it before the user confirms.
5. **Audit log.** Every submission — dry-run and real — appended to a local log with timestamp, course, folder, filename, hash.

The two-step design exists because an accidental submission is not undoable. Brightspace keeps submission history; you cannot un-submit.

### `add_submission_comment` 🔒

Attaches a comment to your own submission.

**Scope correction worth stating plainly:** on a student account, the only comment channel is the comment field of your own submission, sent as part of the multipart submit body. There is no separate learner comment endpoint. Instructor-style commenting on an assignment is not achievable from a student account by any route — see [`02-api-surface.md`](02-api-surface.md).

In practice this folds into `submit_assignment` as an optional `comment` parameter rather than standing alone.

---

## Tool-to-route coverage check

Every tool maps to at least one route; every non-internal route backs at least one tool.

| Tool | Primary routes | Status |
|---|---|---|
| `list_courses` | `myenrollments`, `courses/{id}` | 🔵 |
| `get_course_content` | `content/root`, `content/modules/*/structure` | 🔵 |
| `read_content_file` | `content/topics/{t}`, `.../file` | 🔵 |
| `list_assignments` | `dropbox/folders/`, `mysubmissions`, `feedback` | ⚠️ |
| `get_upcoming_deadlines` | `calendar/events/myEvents`, (+ dropbox) | 🔵 |
| `get_grades` | `grades/values/myGradeValues`, `grades/` | 🔵 / ⚠️ |
| `analyze_grade_summary` | same as `get_grades` | ⚠️ |
| `list_announcements` | `news/` | 🔵 |
| `get_class_list` | `classlist/`, role-filtered enrollments | ⚠️ |
| `search_course_materials` | local index | n/a |
| `sync_course_materials` | content routes + local index | 🔵 |
| `submit_assignment` 🔒 | `POST .../mysubmissions/` | 🔒 |

Internal-only routes (`/d2l/api/versions/`, `users/whoami`) back no tool by design — they serve the client layer.

**Orphan check: none.** Every route in [`02-api-surface.md`](02-api-surface.md) is either consumed by a tool above or explicitly marked internal.
