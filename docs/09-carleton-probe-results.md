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
| `content/root/` | ✅ | Non-empty module tree |
| `content/topics/{id}` | ✅ | `LastModifiedDate` present → incremental sync viable |
| *(file discovery)* | ✅ | Downloadable topics found |
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
| `discussions/.../posts/` | ⬜ **not reached** | No topics to descend into. **Unverified, not denied.** |
| **`classlist/`** | ✅ **works** | Returns a full roster including `Email` and `OrgDefinedId` — see privacy note |
| `enrollments/orgUnits/{id}/users/` | ⛔ 403 | Same as McMaster |

## Things worth flagging

**1. The calendar is not a usable deadline fallback in this course.** `calendar/events/myEvents/`
is permitted but returned zero events, so assignment and quiz due dates do **not** appear
there. Identical to McMaster's result. Deadlines must come from `dropbox/folders/` and
`quizzes/`, which both work — so nothing is lost, but the documented calendar fallback is
not load-bearing at Carleton either.

**2. `discussion_posts` is unverified, not denied.** The probed course had forums but no
topics, so the posts route was never exercised. It is recorded as `"unverified"` rather than
`"permitted"` or `"denied"`. Probing a course with active discussion is the outstanding gap
here, and it gates the discussions half of the search corpus.

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
| Discussions corpus | ⬜ unverified — needs a course with actual topics |
| Class roster | Available but withheld by design (FIPPA) |

---

**Direction matters: probe → docs → profile, never the reverse.** These findings are
recorded in `INSTITUTIONS["carleton"].capabilities` in
[`institutions.py`](../src/avenue_mcp/institutions.py). Reinterpreting a 403 to preserve a
planned feature is the failure mode this document exists to prevent.
