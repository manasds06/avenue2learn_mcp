/**
 * Valence REST client, ported from src/avenue_mcp/client/d2l.py.
 *
 * THE WHOLE DESIGN RESTS ON THIS FILE, so it is worth being precise about why
 * it works:
 *
 *   fetch(url, { credentials: "include" })
 *
 * The extension has `host_permissions` for avenue.cllmcmaster.ca, so (a) the
 * browser attaches the HttpOnly session cookies automatically, and (b) the
 * request is exempt from CORS. We get authenticated data and NEVER SEE THE
 * COOKIE. There is no credential here to store, leak, or be responsible for —
 * which is what makes "we store nothing" structural rather than a promise.
 *
 * Note there is no `chrome.cookies` call anywhere in this codebase, and the
 * manifest does not request the `cookies` permission. That is deliberate and
 * worth keeping true.
 */

import {
  AvenueError,
  invalidRequest,
  network,
  notFound,
  notSignedIn,
  permissionDenied,
  upstream,
} from "./errors.js";

export const BASE_URL = "https://avenue.cllmcmaster.ca";

export type Component = "lp" | "le";

/** Only used if version negotiation fails. 1.0 is universally supported. */
const FALLBACK_VERSIONS: Record<Component, string> = { lp: "1.0", le: "1.0" };

/** Politeness, matching docs/02. Avenue publishes no limit, which is not the
 *  same as there being none. */
const MAX_CONCURRENCY = 4;
const MIN_INTERVAL_MS = 100;
const MAX_ATTEMPTS = 3;

export class AvenueClient {
  private versions: Record<string, string> | null = null;
  private inFlight = 0;
  private queue: Array<() => void> = [];
  private lastStart = 0;

  // --- throttle ----------------------------------------------------------

  private async acquire(): Promise<void> {
    if (this.inFlight >= MAX_CONCURRENCY) {
      await new Promise<void>((resolve) => this.queue.push(resolve));
    }
    this.inFlight++;
    const wait = this.lastStart + MIN_INTERVAL_MS - Date.now();
    if (wait > 0) await sleep(wait);
    this.lastStart = Date.now();
  }

  private release(): void {
    this.inFlight--;
    this.queue.shift()?.();
  }

  // --- versions ----------------------------------------------------------

  /**
   * Negotiate versions from the instance rather than hardcoding.
   * Versions advance with each Brightspace release and differ per institution,
   * so a hardcoded `1.57` is a time bomb. Cached for the worker's lifetime.
   */
  async getVersions(): Promise<Record<string, string>> {
    if (this.versions) return this.versions;

    try {
      const data = await this.rawJson("GET", "/d2l/api/versions/");
      const found: Record<string, string> = {};
      if (Array.isArray(data)) {
        for (const entry of data) {
          const code = String(entry?.ProductCode ?? "").toLowerCase();
          const latest = entry?.LatestVersion;
          if (code && typeof latest === "string") found[code] = latest;
        }
      }
      this.versions = { ...FALLBACK_VERSIONS, ...found };
    } catch (err) {
      // A signed-out user can't negotiate either. Surface that honestly rather
      // than silently falling back to 1.0 and failing later somewhere less
      // obvious.
      if (err instanceof AvenueError) throw err;
      this.versions = { ...FALLBACK_VERSIONS };
    }
    return this.versions;
  }

  async path(component: Component, suffix: string): Promise<string> {
    const versions = await this.getVersions();
    const v = versions[component] ?? FALLBACK_VERSIONS[component];
    return `/d2l/api/${component}/${v}/${suffix.replace(/^\//, "")}`;
  }

  // --- requests ----------------------------------------------------------

  private async rawJson(
    method: string,
    path: string,
    params?: Record<string, string | number | undefined>,
  ): Promise<unknown> {
    const url = new URL(path, BASE_URL);
    for (const [k, v] of Object.entries(params ?? {})) {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
    }

    let attempt = 0;
    for (;;) {
      attempt++;
      await this.acquire();
      let resp: Response;
      try {
        resp = await fetch(url.toString(), {
          method,
          // The entire mechanism. Without this the cookies are not sent.
          credentials: "include",
          headers: { Accept: "application/json, text/plain, */*" },
        });
      } catch (err) {
        if (attempt >= MAX_ATTEMPTS) throw network(path, String(err));
        await sleep(backoffMs(attempt));
        continue;
      } finally {
        // Runs on the success path, the retry `continue`, and the throw
        // alike. Releasing anywhere else leaks a permit and, after
        // MAX_CONCURRENCY leaks, deadlocks every later request.
        this.release();
      }

      if (resp.status === 429 || resp.status >= 500) {
        if (attempt >= MAX_ATTEMPTS) throw upstream(path, resp.status);
        const retryAfter = Number(resp.headers.get("retry-after"));
        await sleep(Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter * 1000 : backoffMs(attempt));
        continue;
      }

      return await this.parse(resp, path);
    }
  }

  private async parse(resp: Response, path: string): Promise<unknown> {
    const ctype = (resp.headers.get("content-type") ?? "").toLowerCase();
    const isJson = ctype.includes("json");

    if (resp.ok) {
      // A JSON route answering with HTML is the sign-in wall, even at 200.
      if (!isJson) throw notSignedIn(`Avenue returned a sign-in page for ${path}.`);
      try {
        return await resp.json();
      } catch {
        throw upstream(path, resp.status);
      }
    }

    if (resp.status === 401) throw notSignedIn();

    if (resp.status === 403) {
      // Both meanings arrive as 403 (docs/08). Signed-out Avenue answers with
      // HTML; a genuine permission denial answers with JSON. Getting this
      // backwards sends the user to re-login against a wall that never moves.
      throw isJson ? permissionDenied(path) : notSignedIn();
    }

    if (resp.status === 404) throw notFound(path);
    if (resp.status === 400) throw invalidRequest(path, (await safeText(resp)).slice(0, 200));
    throw upstream(path, resp.status);
  }

  /** GET a versioned route. */
  async get(
    component: Component,
    suffix: string,
    params?: Record<string, string | number | undefined>,
  ): Promise<unknown> {
    return this.rawJson("GET", await this.path(component, suffix), params);
  }

  /**
   * Follow bookmark pagination to exhaustion.
   *
   * Implemented once, here. A per-call-site paging loop is how you silently
   * truncate a course list at page one.
   */
  async getPaged(
    component: Component,
    suffix: string,
    params?: Record<string, string | number | undefined>,
    maxPages = 50,
  ): Promise<unknown[]> {
    const path = await this.path(component, suffix);
    const items: unknown[] = [];
    let bookmark: string | undefined;

    for (let page = 0; page < maxPages; page++) {
      const data = await this.rawJson("GET", path, { ...params, bookmark });

      if (Array.isArray(data)) {
        items.push(...data);
        break; // unpaged route
      }
      if (typeof data !== "object" || data === null) break;

      const obj = data as Record<string, unknown>;
      const chunk = obj["Items"] ?? obj["Objects"];
      if (Array.isArray(chunk)) items.push(...chunk);

      const paging = (obj["PagingInfo"] ?? {}) as Record<string, unknown>;
      const next = paging["Bookmark"];
      const hasMore = Boolean(paging["HasMoreItems"]);
      if (!hasMore || typeof next !== "string" || next === bookmark) break;
      bookmark = next;
    }

    return items;
  }
}

function backoffMs(attempt: number): number {
  return 2 ** (attempt - 1) * 1000 + Math.random() * 500;
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function safeText(resp: Response): Promise<string> {
  try {
    return await resp.text();
  } catch {
    return "";
  }
}

export const avenue = new AvenueClient();
