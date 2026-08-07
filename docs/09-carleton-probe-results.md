# Phase 0 probe results — Carleton Brightspace

**Instance:** `https://brightspace.carleton.ca`
**Measured:** 2026-08-07, authenticated student account
**Course probed:** one course carrying assignments, grades, quizzes, and forums (identifiers
and contents omitted)
**Brightspace API:** `lp 1.62`, `le 1.96` — identical to McMaster's

Kept separate from [`08-api-probe-results.md`](08-api-probe-results.md) deliberately. One
file holding two instances' results is precisely where "measured at McMaster" leaks into a
Carleton conclusion.

Recorded qualitatively on purpose: **which routes answer** is the finding, and exact item
counts would describe one student's enrollments, gradebook, and classmates without adding
anything a future implementer needs. Field names appear below; values never do.

---

## Headline finding: the probe's one-folder sample produced a false positive

`dropbox/.../mysubmissions/` initially looked **permitted** at Carleton, diverging from
McMaster's 403. It does not. It is **denied**, the same as McMaster.

`scripts/probe.py` samples `folders[0]`. On the probed course that position happened to hold
a leftover scratch folder, and that one folder returns **200 with `[]`**. Checking every
folder individually instead:

```
OK     1 folder    (the scratch folder)         -> 200 []
403    every other folder                       -> PermissionDeniedError
```

This is the failure mode [`08`](08-api-probe-results.md) warns about — "empty is not the same
as permitted" — arriving from the opposite direction. There, an empty course shell made
working routes look blocked. Here, one anomalous folder made a blocked route look permitted.
A single-folder sample is not evidence about a route either way.

**Two things caught it before it mattered.** First, capabilities never gate control flow:
`list_assignments` still attempted every folder and still reported
`submission_status_available: false`, because the tool discovers permissions by trying, not
by consulting the profile. Second, the mistake was visible in the *prose* — the note read
"this route normally works, so this denial is likely specific to this course," which is
exactly the confident-and-wrong sentence the capability system exists to prevent. Had the
capability been allowed to skip the call, the tool would have silently claimed submission
status was available and returned nothing.

**Consequence for probing any future school:** `mysubmissions` must be checked across
multiple folders. Sampling one is how you record a permission you have not measured.

## Second finding: the content listing omits `Url`, and that silently killed indexing

`sync_course_materials` initially reported **every file unsupported** — found them all,
indexed none, and finished in three seconds without downloading anything.

The cause is a response-shape difference, not a permission. `discover_files` walks
`content/root/` and `content/modules/{id}/structure/`, and on Carleton those listings return
topics **without a `Url` field**. `guess_filename` therefore falls back to the display title,
which carries no extension, so `is_supported()` rejects all of them. The real filename is
available — `content/topics/{id}` returns `Url: /content/enforced/.../<name>.pdf` — just not
in the listing McMaster supplies it in.

**Fix:** back-fill from the topic detail record for any topic whose name lacks a usable
extension ([`rag/sync.py`](../src/avenue_mcp/rag/sync.py), `_resolve_missing_filenames`).
Instances that already include `Url` in the listing issue no extra requests, so McMaster pays
nothing for Carleton's shape. Topics whose detail `Url` is **absolute** are left unsupported
on purpose: those are external links to publisher or syllabus sites, and following one would
download somebody else's HTML into the course index.

After the fix, on the same course: **36 of 48 files indexed, 723 chunks**, and semantic search
returns correct passages cited to file and page. The remaining 12 are genuinely not
extractable — external links, plus one image-only PDF that yields no text and is reported as
such rather than indexed empty.

**Why the test suite missed it:** the mock Brightspace in `test_integration_tools.py` includes
`Url` in its content listing, i.e. it is McMaster-shaped. Two instances is the minimum needed
to notice that a field's presence was an assumption. Regression coverage for the
`Url`-less shape is now in `tests/test_sync_filename_resolution.py`.

## Third finding: posts carry no role, so every author was "Unknown"

Carleton's discussion posts are shaped `{"PostingUserId": …, "PostingUserDisplayName": …,
"Message": {"Text": "", "Html": "…"}}`. There is no `AuthorRole`, no `Role`, no nested
`Author.Role` — so `normalize_role` returned `Unknown` for every post and
`has_instructor_replies` was **always false**.

That guts the feature. An instructor's ruling and a classmate's guess became
indistinguishable, which is most of the reason to index forums at all.

`PostingUserId` does match the classlist's `Identifier`, so the role is recoverable from the
roster. [`client/roles.py`](../src/avenue_mcp/client/roles.py) builds an id→role map once per
course and both the tool and the indexer use it. **Privacy is preserved deliberately:** the
roster is read for id→role only — names, emails, and OrgDefinedIds are discarded and never
returned, stored, or indexed, so docs/07's "roles and not names" rule still holds.

Before and after, on a 10-thread forum:

| | Before | After |
|---|---|---|
| Threads flagged as having instructor replies | 0 of 10 | **9 of 10** |
| Role distribution | `Unknown: 15` | `Instructor: 9, Student: 2, TA: 1, Unknown: 3` |

The residual `Unknown: 3` is honest: those users are no longer on the roster (dropped or
withdrawn), and inventing "Student" for them would be a guess presented as a fact.

Note that post bodies were fine all along — `Message.Text` is empty and `Message.Html`
carries the content, and the extraction already preferred `Html`.

## Fourth finding: an HTML course file was misread as an expired session

Downloading an instructor-uploaded `.html` file raised `SessionExpiredError` on a session
that was verifiably alive (57 minutes old, `whoami` 200). `_raise_for_session` treated any
`200 + text/html` on a `/d2l/api/` path as a sign-in wall — correct for JSON routes, wrong for
the file-download route, where `text/html` is just a file's content type.

This is the failure mode docs/08's third finding exists to prevent, in a path it did not
cover: the user is told to log in again against a wall that does not exist.

**Fix:** file downloads pass `expect_json=False`, and there the discriminator is
`Content-Disposition`. A real download carries `attachment; filename="…"`; a login page does
not. JSON routes keep the strict rule — HTML there is still a wall.

## Authentication

The login flow needed **no changes**. `auth/login.py` codes no form selectors, so Carleton's
ADFS chain worked unmodified where McMaster uses Microsoft Entra.

| | |
|---|---|
| Entry point | `brightspace.carleton.ca/d2l/lp/auth/saml/login` |
| IdP | ADFS at `cufed.carleton.ca` |
| Hosts involved | **one** (McMaster splits login and API across two) |
| Session cookies | `d2lSessionVal` + `d2lSecureSessionVal`, both present |
| Liveness (`users/whoami`) | 200 JSON |
| XSRF (`/d2l/lp/auth/xsrf-tokens`) | 200 JSON |

Cookie names are D2L platform constants and matched exactly. The ADFS round trip also
deposits `MSISAuth*` / `SamlSession` cookies, which are irrelevant to us but harmless.

**Anonymous behaviour is identical to McMaster:** `/d2l/home` returns 200 with **zero
redirect hops** (no bounce to SSO), and unauthenticated API routes return **403 with a
`text/html` body**. So the client's 403-splitting heuristic — 403+HTML means sign-in wall,
403+JSON means genuine permission denial — is correct here without modification.

## Route results

Field names are listed where the response shape matters. No values are recorded.

| Route | Status | Notes |
|---|---|---|
| `versions/` | ✅ | `lp 1.62`, `le 1.96` |
| `users/whoami` | ✅ | `{FirstName, Identifier, LastName, ProfileIdentifier, Pronouns, UniqueName}` |
| `enrollments/myenrollments/` | ✅ | Non-empty; paging works |
| **`courses/{id}`** | ⛔ **403** | Blocked, same as McMaster. Course names come from `myenrollments`. |
| `content/root/` | ✅ | Non-empty module tree, but **no `Url` on topics** — see second finding |
| `content/modules/{id}/structure/` | ✅ | Same omission |
| `content/topics/{id}` | ✅ | Carries `Url` **and** `LastModifiedDate` → incremental sync viable |
| `content/topics/{id}/file` | ✅ | Downloads work; files land under the per-institution cache dir |
| *(file discovery)* | ✅ | Most topics indexable once `Url` is back-filled |
| **`dropbox/folders/`** | ✅ | Non-empty, with `DueDate`, `Assessment`, `CustomInstructions` |
| `dropbox/folders/{id}` | ✅ | |
| **`dropbox/.../mysubmissions/`** | ⛔ **403** | Same as McMaster. Denied on every real assignment folder; see the headline finding. Submission status is not knowable. |
| `grades/values/myGradeValues/` | ✅ | Non-empty, with `WeightedNumerator`/`WeightedDenominator` |
| `grades/` (structure) | ✅ | Non-empty, **weights present** |
| `news/` | ✅ | Non-empty |
| `calendar/events/myEvents/` | ✅ | Readable but returned **zero events** — see below |
| `quizzes/` | ✅ | Non-empty, with dates |
| `quizzes/{id}/attempts/` | ⛔ 403 | Quiz *attempt* status unavailable; the quiz itself is readable. Same as McMaster. |
| `discussions/forums/` | ✅ | Non-empty |
| `discussions/.../topics/` | ✅ | `[]` — forums exist but carry no topics |
| `discussions/.../posts/` | ✅ | Verified on another course. Carries `Message.Html`, `ParentPostId`, `PostingUserId` — but **no role field**; see third finding |
| **`classlist/`** | ✅ **works** | Returns a full roster including `Email` and `OrgDefinedId` — see privacy note |
| `enrollments/orgUnits/{id}/users/` | ⛔ 403 | Same as McMaster |

## Things worth flagging

**1. The calendar is not a usable deadline fallback in this course.** `calendar/events/myEvents/`
is permitted but returned zero events, so assignment and quiz due dates do **not** appear
there. Identical to McMaster's result. Deadlines must come from `dropbox/folders/` and
`quizzes/`, which both work — so nothing is lost, but the documented calendar fallback is
not load-bearing at Carleton either.

**2. Most "unsupported file" reports are not files.** One course reported 4 of 55 topics
indexed, which looks broken and is not: 11 were external links, ~38 were quiz-launcher URLs
(`/d2l/lp/quizzes/…?type=quiz&rcode=…`), one was a deleted topic whose detail record 404s, and
exactly 4 were real files. All four indexed. The reason string
("unsupported file type") is misleading for a quiz launcher, since it is not a file at all --
worth improving, but it is a reporting-clarity issue rather than a functional one. Note also
that `files_found` counts topics, not files.

**3. PowerPoint rendering needs LibreOffice**, which is not installed here, so `get_page_image`
on a `.pptx` raises a clear `RenderError` pointing at `read_content_file` instead. PDF
rendering works and needs nothing extra. This is environmental and identical at McMaster --
not a Carleton finding. The error message previously told Windows and macOS users to run
`sudo apt install`; it now gives platform-correct advice.

**3. `classlist` returns a full roster, including `Email` and `OrgDefinedId`.** More than
McMaster exposes — McMaster leaves `Email` empty for staff. `get_class_list` still returns
`students: []` and `student_roster_returned: false` by design: this is FIPPA-protected
personal information, and shipping an entire class's names and email addresses to whatever
model provider the MCP client uses is not something a "who teaches this course?" question
asked for. The roster being *available* does not make returning it *appropriate*.

The probe was deliberately run **without** `--save-fixtures` for this reason: that flag would
have written the raw roster to disk. Do the same on any future school.

**4. `whoami` carries no numeric user id** — the shape is `{FirstName, Identifier, LastName,
ProfileIdentifier, Pronouns, UniqueName}`. Anything that needs a user id should use
`Identifier`.

**5. Every blocked route matches McMaster exactly:** `courses/{id}`, `mysubmissions`,
`quizzes/{id}/attempts/`, `enrollments/orgUnits/{id}/users/`. Two instances at the same
Brightspace version (`lp 1.62`, `le 1.96`) agreeing on all four is good evidence these are
D2L role defaults rather than per-institution configuration — which in turn suggests other
Ontario schools on the same version will behave the same way. Suggests, not establishes:
that is still a prediction until someone probes one.

## Feature viability

| Feature | Status |
|---|---|
| Courses + content tree | ✅ |
| Assignments | ✅ due dates, points, instructions — **but no submission status** (`mysubmissions` 403) |
| Deadlines | ✅ from `dropbox/folders/` + `quizzes/`; calendar is empty and not a fallback |
| Grades + projection | ✅ weights present |
| Announcements | ✅ |
| Quizzes | ✅ list and dates; attempt status blocked |
| Discussions corpus | ✅ verified — threads indexed and searchable, with instructor/TA attribution |
| Class roster | Available but withheld by design (FIPPA) |
| **File search (RAG)** | ✅ verified end to end — download, extract, embed, cite. Required the `Url` back-fill above. |

---

**Direction matters: probe → docs → profile, never the reverse.** These findings are
recorded in `INSTITUTIONS["carleton"].capabilities` in
[`institutions.py`](../src/avenue_mcp/institutions.py). Reinterpreting a 403 to preserve a
planned feature is the failure mode this document exists to prevent.
