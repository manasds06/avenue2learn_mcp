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

## The path that works: riding the browser session

Brightspace's own web frontend is a JavaScript application, and when you click around Avenue it *does* call the same `/d2l/api/lp/…` and `/d2l/api/le/…` REST endpoints that Valence documents. So the endpoints are reachable from a logged-in browser context. The open question is **how those calls are authenticated**, and there are two answers.

### Two mechanisms, not one

**Cookie session auth.** Send the session cookies with the request and nothing else. D2L's own guidance for calling the API from a user agent describes exactly this — "relying on the browser session as the authentication mechanism" — with an XSRF token required on non-GET requests. If this works, it is the simplest possible client.

**Bearer token auth.** The frontend commonly attaches an `Authorization: Bearer <JWT>` header to its `/d2l/api/` calls. The token is short-lived (~1 hour) and is minted from the browser session at:

```
POST /d2l/lp/auth/oauth2/token
```

with the session cookies and an `X-Csrf-Token` header. Note that this is *not* the admin-registered OAuth of the previous section — no client ID, no secret, no Manage Extensibility. It is the session exchanging itself for a short-lived token, which is a facility the logged-in browser already has.

**Which one Avenue accepts is a Phase 0 measurement, not an assumption.** They are not mutually exclusive; the instance may accept both.

### Why this is stated as an open question

An earlier draft of this document asserted cookie auth as settled, citing three prior-art MCP servers as proof. That citation does not survive inspection — see [Prior art](#prior-art-and-what-it-actually-shows) below. The mechanism the one inspectable project actually ships is bearer capture, not cookies. Getting this wrong is expensive: it determines whether the browser is a one-time login step or an hourly runtime dependency.

### The pieces, in detail

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

**The XSRF token is v1-critical, not v2 groundwork.** It is required on **non-GET** requests, and it is tempting to conclude that a read-only v1 doesn't need it. That conclusion is wrong: minting a bearer token is itself a `POST /d2l/lp/auth/oauth2/token`, so under bearer auth the token is on the critical path from the first call. Even under cookie-only auth it earns its place as a cheap, reliable session-liveness probe.

**3. API version negotiation.** Valence routes embed a version number in the path — `/d2l/api/le/1.89/...`. Versions advance with each Brightspace release, and different instances run different versions. Do not hardcode. Call:

```
GET /d2l/api/versions/
```

which returns supported version ranges per product component (`lp`, `le`), and pin from the response at startup. Cache it; it changes only when McMaster upgrades Brightspace.

### What this does and does not grant

Calls made this way run **as you, with your permissions**. A student session sees student data. There is no privilege escalation here — the API enforces the same role checks it enforces in the UI. If you can't see a class roster in the browser, the API will not hand you one either.

This is worth being precise about because it is the crux of the ethical position in [`07-risks-and-policy.md`](07-risks-and-policy.md): this is automated access to *your own account*, not unauthorized access to anyone else's.

### Prior art, and what it actually shows

Several independent D2L MCP servers exist and work against student accounts, which establishes that *some* browser-derived mechanism is viable. It does **not** establish that cookie auth specifically is.

| Project | What it does |
|---|---|
| `joshuasoup/d2l-mcp` | Node/TS, Playwright SSO. **Captures the frontend's bearer token, does not use cookies.** |
| `RohanMuppa/brightspace-mcp-server` | Node/TS. Browser automation with MFA, AES-256-GCM session storage. Mechanism not inspectable from the published README. |
| `general-mudkip/d2l-mcp-server` | Assignments, grades, calendar, announcements, content. |

The middle column matters. `joshuasoup/d2l-mcp` is the one whose source is readable, and its `src/auth.ts` intercepts Brightspace's own outbound requests to lift the token:

```js
if (url.includes("/d2l/api/")) {
  const auth = request.headers()["authorization"];
  if (auth?.startsWith("Bearer ")) { capturedToken = auth.slice(7);
```

`src/client.ts` then sends `Authorization: Bearer ${token}` on every call. There is no cookie auth in it. Its README confirms the consequence: *"Auth tokens expire after ~1 hour but auto-refresh using the saved browser session."*

So the honest reading of the prior art is: **bearer tokens are confirmed to work; cookie auth is unconfirmed.** Phase 0 tests cookies first because it would be the better design if it works — but the fallback is the mechanism that's already known to.

D2L publishes `Brightspace/superagent-d2l-session-auth`, a first-party plugin whose job is attaching D2L session auth headers to requests. That's evidence session-based API auth is a real mechanism D2L builds on itself; it is not evidence about Avenue's specific configuration.

## Login flow

McMaster SSO involves a MacID, a password, and multi-factor authentication. MFA means **fully headless credential-stuffing is not a design goal** — it's fragile and it means storing a password. Instead:

```
  ┌─ first run, or session expired ──────────────────┐
  │                                                  │
  │  1. Launch Playwright, headed, real browser      │
  │  2. Navigate to avenue.mcmaster.ca               │
  │  3. USER logs in: MacID + password + MFA         │
  │  4. Wait for the post-login landing page         │
  │  5. Capture any Bearer token the frontend uses   │
  │  6. Persist context.storage_state() to disk      │
  │  7. Close the browser                            │
  │                                                  │
  └──────────────────────────────────────────────────┘
                       │
  ┌─ every subsequent call ───────────────────────────┐
  │                                                   │
  │  1. Load storage_state from disk                  │
  │  2. Build an httpx client with that cookie jar    │
  │  3. GET /d2l/lp/auth/xsrf-tokens  → liveness      │
  │  4. Authorize the request per the active strategy │
  │  5. If OK: call the API directly. No browser.     │
  │     If not: session is dead → re-auth             │
  │                                                   │
  └───────────────────────────────────────────────────┘
```

Step 5 of the login flow is cheap insurance: capturing the bearer costs one request listener and guarantees a working credential even if cookie auth turns out not to be accepted.

### The three strategies

All three implement `AuthProvider` (below). The server resolves one at first use and sticks with it.

| Strategy | How it authorizes | Browser needed at runtime? |
|---|---|---|
| `CookieSessionAuth` | Cookie jar only | No |
| `BearerTokenAuth` | Mints a bearer from cookies via `POST /d2l/lp/auth/oauth2/token`, re-mints on expiry | No |
| `CapturedBearerAuth` | Replays the bearer lifted during login | **Yes, ~hourly** |

**Resolution order is cookies → minted bearer → captured bearer**, cheapest and most durable first. The first two both preserve the property that matters: *the browser is a login mechanism, not a runtime dependency.* Only the third breaks it, and it exists solely as the guaranteed-to-work floor, since it's what the prior art ships.

If Phase 0 finds cookie auth works, the other two become dead code paths worth keeping for other institutions' instances. If it finds cookie auth fails, the fallback already exists and no rewrite is needed. That's the whole reason for building the abstraction before knowing the answer.

### `SessionManager` responsibilities

| Responsibility | Behavior |
|---|---|
| `login()` | Launch headed Playwright, wait for user auth, capture bearer, persist `storage_state` |
| `load()` | Read persisted state; return a configured `httpx.AsyncClient` |
| `is_alive()` | Probe `/d2l/lp/auth/xsrf-tokens`; also refreshes the cached token |
| `xsrf_token()` | Return current token, fetching if absent |
| `provider()` | Resolve and cache the working `AuthProvider` per the order above |
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

**How often you re-login depends entirely on which strategy wins**, so don't promise a number until the probe returns one:

| Active strategy | Expected login cadence |
|---|---|
| `CookieSessionAuth` | Once the browser session dies — roughly daily |
| `BearerTokenAuth` | Also roughly daily; the ~1h token re-mints from cookies without a browser |
| `CapturedBearerAuth` | **Roughly hourly** — the token can only be refreshed by re-running the browser flow |

The gap between "daily" and "hourly" is the difference between a tool that's pleasant and one that's irritating, which is why resolving the strategy is Phase 0's first job rather than a detail.

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
| Contents | Playwright `storage_state` — cookies + localStorage — plus any captured bearer token |
| Permissions | Owner-only, set explicitly on write. **Mechanism differs by platform — see below.** |
| Git | The whole `~/.avenue-mcp/` directory lives outside the repo. `.gitignore` additionally covers `*.session.json`, `storage_state.json`, and `.avenue-mcp/` as a belt-and-braces guard against a stray local copy. |

**Treat this file as equivalent to your logged-in Avenue session, because that's exactly what it is.** Anyone with it can act as you on Avenue until it expires. Do not commit it, do not sync it, do not paste it in a bug report.

### Restricting the file, per platform

`os.chmod(path, 0o600)` is the obvious implementation and it is **a silent no-op on Windows** — NTFS uses ACLs, and CPython's `chmod` there only toggles the read-only attribute. Writing `chmod(0o600)` and calling the file protected would leave this project's single most sensitive artifact inheriting whatever the parent directory grants, while the code and the docs both claim otherwise.

| Platform | Mechanism |
|---|---|
| Linux / macOS | `os.chmod(path, 0o600)` |
| Windows | `icacls <path> /inheritance:r /grant:r "<current user>:F"` — break inheritance, then grant the current user only |

Verify rather than assume: `ls -l` on POSIX, `icacls session.json` on Windows. The check belongs in the Phase 1 exit criteria for exactly this reason — a protection that silently doesn't apply is worse than a known-absent one, because it stops anyone looking.

Encryption at rest (as `brightspace-mcp-server` does with AES-256-GCM) is a reasonable Phase 4 hardening item, but it is worth being honest that it buys less than it appears: the key has to live on the same machine, so it protects against casual file-browsing and backup leakage rather than a compromised account. File permissions plus not-in-git covers most of the realistic risk.

## Failure modes to expect

| Failure | Cause | Handling |
|---|---|---|
| Login page layout changed | McMaster updated SSO | Login flow waits for a *post-login* success signal (an authenticated Avenue URL), not for specific form selectors. Selector-based waits are the brittle design; avoid them. |
| MFA push not approved | User missed it | `LoginTimeoutError` with a generous window (~5 min) |
| Session dies mid-tool-call | Expiry, or admin invalidation | Typed `SessionExpiredError`, no retry storm |
| HTML returned instead of JSON | Expiry redirect | Caught by the content-type check above |
| Wrong API version in path | Hardcoded version | Prevented by `/d2l/api/versions/` negotiation |
| Cookies work in browser, 401/403 via httpx | The instance requires a bearer, not cookies | Fall through to `BearerTokenAuth`, then `CapturedBearerAuth`. This is the failure the strategy ladder exists for. |
| MFA challenges every single login | Playwright's bundled Chromium reads as an unfamiliar device to Entra ID | Launch with `channel="msedge"` and a persistent user-data-dir so the "remember this device" state survives |

## The OAuth slot

Auth is behind a provider interface so that a future credential grant is a drop-in:

```python
class AuthProvider(Protocol):
    def authorize(self, request: httpx.Request) -> httpx.Request: ...
    def is_alive(self) -> bool: ...
```

- `CookieSessionAuth`, `BearerTokenAuth`, `CapturedBearerAuth` — the three strategies described in this document.
- `OAuthAuth` — **documented, not implemented.** If McMaster ever issues a client ID and secret, this class is written, the config selects it, and *nothing in the tool layer changes*.

The interface was originally justified by the hypothetical of McMaster granting OAuth someday. It has already earned its keep for a much nearer-term reason: the cookie-vs-bearer question is unresolved, and the abstraction is what lets that be resolved by measurement instead of by guessing right in advance.

That separation is the reason this doc's conclusion is "OAuth is closed *today*" rather than "OAuth is irrelevant."

## Open questions for Phase 0

Record answers in [`08-api-probe-results.md`](08-api-probe-results.md).

**Question 0 outranks the rest and gates the design:**

0. **Does a cookie-only GET to `users/whoami` succeed?** If yes, `CookieSessionAuth` wins and the client is as simple as it gets. If no, does `POST /d2l/lp/auth/oauth2/token` mint a usable bearer from the same cookies? If that also fails, we are on `CapturedBearerAuth` and the browser is an hourly runtime dependency — which changes what the README can promise about daily use.

Then:

1. Which exact cookies does `avenue.mcmaster.ca` set, and is the full jar necessary or is the documented pair sufficient?
2. What API versions does `/d2l/api/versions/` report for `lp` and `le`?
3. How long does a session actually survive, idle and active? How long does a minted bearer last?
4. Does an expired session return `401`, a `302`, or an HTML `200`?
5. Does McMaster's SSO land on Avenue directly, or bounce through an intermediate portal that the login-success wait must account for?
6. Is a browser-like `User-Agent` required, or do requests succeed with the default `httpx` one?
7. Does Entra ID accept Playwright's bundled Chromium, or is `channel="msedge"` needed to avoid an MFA challenge on every run?
