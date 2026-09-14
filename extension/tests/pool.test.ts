/**
 * The per-course sweeps are the slowest thing this extension does, and Home
 * waits on two of them. Both correctness properties below are load-bearing:
 * results must stay positionally aligned with their inputs (the deadline view
 * zips submission results back against folders by index), and the pool must
 * actually cap concurrency rather than degenerating into Promise.all.
 */

import { describe, expect, it } from "vitest";

import { mapPool } from "../src/avenue/pool.js";

describe("mapPool", () => {
  it("returns results in input order, not completion order", async () => {
    const out = await mapPool([30, 20, 10, 0], 4, async (ms, i) => {
      await new Promise((r) => setTimeout(r, ms));
      return `${i}:${ms}`;
    });
    // Finished in the reverse order they were started.
    expect(out).toEqual(["0:30", "1:20", "2:10", "3:0"]);
  });

  it("never runs more than `limit` at once", async () => {
    let live = 0;
    let peak = 0;

    await mapPool(Array.from({ length: 20 }, (_, i) => i), 3, async () => {
      live++;
      peak = Math.max(peak, live);
      await new Promise((r) => setTimeout(r, 5));
      live--;
    });

    expect(peak).toBeLessThanOrEqual(3);
    // Guards against a "pool" that silently serialized — which is the bug it
    // exists to fix.
    expect(peak).toBeGreaterThan(1);
  });

  it("visits every item exactly once", async () => {
    const seen: number[] = [];
    await mapPool(Array.from({ length: 50 }, (_, i) => i), 7, async (n) => {
      seen.push(n);
    });
    expect(seen.sort((a, b) => a - b)).toEqual(Array.from({ length: 50 }, (_, i) => i));
  });

  it("handles an empty list without hanging", async () => {
    expect(await mapPool([], 4, async () => 1)).toEqual([]);
  });

  it("rejects if an item rejects, so callers must catch per item", async () => {
    // Documented behaviour rather than an accident: every caller in the tool
    // layer catches inside `fn`, because one course's denied route must not
    // cost the other twenty.
    await expect(
      mapPool([1, 2, 3], 2, async (n) => {
        if (n === 2) throw new Error("boom");
        return n;
      }),
    ).rejects.toThrow("boom");
  });
});
