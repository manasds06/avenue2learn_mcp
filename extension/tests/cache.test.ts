/**
 * The cache is the one component here that can make the app CONFIDENTLY WRONG
 * rather than merely slow: a stale value is served with exactly the same
 * authority as a fresh one, and nothing in the UI distinguishes them.
 *
 * So the tests that matter are not "does it cache". They are: does it refuse to
 * cache the things that must never be stale, does it drop them when the panel
 * opens, and does it decline to remember a failure.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

// chrome.storage.local, in memory. The real one is not available under vitest,
// and the persistence path is exactly what these tests need to observe.
const store = new Map<string, unknown>();
vi.stubGlobal("chrome", {
  storage: {
    local: {
      get: async (keys: string | string[] | null) => {
        if (keys === null) return Object.fromEntries(store);
        const list = Array.isArray(keys) ? keys : [keys];
        return Object.fromEntries(list.filter((k) => store.has(k)).map((k) => [k, store.get(k)]));
      },
      set: async (items: Record<string, unknown>) => {
        for (const [k, v] of Object.entries(items)) store.set(k, v);
      },
      remove: async (keys: string | string[]) => {
        for (const k of Array.isArray(keys) ? keys : [keys]) store.delete(k);
      },
    },
  },
});

const { cached, clearCache, onPanelOpened, resetCacheForTests, cacheStatus } = await import(
  "../src/avenue/cache.js"
);

/** Keys are `institution|component/suffix`, as built by client.cacheKey. */
const COURSES = "mcmaster|lp/enrollments/myenrollments/";
const GRADES = "mcmaster|le/759806/grades/values/myGradeValues/";
const SUBMISSIONS = "mcmaster|le/759806/dropbox/folders/1/submissions/mysubmissions/";
const CALENDAR = "mcmaster|le/759806/calendar/events/myEvents/";

/** Nothing in the flush path is awaited by callers, so give it a beat. */
const settle = () => new Promise((r) => setTimeout(r, 600));

beforeEach(() => {
  store.clear();
  resetCacheForTests();
});

describe("what gets cached", () => {
  it("serves a second read of a stable route without refetching", async () => {
    const fetcher = vi.fn(async () => ({ courses: 21 }));
    expect(await cached(COURSES, fetcher)).toEqual({ courses: 21 });
    expect(await cached(COURSES, fetcher)).toEqual({ courses: 21 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not cache a route with no rule", async () => {
    // A new endpoint has to be considered deliberately rather than inheriting
    // a default that might be wrong for it.
    const fetcher = vi.fn(async () => 1);
    await cached("mcmaster|lp/users/whoami", fetcher);
    await cached("mcmaster|lp/users/whoami", fetcher);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("keys separately per institution", async () => {
    // Two schools can both have an org unit 1234. Serving one student the
    // other's data would be the worst bug in this codebase.
    const mac = vi.fn(async () => "mac");
    const carleton = vi.fn(async () => "carleton");
    expect(await cached("mcmaster|lp/enrollments/myenrollments/", mac)).toBe("mac");
    expect(await cached("carleton|lp/enrollments/myenrollments/", carleton)).toBe("carleton");
    expect(carleton).toHaveBeenCalledTimes(1);
  });

  it("de-duplicates concurrent identical requests even when uncacheable", async () => {
    // This is what stops Home fetching each course's calendar twice — once for
    // "what's due", once for the digest.
    let calls = 0;
    const slow = async () => {
      calls++;
      await new Promise((r) => setTimeout(r, 20));
      return calls;
    };
    const [a, b] = await Promise.all([
      cached("mcmaster|lp/users/whoami", slow),
      cached("mcmaster|lp/users/whoami", slow),
    ]);
    expect(calls).toBe(1);
    expect(a).toBe(b);
  });
});

describe("what must never be served stale", () => {
  it("never writes grades or submissions to disk", async () => {
    await cached(GRADES, async () => ({ mark: 92 }));
    await cached(SUBMISSIONS, async () => ({ submitted: true }));
    await cached(COURSES, async () => ({ courses: 21 }));
    await settle();

    const persisted = [...store.keys()];
    expect(persisted.some((k) => k.includes("grades"))).toBe(false);
    expect(persisted.some((k) => k.includes("mysubmissions"))).toBe(false);
    // The stable one did persist, so this is not just an empty store.
    expect(persisted.some((k) => k.includes("myenrollments"))).toBe(true);
  });

  it("drops grades when the panel opens, and keeps the slow structural reads", async () => {
    const grades = vi.fn(async () => ({ mark: 92 }));
    const courses = vi.fn(async () => ({ courses: 21 }));
    const calendar = vi.fn(async () => ["event"]);

    await cached(GRADES, grades);
    await cached(COURSES, courses);
    await cached(CALENDAR, calendar);

    await onPanelOpened();

    await cached(GRADES, grades);
    await cached(COURSES, courses);
    await cached(CALENDAR, calendar);

    // A mark shown after opening the panel is one that was just fetched.
    expect(grades).toHaveBeenCalledTimes(2);
    // …while the reads that make the first paint fast survived.
    expect(courses).toHaveBeenCalledTimes(1);
    expect(calendar).toHaveBeenCalledTimes(1);
  });

  it("does not remember a failure", async () => {
    // A cached failure is the bug that put "Course 759806" into every chunk
    // header for a whole session.
    const fetcher = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new Error("403"))
      .mockResolvedValue("real data");

    await expect(cached(COURSES, fetcher)).rejects.toThrow("403");
    expect(await cached(COURSES, fetcher)).toBe("real data");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("stops serving an entry once its TTL has passed", async () => {
    vi.useFakeTimers();
    try {
      const fetcher = vi.fn(async () => "v1");
      await cached(COURSES, fetcher);
      // The course list rule is 45 minutes.
      vi.setSystemTime(Date.now() + 46 * 60_000);
      await cached(COURSES, fetcher);
      expect(fetcher).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("the cache is visible and clearable", () => {
  it("reports what it is holding", async () => {
    await cached(COURSES, async () => 1);
    const status = await cacheStatus();
    expect(status.entries).toBe(1);
    expect(status.next_expiry_seconds).toBeGreaterThan(0);
  });

  it("clears memory and disk together", async () => {
    await cached(COURSES, async () => 1);
    await settle();
    expect([...store.keys()].length).toBeGreaterThan(0);

    await clearCache();

    expect((await cacheStatus()).entries).toBe(0);
    expect([...store.keys()].filter((k) => k.startsWith("hc:"))).toEqual([]);
  });
});
