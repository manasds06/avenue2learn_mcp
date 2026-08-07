# 01 — Authentication

This is the load-bearing document. If auth doesn't work, nothing else does.

> ## Verified against the live instance (2026-08-05)
>
> Three assumptions in the original draft were wrong. All are corrected below and
> pinned by tests in `tests/test_live_findings.py`; full detail in
> [`08-api-probe-results.md`](08-api-probe-results.md).
>
> | Assumption | Reality |
> |---|---|
> | Brightspace is at `avenue.mcmaster.ca` | **No.** That is a static Apache landing page; `/d2l/*` 404s. Brightspace is **`avenue.cllmcmaster.ca`** (lp 1.62, le 1.96). |
> | `/d2l/lp/auth/xsrf-tokens` can serve as the liveness probe | **No.** It returns 200 + JSON with **no session at all**. Liveness is now `/d2l/api/lp/1.0/users/whoami`, requiring 200 **and** a JSON object. |
> | A `403` means "authenticated but not permitted" | **Only when the body is JSON.** Anonymous API calls return **403 + HTML** — the sign-in wall. That now maps to `SessionExpiredError`. |
>
> The SSO chain is also confirmed: **SAML 2.0** to Microsoft Entra, tenant
> `44376307-b429-42ad-8c25-28cd496f4772`, spanning three hosts.

## Why the official path is closed

D2L's supported way to call Valence is **OAuth 2.0**. There are two grant types:

- **Authorization Code** — acts as a logged-in user, requires their consent. Intended for user-facing apps.
- **Client Credentials** — acts as a service account. Intended for server-to-server integrations.

Both require the same prerequisite: **the application must be registered inside Brightspace by an administrator**, using the *Manage Extensibility* admin tool. Registration issues a client ID and secret, and configures the scopes the app may request.

A student account has no access to Manage Extensibility. There is no self-service registration, no public developer portal for a specific institution's instance, and no way to mint credentials against `avenue.mcmaster.ca` without McMaster UTS creating them.

There is also a legacy path (the Keytool service, issuing App ID/Key pairs), and it has the same gate — keys are distributed only to registered third parties and to D2L's own installations.

**Conclusion:** OAuth is unavailable to us today. It remains the right long-term answer, and asking UTS about Valence API access is a reasonable thing to do — see [`07-risks-and-policy.md`](07-risks-and-policy.md). The architecture leaves a slot for it.

### The LTI path is closed for the same reason

D2L's other sanctioned integration route is **LTI 1.3** — the standard by which external tools get launched from inside a course. It has the identical blocker: the institution must register the tool in Brightspace, exchanging a client ID, a deployment ID, and a JWKS endpoint. Same admin gate, different acronym.

### "Sign in with MacID" does not produce an Avenue session

This deserves its own heading because it is an appealing idea that does not work, and the failure isn't obvious until you've built it.

McMaster's MacID sign-in is Microsoft Entra ID. So a natural thought is: register an app with Entra, run an OAuth flow, get a token, use it against Avenue.

**Entra tokens are scoped to the application that requested them.** A token minted for your app carries your app's audience claim. Brightspace will not accept it — it has no trust relationship with your app registration, and no endpoint that exchanges a third-party IdP token for a Brightspace session. Session cookies are minted by *Brightspace*, after Brightspace itself completes a SAML/OIDC handshake with McMaster's IdP as the relying party. You cannot insert yourself into that handshake from outside it.

Concretely:

```
✗  user → your app → Entra OAuth → access token → ??? → Avenue
      no exchange exists at the "???" step

✓  user → browser → avenue.mcmaster.ca
        → redirect to Microsoft (MacID + MFA)
        → redirect back to Avenue
        → Avenue sets d2lSessionVal / d2lSecureSessionVal
        → we keep those cookies
```

The distinction is **the redirect target**. In the working flow the handshake completes *with Avenue*, and we observe the result. In the broken flow it completes with us, and we hold a credential Avenue has never heard of.

From the user's point of view the two are indistinguishable — they click a button, sign in with their MacID Microsoft account, approve MFA, and land back working. That UX is exactly what the login flow below delivers. What we cannot do is skip the browser and own the token exchange ourselves.

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

### Prior art, and what it actually shows

Several independent D2L MCP servers work against student accounts, which establishes that *some* browser-derived mechanism is viable. It does **not** establish that cookie auth specifically is.

| Project | What it does |
|---|---|
| `joshuasoup/d2l-mcp` | Node/TS, Playwright SSO. **Captures the frontend's bearer token; uses no cookie auth.** |
| `RohanMuppa/brightspace-mcp-server` | Node/TS. Browser automation with MFA, AES-256-GCM session storage. Mechanism not inspectable from the published README. |
| `general-mudkip/d2l-mcp-server` | Assignments, grades, calendar, announcements, content. |

The middle column matters, and an earlier draft of this document got it wrong — it claimed all three "ship on this mechanism", meaning cookies. `joshuasoup/d2l-mcp` is the one whose source is readable, and its `src/auth.ts` intercepts Brightspace's own outbound requests to lift a token:

```js
if (url.includes("/d2l/api/")) {
  const auth = request.headers()["authorization"];
  if (auth?.startsWith("Bearer ")) { capturedToken = auth.slice(7);
```

`src/client.ts` then sends `Authorization: Bearer ${token}` on every call. Its README confirms the consequence: *"Auth tokens expire after ~1 hour but auto-refresh using the saved browser session."*

So the honest position is: **bearer tokens are confirmed to work somewhere; cookie auth is unconfirmed here.** Cookie auth remains plausible — D2L's own guidance describes "relying on the browser session as the authentication mechanism", with `X-Csrf-Token` on non-GETs — and it is what this server implements, because it would be the better design. But it is a bet, not a settled fact, and the first login will settle it.

D2L publishes `Brightspace/superagent-d2l-session-auth`, a first-party plugin whose job is attaching D2L session auth headers to requests. That's evidence session-based API auth is a real mechanism D2L builds on itself; it is not evidence about Avenue's configuration.

### If cookie auth turns out not to work

Two fallbacks exist, in preference order. Both implement `AuthProvider` (`apply` / `headers_for` / `is_alive`), so neither touches the tool layer:

1. **Mint a bearer from the session.** `POST /d2l/lp/auth/oauth2/token` with the session cookies and an `X-Csrf-Token`. This is not the admin-registered OAuth above — no client ID, no secret; it is the session exchanging itself for a short-lived token, which the logged-in browser can already do. **This preserves the property that matters:** the browser stays a login-only step.
2. **Capture the bearer during login.** Add a request listener to the Playwright context that lifts `Authorization: Bearer …` off any `/d2l/api/` call, and persist it beside the cookies. Guaranteed to work — it's what the prior art ships — but the token dies in about an hour and only a browser can refresh it.

Neither is implemented, deliberately: unwired auth code that no caller invokes and no test covers rots. Write whichever the probe shows is needed.

**The choice decides the product's ergonomics**, so record it in [`08-api-probe-results.md`](08-api-probe-results.md):

| Active strategy | Login cadence |
|---|---|
| Cookies | Roughly daily |
| Minted bearer | Roughly daily — the ~1h token re-mints without a browser |
| Captured bearer | **Roughly hourly** — only a browser can refresh it |

**Use one of these as a Phase 0 shortcut.** Before writing our own probe, install `RohanMuppa/brightspace-mcp-server` (it advertises MFA support and "works with any school") and point it at `avenue.mcmaster.ca`. If it lists your courses, the entire auth premise above is validated in twenty minutes rather than half a day. If it fails, *how* it fails tells us what McMaster's Entra chain does to browser automation — which is precisely what our login flow has to handle.

Two things it will not do for us: it's read-only, and it has no semantic search over course files. It's a de-risking instrument, not a substitute. See [`06-roadmap.md`](06-roadmap.md) Phase 0.

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
| `status()` | Session present? age? last successful call? — backs the `get_status` tool ([`03`](03-mcp-tools.md)) |
| `start_keepalive()` / `stop_keepalive()` | Scoped session extension; see Keepalive below |

`require_session()` **never silently launches a browser.** An MCP server runs headless under a client; popping a browser window mid-tool-call is hostile and can hang the call. Instead the tool fails with a clear, actionable message and the user re-runs the login command deliberately. See the error contract below.

## Session lifetime

**Measured on `avenue.cllmcmaster.ca`, 2026-08-07:**

| Thing | Comparable D2L deployments | **McMaster, measured** |
|---|---|---|
| Auth/XSRF token | ~1 hour | not separately measured (cookies are the credential) |
| Browser session | ~24 hours idle | **dead by 409 minutes (~6.8 h)** |
| Absolute cap | institution-configured | ≤ ~6.8 h |

The session was created at 09:29 and reported `alive=False` at age 409 min, having served several bursts of API calls in between. So **the real figure is roughly a third of the ~24 h the plan assumed**, and activity did not visibly extend it across that span.

Practical consequence: **expect to re-login about twice in a working day**, not once. That is materially more friction than "roughly daily", and it is the strongest argument yet for the keepalive below — the idle window is short enough for a cheap periodic request to be worth it, which was the open question gating it.

Not yet pinned down, and worth a second measurement: whether 409 min is an idle timeout that activity *would* have extended (the bursts were sparse), or a hard absolute cap that nothing extends. The two imply different keepalive designs.

### Keepalive — extending a session in active use

Daily re-login is the friction most likely to make this tool unpleasant enough to abandon. Brightspace sessions, like most session-based apps, extend their idle timer on activity. So a cheap periodic request can keep a session alive well past its idle window.

This sits in tension with the no-background-requests commitment in [`07-risks-and-policy.md`](07-risks-and-policy.md), and the resolution has to be deliberate rather than accidental:

| Rule | Value |
|---|---|
| Keepalive runs | **Only while the server process is up and has served at least one tool call** |
| Never runs | Before first use; after an idle period; when no session exists |
| Request used | `GET /d2l/lp/auth/xsrf-tokens` — the same liveness probe, no extra surface |
| Interval | ~30 min (config: `AVENUE_MCP_KEEPALIVE_MINUTES`, `0` disables) |
| Idle shutoff | Stops after `AVENUE_MCP_KEEPALIVE_IDLE_STOP` minutes with no tool calls (default 120) |

The distinction that keeps this honest: **this is not polling for data.** It sends no queries, fetches no course content, and stops on its own when you walk away. It's one lightweight request every half hour during a working session — materially less traffic than leaving an Avenue tab open in a browser, which does the same thing automatically.

It is also **off by default in v1** (`AVENUE_MCP_KEEPALIVE_MINUTES=0`) until Phase 0 confirms that (a) sessions do in fact extend on activity, and (b) the idle window is short enough for this to be worth doing. Both are measurements, not assumptions — record them in [`08-api-probe-results.md`](08-api-probe-results.md).

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
7. **Does activity extend the session's idle timer?** Gates whether keepalive is worth enabling at all.
8. **Is there an absolute session cap that activity cannot extend?** If so, keepalive buys hours, not days, and the docs should say which.
9. Does `RohanMuppa/brightspace-mcp-server` successfully authenticate against Avenue? (The twenty-minute validation above.)
