# 01 — Authentication

This is the load-bearing document. If auth doesn't work, nothing else does.

## Why the official path is closed

D2L's supported way to call Valence is **OAuth 2.0**. There are two grant types:

- **Authorization Code** — acts as a logged-in user, requires their consent. Intended for user-facing apps.
- **Client Credentials** — acts as a service account. Intended for server-to-server integrations.

Both require the same prerequisite: **the application must be registered inside Brightspace by an administrator**, using the *Manage Extensibility* admin tool. Registration issues a client ID and secret, and configures the scopes the app may request.

A student account has no access to Manage Extensibility. There is no self-service registration, no public developer portal for a specific institution's instance, and no way to mint credentials against `avenue.mcmaster.ca` without McMaster UTS creating them.

There is also a legacy path (the Keytool service, issuing App ID/Key pairs), and it has the same gate — keys are distributed only to registered third parties and to D2L's own installations.

**Conclusion:** OAuth is unavailable to us today. It remains the right long-term answer, and asking UTS about Valence API access is a reasonable thing to do — see [`07-risks-and-policy.md`](07-risks-and-policy.md). The architecture leaves a slot for it.

## The path that works: session authentication

Brightspace's own web frontend is a JavaScript application. When you click around Avenue, the page is calling the *same* `/d2l/api/lp/…` and `/d2l/api/le/…` REST endpoints that Valence documents. It authenticates those calls not with OAuth, but with **the browser session** — the cookies you already have from logging in.

That mechanism is available to us. Log in once through a real browser, keep the cookies, and call the API directly.

### The mechanism in detail

Three pieces:

**1. Session cookies.** After a successful login, Brightspace sets:

| Cookie | Notes |
|---|---|
| `d2lSessionVal` | The session value. |
| `d2lSecureSessionVal` | The `Secure`-flagged counterpart. Sent only over HTTPS. |

Both must be present on API requests. There are other D2L cookies (`d2lLocaleCookie`, load-balancer affinity cookies, etc.); the pragmatic approach is to carry **the whole cookie jar** from the browser context rather than cherry-picking, since affinity cookies can matter for routing.

**2. The XSRF token.** Because session cookies are ambient credentials, Brightspace requires CSRF protection on state-changing calls. Fetch the token from:

```
GET /d2l/lp/auth/xsrf-tokens
```

and send the returned `referrerToken` on non-GET requests as:

```
X-Csrf-Token: <token>
```

The same value is also present in browser `localStorage` under the key `XSRF.Token`, which is a useful cross-check when debugging. It expires with the session.

**Important scoping:** the XSRF token is required for **non-GET** requests only. Since v1 of this server is entirely read-only (all GETs), the token is not strictly needed yet. We fetch and manage it anyway, for two reasons: it is a cheap and reliable liveness probe for the session, and the v2 write path (`POST` a submission) will need it. Building it in now avoids a retrofit later.

**3. API version negotiation.** Valence routes embed a version number in the path — `/d2l/api/le/1.89/...`. Versions advance with each Brightspace release, and different instances run different versions. Do not hardcode. Call:

```
GET /d2l/api/versions/
```

which returns supported version ranges per product component (`lp`, `le`), and pin from the response at startup. Cache it; it changes only when McMaster upgrades Brightspace.

### What this does and does not grant

Calls made this way run **as you, with your permissions**. A student session sees student data. There is no privilege escalation here — the API enforces the same role checks it enforces in the UI. If you can't see a class roster in the browser, the API will not hand you one either.

This is worth being precise about because it is the crux of the ethical position in [`07-risks-and-policy.md`](07-risks-and-policy.md): this is automated access to *your own account*, not unauthorized access to anyone else's.

### Prior art confirming this works

Not theoretical. Three independent D2L MCP servers ship on this mechanism:

| Project | Notes |
|---|---|
| `RohanMuppa/brightspace-mcp-server` | Node/TS. Browser automation with MFA support, AES-256-GCM encrypted session storage. Read-only. |
| `joshuasoup/d2l-mcp` | Node/TS. Puppeteer SSO, persistent sessions, ~12 tools. Ships an academic-integrity disclaimer. |
| `general-mudkip/d2l-mcp-server` | Assignments, grades, calendar, announcements, content. |

D2L publishes `Brightspace/superagent-d2l-session-auth`, a first-party plugin whose entire job is attaching D2L session auth headers to requests — the mechanism is one D2L builds on itself.

## Login flow

McMaster SSO involves a MacID, a password, and multi-factor authentication. MFA means **fully headless credential-stuffing is not a design goal** — it's fragile and it means storing a password. Instead:

```
  ┌─ first run, or session expired ──────────────────┐
  │                                                  │
  │  1. Launch Playwright, headed, real browser      │
  │  2. Navigate to avenue.mcmaster.ca               │
  │  3. USER logs in: MacID + password + MFA         │
  │  4. Wait for the post-login landing page         │
  │  5. Persist context.storage_state() to disk      │
  │  6. Close the browser                            │
  │                                                  │
  └──────────────────────────────────────────────────┘
                       │
  ┌─ every subsequent call ───────────────────────────┐
  │                                                   │
  │  1. Load storage_state from disk                  │
  │  2. Build an httpx client with that cookie jar    │
  │  3. GET /d2l/lp/auth/xsrf-tokens  → liveness      │
  │  4. If OK: call the API directly. No browser.     │
  │     If not: session is dead → re-auth             │
  │                                                   │
  └───────────────────────────────────────────────────┘
```

The browser is used **only** to acquire a session. Once we have cookies, all real work goes through `httpx` — far faster, lower memory, and no browser process hanging around. Playwright is a login mechanism, not a scraping mechanism.

### `SessionManager` responsibilities

| Responsibility | Behavior |
|---|---|
| `login()` | Launch headed Playwright, wait for user auth, persist `storage_state` |
| `load()` | Read persisted state; return a configured `httpx.Client` |
| `is_alive()` | Probe `/d2l/lp/auth/xsrf-tokens`; also refreshes the cached token |
| `xsrf_token()` | Return current token, fetching if absent |
| `require_session()` | `load()` → `is_alive()` → raise a typed `SessionExpiredError` if dead |

`require_session()` **never silently launches a browser.** An MCP server runs headless under a client; popping a browser window mid-tool-call is hostile and can hang the call. Instead the tool fails with a clear, actionable message and the user re-runs the login command deliberately. See the error contract below.

## Session lifetime

Observed behavior in comparable D2L deployments:

| Thing | Rough lifetime |
|---|---|
| Auth/XSRF token | ~1 hour |
| Browser session (idle) | ~24 hours |
| Absolute session cap | Institution-configured; assume shorter than you'd like |

McMaster's exact values are unknown and are a **Phase 0 measurement** — record them in [`08-api-probe-results.md`](08-api-probe-results.md).

Practical consequence: expect to re-login roughly once a day. Design for that being a smooth, one-command operation rather than a crisis.

### Detecting expiry

Do not trust status codes alone. Brightspace, like many session-based apps, may respond to an expired session with a `302` to the login page — or with a `200` whose body is an HTML login form — rather than a clean `401`. A naive client parses that HTML as JSON, fails weirdly, and reports a confusing error.

The client must treat **all** of these as "session expired":

- `401` or `403` on a route that previously worked
- A redirect whose target matches the SSO/login host
- A `200` whose `Content-Type` is `text/html` on a route that returns JSON

and surface a single typed error, not a parse failure.

## Error contract

Auth failures must be legible to the model calling the tool, because the model is the one that has to tell the user what to do.

| Condition | Error | Message the tool returns |
|---|---|---|
| No session file | `NoSessionError` | "Not logged in to Avenue. Run `avenue-mcp login` in a terminal, complete MacID sign-in, then retry." |
| Session expired | `SessionExpiredError` | "Avenue session expired. Run `avenue-mcp login` to sign in again, then retry." |
| Login timed out | `LoginTimeoutError` | "Login window closed or timed out before sign-in completed. Run `avenue-mcp login` and complete MacID + MFA." |
| Permission denied on a live session | `PermissionDeniedError` | "Your account doesn't have access to this on Avenue (this route may be instructor-only)." |

That last one is distinct on purpose: a `403` with a *healthy* session is not an auth problem, it's a permissions fact, and telling the user to log in again would send them down a dead end. Distinguishing the two requires the liveness probe.

## Credential storage

**The MacID password is never stored, never read, and never passed through this program.** It is typed by the user into a real browser window rendering McMaster's real SSO page. The program never sees it.

What *is* persisted is the session state:

| Item | Detail |
|---|---|
| Path | `~/.avenue-mcp/session.json` (override via `AVENUE_MCP_STATE_DIR`) |
| Contents | Playwright `storage_state` — cookies + localStorage |
| Permissions | `0600`, owner-only, set explicitly on write |
| Git | The whole `~/.avenue-mcp/` directory lives outside the repo. `.gitignore` additionally covers `*.session.json`, `storage_state.json`, and `.avenue-mcp/` as a belt-and-braces guard against a stray local copy. |

**Treat this file as equivalent to your logged-in Avenue session, because that's exactly what it is.** Anyone with it can act as you on Avenue until it expires. Do not commit it, do not sync it, do not paste it in a bug report.

Encryption at rest (as `brightspace-mcp-server` does with AES-256-GCM) is a reasonable Phase 4 hardening item, but it is worth being honest that it buys less than it appears: the key has to live on the same machine, so it protects against casual file-browsing and backup leakage rather than a compromised account. File permissions plus not-in-git covers most of the realistic risk.

## Failure modes to expect

| Failure | Cause | Handling |
|---|---|---|
| Login page layout changed | McMaster updated SSO | Login flow waits for a *post-login* success signal (an authenticated Avenue URL), not for specific form selectors. Selector-based waits are the brittle design; avoid them. |
| MFA push not approved | User missed it | `LoginTimeoutError` with a generous window (~5 min) |
| Session dies mid-tool-call | Expiry, or admin invalidation | Typed `SessionExpiredError`, no retry storm |
| HTML returned instead of JSON | Expiry redirect | Caught by the content-type check above |
| Wrong API version in path | Hardcoded version | Prevented by `/d2l/api/versions/` negotiation |
| Cookies work in browser, 403 via httpx | Missing cookie, or missing headers the frontend sends | Carry the full jar; set a realistic `User-Agent`; add `X-Csrf-Token` on non-GETs |

## The OAuth slot

Auth is behind a provider interface so that a future credential grant is a drop-in:

```python
class AuthProvider(Protocol):
    def authorize(self, request: httpx.Request) -> httpx.Request: ...
    def is_alive(self) -> bool: ...
```

- `CookieSessionAuth` — the implementation described in this document.
- `OAuthAuth` — **documented, not implemented.** If McMaster ever issues a client ID and secret, this class is written, the config selects it, and *nothing in the tool layer changes*.

That separation is the reason this doc's conclusion is "OAuth is closed *today*" rather than "OAuth is irrelevant."

## Open questions for Phase 0

Record answers in [`08-api-probe-results.md`](08-api-probe-results.md).

1. Which exact cookies does `avenue.mcmaster.ca` set, and is the full jar necessary or is the documented pair sufficient?
2. What API versions does `/d2l/api/versions/` report for `lp` and `le`?
3. How long does a session actually survive, idle and active?
4. Does an expired session return `401`, a `302`, or an HTML `200`?
5. Does McMaster's SSO land on Avenue directly, or bounce through an intermediate portal that the login-success wait must account for?
6. Is a browser-like `User-Agent` required, or do requests succeed with the default `httpx` one?
