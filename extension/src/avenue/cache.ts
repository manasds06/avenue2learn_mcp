/**
 * Response cache for Brightspace GETs.
 *
 * Two problems, one mechanism:
 *
 *   1. Home fetches each course's calendar TWICE — once for "what's due", once
 *      for the digest — because neither tool knows the other just asked.
 *   2. An MV3 service worker is killed after ~30s idle, so anything held in
 *      memory there evaporates between panel opens. An in-memory cache alone
 *      would be almost useless for the case that actually hurts: opening the
 *      panel and waiting.
 *
 * So entries persist to chrome.storage.local, and the in-flight map de-dupes
 * concurrent identical requests even for routes that are never cached.
 *
 * WHAT IS NEVER PERSISTED, and why it is a policy rather than a tuning knob:
 * grades and submission status. A cached 92 that was regraded to 78 is worse
 * than a slow panel — it is the same failure this project keeps designing
 * against, a stale reading presented as a current fact. Those routes get a
 * short in-memory TTL only, long enough to stop one screen asking twice, and
 * they are dropped every time the panel opens.
 *
 * Nothing here leaves the machine. It is the same browser profile that already
 * holds the session cookie and the file index.
 */

const PREFIX = "hc:";

interface Entry {
  /** Epoch ms when this stops being served. */
  until: number;
  value: unknown;
}

interface Rule {
  match: RegExp;
  ttlMs: number;
  /** Survives a worker restart and a panel open. */
  persist: boolean;
  /** Dropped when the panel opens. */
  volatile: boolean;
}

/**
 * First match wins, so the never-persist rules are listed first on purpose.
 * A route with no rule is NOT cached — a new endpoint has to be considered
 * deliberately rather than inheriting a default that might be wrong for it.
 */
const RULES: Rule[] = [
  // --- never persisted, always refreshed when the panel opens ---
  { match: /\/grades\//, ttlMs: 60_000, persist: false, volatile: true },
  { match: /\/mysubmissions\//, ttlMs: 60_000, persist: false, volatile: true },
  { match: /\/quizzes\//, ttlMs: 60_000, persist: false, volatile: true },

  // --- slow, stable, and the reason Home felt heavy ---
  { match: /enrollments\/myenrollments\//, ttlMs: 45 * 60_000, persist: true, volatile: false },
  { match: /\/calendar\/events\//, ttlMs: 30 * 60_000, persist: true, volatile: false },
  { match: /\/content\/(root|modules)\//, ttlMs: 45 * 60_000, persist: true, volatile: false },
  { match: /\/dropbox\/folders\//, ttlMs: 15 * 60_000, persist: true, volatile: false },
  { match: /\/news\//, ttlMs: 10 * 60_000, persist: true, volatile: false },
  { match: /\/discussions\//, ttlMs: 15 * 60_000, persist: true, volatile: false },
];

function ruleFor(key: string): Rule | null {
  return RULES.find((r) => r.match.test(key)) ?? null;
}

const memory = new Map<string, Entry>();
const inFlight = new Map<string, Promise<unknown>>();
let hydrated = false;

/** Pull persisted entries into memory once per worker lifetime. */
async function hydrate(): Promise<void> {
  if (hydrated) return;
  hydrated = true;
  try {
    const all = await chrome.storage.local.get(null);
    const now = Date.now();
    const expired: string[] = [];
    for (const [k, v] of Object.entries(all)) {
      if (!k.startsWith(PREFIX)) continue;
      const entry = v as Entry;
      if (!entry || typeof entry.until !== "number") continue;
      if (entry.until <= now) expired.push(k);
      else memory.set(k.slice(PREFIX.length), entry);
    }
    // Sweep on the way in, so an abandoned course's entries do not accumulate
    // forever in a profile.
    if (expired.length) await chrome.storage.local.remove(expired);
  } catch {
    // A cache that cannot load is a cache miss, never an error the user sees.
  }
}

/** Batched so twenty-one course fetches are not twenty-one storage writes. */
let pendingWrites: Record<string, Entry> = {};
let writeTimer: ReturnType<typeof setTimeout> | null = null;

function scheduleWrite(key: string, entry: Entry): void {
  pendingWrites[PREFIX + key] = entry;
  if (writeTimer) return;
  writeTimer = setTimeout(() => {
    const batch = pendingWrites;
    pendingWrites = {};
    writeTimer = null;
    void chrome.storage.local.set(batch).catch(() => {});
  }, 500);
}

/**
 * Run `fetcher`, serving a fresh cached value if there is one.
 *
 * `force` skips the read but still writes, which is what a Refresh button
 * should do: get the truth, and let everything else benefit from it.
 */
export async function cached<T>(
  key: string,
  fetcher: () => Promise<T>,
  opts: { force?: boolean } = {},
): Promise<T> {
  const rule = ruleFor(key);
  await hydrate();

  if (!opts.force && rule) {
    const hit = memory.get(key);
    if (hit && hit.until > Date.now()) return hit.value as T;
  }

  // De-duplicate concurrent identical requests even when nothing is cacheable.
  // This alone removes the double calendar fetch on every Home load.
  const running = inFlight.get(key);
  if (running && !opts.force) return running as Promise<T>;

  const promise = (async () => {
    const value = await fetcher();
    if (rule) {
      const entry: Entry = { until: Date.now() + rule.ttlMs, value };
      memory.set(key, entry);
      if (rule.persist) scheduleWrite(key, entry);
    }
    return value;
  })();

  inFlight.set(key, promise);
  try {
    return (await promise) as T;
  } finally {
    // A rejected request must NOT be cached. A cached failure is the bug that
    // put "Course 759806" into every chunk header for a whole session.
    inFlight.delete(key);
  }
}

/**
 * Called when the side panel opens.
 *
 * Drops everything a student would be annoyed to see stale — grades, quiz
 * attempts, submissions — while keeping the slow structural reads that make the
 * panel feel instant. Opening the panel is the moment someone expects to be
 * looking at current information.
 */
export async function onPanelOpened(): Promise<number> {
  await hydrate();
  let dropped = 0;
  for (const key of [...memory.keys()]) {
    const rule = ruleFor(key);
    if (rule?.volatile !== false) {
      memory.delete(key);
      dropped++;
    }
  }
  return dropped;
}

/** Setup's "clear cached data". Also the honest answer to "is this stale?". */
export async function clearCache(): Promise<number> {
  memory.clear();
  const all = await chrome.storage.local.get(null);
  const keys = Object.keys(all).filter((k) => k.startsWith(PREFIX));
  if (keys.length) await chrome.storage.local.remove(keys);
  return keys.length;
}

/**
 * For get_status, so the cache is visible rather than a mystery.
 *
 * If a number looks wrong, the first question is "how old is this?" — and an
 * invisible cache makes that unanswerable.
 */
export async function cacheStatus(): Promise<{
  entries: number;
  next_expiry_seconds: number | null;
}> {
  await hydrate();
  const now = Date.now();
  let soonest: number | null = null;
  for (const entry of memory.values()) {
    const remaining = entry.until - now;
    if (soonest === null || remaining < soonest) soonest = remaining;
  }
  return {
    entries: memory.size,
    next_expiry_seconds: soonest === null ? null : Math.max(0, Math.round(soonest / 1000)),
  };
}

/** Test hook. */
export function resetCacheForTests(): void {
  memory.clear();
  inFlight.clear();
  hydrated = false;
  pendingWrites = {};
  if (writeTimer) {
    clearTimeout(writeTimer);
    writeTimer = null;
  }
}
