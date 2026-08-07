# Merge notes — `main` + `manas`

Two people implemented this repo independently from the same design docs, starting from `f361c90` ("Initial buildplan"). Neither saw the other's work until the merge.

- **`main` @ `d9e9c0b`** — "MCP server core implementation". 68 files, ~12,200 lines. 17 tools, full RAG pipeline, 276 tests.
- **`manas` @ `0f61bf2`** — auth + client + 9 read-only tools, ~3,500 lines. No RAG, no tests.

This file records what survived from each and why, so nobody re-litigates a decision or accidentally reverts a fix whose reason isn't obvious from the diff.

**Result: `main` is the base. Four defects in it were fixed using corrections from `manas`, and one documentation claim was corrected. Everything else from `manas` was discarded.**

284 tests pass (276 inherited + 8 added here).

---

## Why `main` became the base

Not a close call. `main` is roughly 3.5× the code and does strictly more:

| | `main` | `manas` |
|---|---|---|
| Tools | 17 + gated write | 9 |
| RAG (extract/chunk/embed/retrieve/store/render) | ✅ | ✗ |
| Quizzes, discussions, what's-new, status | ✅ | ✗ |
| Tests | 276 | 0 |
| Live host probed | ✅ (unauthenticated) | ✗ (docs only) |

The decisive part is that last row. `main`'s author probed the real Avenue host and found three things no amount of reading documentation would have caught — including that **`avenue.mcmaster.ca` is a static Apache landing page where every `/d2l/*` path 404s**; real Brightspace is `avenue.cllmcmaster.ca`. `manas` had the wrong host baked into its config default, so **every single tool would have failed on first use.**

`main` also independently found the two environment problems `manas` found (MCP SDK 2.0 removing `FastMCP`; session-file write ordering), and fixed ten further defects in code review that `manas` never reached — path traversal in the download filename, a watermark bug silently discarding changes, TTL-cached submission status, and others. Those are documented in [`HANDOFF.md`](HANDOFF.md).

The architectures were also incompatible: `main` threads an `AppContext` through every tool; `manas` passes a `D2LClient`. Cherry-picking tool bodies between them was not possible, so overlapping files were taken wholesale from `main` and the corrections re-applied by hand.

---

## Kept from `manas` — four fixes and one correction

Each existed on `main`, was verified against D2L's Valence documentation, and is now pinned by a test in [`tests/test_merge_fixes.py`](tests/test_merge_fixes.py). All four tests were confirmed to **fail** against the unfixed code before being committed — `HANDOFF.md` warns about tests that stay green no matter how broken the code is, so they were checked against that trap.

### 1. Calendar route was called without its required parameters — *would break at runtime*

`tools/assignments.py::_calendar_events`

```python
# was — returns 400 every time
await ctx.client.get_paged("le", f"{org_unit_id}/calendar/events/myEvents/")
```

`startDateTime` and `endDateTime` are **required** on `calendar/events/myEvents/`; Valence returns `400` without them.

This is the worst of the four. That route is both the primary source for `get_upcoming_deadlines` — "what's due this week?", the most common question the product exists to answer — **and** the documented fallback when `dropbox/folders/` turns out to be instructor-only. So the failure would have surfaced as "you have no deadlines" rather than "malformed request", in every course, in both the primary and the backup path.

Now sends an explicit window (default −30d/+120d) built from `now_utc()`.

### 2. `classlist` was requested under the wrong API component — *would break at runtime*

`tools/classlist.py`

```python
# was
await ctx.client.get_paged("lp", f"{org_unit_id}/classlist/")
# now
await ctx.client.get_paged("le", f"{org_unit_id}/classlist/")
```

Classlist lives under `le` ([Valence enrollment routes](https://docs.valence.desire2learn.com/res/enroll.html)). The wrong component returns `404`, and `get_class_list`'s `except` arms would have recorded that as "roster unavailable" — permanently degrading the tool on the strength of a typo rather than a real permission boundary.

That is precisely the failure mode `docs/08` exists to prevent ("probe → docs, never docs → probe"), and it would have been invisible: the degraded output is exactly what everyone *expects* from this route, since a `403` here is the predicted outcome.

### 3. Windows had no session-file protection — *silent security gap*

`auth/filelock.py` (new) + `auth/login.py`

`main` correctly creates the file via `os.open(..., 0o600)` rather than write-then-chmod, which closes a real world-readable window **on POSIX**. On Windows, both `os.open`'s mode argument and `os.chmod` are no-ops — NTFS uses ACLs, and CPython's `chmod` there only toggles the read-only attribute. `main`'s own code hints at the gap in a debug log ("non-POSIX filesystem?") without closing it.

`session.json` is, as every doc in this repo correctly states, *equivalent to a logged-in Avenue session*. Measured on Windows before the fix:

```
NT AUTHORITY\SYSTEM:(I)(F)
BUILTIN\Administrators:(I)(F)
Manas-Lenovo\Manas:(I)(F)
```

After:

```
Manas-Lenovo\Manas:(R,W)
```

`restrict_to_owner()` runs `icacls /inheritance:r /grant:r`, returns a bool, never raises, and is inert off Windows. `describe_permissions()` exists so this is *verified* rather than assumed — a protection that silently doesn't apply is worse than a known-absent one, because it stops anyone from looking.

### 4. `zoneinfo` cannot resolve `America/Toronto` on Windows — *would break at runtime*

`util/dates.py` + `pyproject.toml`

Windows ships no system tz database, so `ZoneInfo("America/Toronto")` raises `ZoneInfoNotFoundError` unless the `tzdata` package is installed. Every deadline in the product passes through `to_local()`, so on a Windows machine nothing date-related worked at all.

Added `tzdata; platform_system == 'Windows'` and a `_zone()` wrapper that raises `ConfigError` naming the missing package — otherwise the traceback reads as a bug in the deadline code.

Neither implementer would have caught this from the other's side: `main` was developed on POSIX (`.venv/bin/`), `manas` on Windows.

### 5. The auth document's central claim was false — *documentation*

`docs/01-authentication.md`

The doc said three prior-art MCP servers "ship on this mechanism", meaning cookie authentication, and used that as evidence the approach is proven.

[`joshuasoup/d2l-mcp`](https://github.com/joshuasoup/d2l-mcp) — the only one whose source is readable — does not. Its `src/auth.ts` intercepts Brightspace's own outbound traffic to lift the frontend's `Authorization: Bearer` token, and `src/client.ts` uses that on every call. There is no cookie auth in it. Its README confirms the consequence: *"Auth tokens expire after ~1 hour but auto-refresh using the saved browser session."*

Cookie auth is still plausible and is still what this server implements — D2L's own guidance describes relying on the browser session, and it would be the better design. But it is **a bet, not a settled fact**, and the doc now says so. Two fallbacks are specified in `docs/01` (mint a bearer via `POST /d2l/lp/auth/oauth2/token`; capture one during login), along with what each implies for how often you have to log in — daily versus hourly, which is the difference between a pleasant tool and an irritating one.

---

## Discarded from `manas`

| Dropped | Superseded by | Why |
|---|---|---|
| `config.py`, `errors.py`, `client/d2l.py`, `server.py`, `__main__.py`, all `tools/*`, all `util/*` | `main`'s equivalents | Same jobs, done more thoroughly, and validated by 276 tests plus a live host probe. `main` also has the correct base URL. |
| `auth/session.py`, `auth/base.py`, `auth/login.py` | `main`'s | `main`'s liveness probe is live-verified: `xsrf-tokens` returns `200` + JSON **with no session at all**, so using it as the liveness check — which `manas` did — reports dead sessions as alive. `main` probes `whoami` and requires a JSON object. It also splits `403`+HTML (logged out) from `403`+JSON (genuinely forbidden), which `manas` collapsed into one wrong answer. |
| `auth/bearer.py` (~200 lines) | Nothing — documented instead | The *finding* was kept; the code was not. It implemented a different `AuthProvider` protocol, no caller invoked it, and no test covered it. Unwired auth code rots. `docs/01` now specifies both fallbacks so whichever the probe shows is needed can be written against `main`'s protocol. |
| `auth/oauth.py` | `session.py::OAuthAuth` | Duplicate stub. |
| `client/paging.py` | `client/d2l.py::get_paged` | Duplicate. |
| `scripts/check_tools.py` | `scripts/smoke_server.py` | Duplicate, and `main`'s checks more. |
| `scripts/selfcheck.py` | `tests/` | Duplicate coverage, and its fake client used the `D2LClient` signature — broken against the `AppContext` architecture. |
| `scripts/probe.py` (mine) | `main`'s `probe.py` + `probe_unauth.py` | `main`'s is more complete and its unauthenticated half has actually been run. **One idea was carried across in spirit:** auth-strategy detection should run *first*, since it gates everything else — noted in `docs/01`. |

`manas`'s doc edits to `02`/`03`/`05`/`06`/`08` were also dropped in favour of `main`'s, which are more extensive and corrected from live findings rather than from reading specifications. The exceptions are the classlist component and the calendar parameters, both folded into the code above.

---

## What is still unresolved

Unchanged by this merge, and still the blocker: **nobody has logged in yet.**

```bash
.venv/Scripts/python -m avenue_mcp login    # MacID + 2FA
.venv/Scripts/python -m avenue_mcp status
.venv/Scripts/python scripts/probe.py --course ORG_UNIT_ID
```

That settles which auth strategy works (§5 above), and the six ⚠️ routes in [`docs/02-api-surface.md`](docs/02-api-surface.md) that decide whether three tools ship full or degraded. Write findings into [`docs/08-api-probe-results.md`](docs/08-api-probe-results.md), then update `docs/02` **from** them.

Two of the four fixes above are unverifiable until then — they are correct per the Valence documentation, but only a live call proves it. If `list_courses` returns empty after a successful login, see the host note in [`HANDOFF.md`](HANDOFF.md#2-watch-for-this).

---

## Postscript — the login happened (2026-08-07)

**All four fixes are confirmed against the live host**, and the probe found five more defects that only real data exposes. Full results in [`docs/08-api-probe-results.md`](docs/08-api-probe-results.md); the short version:

- **Auth: cookies work.** `CookieSessionAuth` is the strategy, the browser stays a login-only step, and neither bearer fallback needs building. The §5 question is closed.
- **Calendar:** required-params fix was right, and incomplete — Valence also rejects second-precision timestamps with the *same* 400 it gives for omitting them. Added `to_utc_param()` (milliseconds).
- **Classlist under `le`:** right, and it turned "blocked" into 145 real users — which made the roster a live privacy exposure rather than a hypothetical one. `get_class_list` now withholds the student list by policy.
- **`403` + HTML does not mean "session dead".** `courses/{id}` returns exactly that on a fully live session. `main`'s content-type heuristic was derived from the anonymous probe and is wrong once you are logged in; the client now resolves ambiguous 403s with a liveness probe.
- **`mysubmissions` is 403** despite being a documented Learner route, and `list_assignments` was reporting every assignment as `not_submitted` on that silence.

290 tests pass. The remaining ⚠️ is `discussions/`, which was empty in the probed course and so is still unexercised.
