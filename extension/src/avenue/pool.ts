/**
 * Bounded-concurrency map.
 *
 * The Python side runs per-course work through `asyncio.gather`. The port used
 * `for … await`, which turned every sweep into one request at a time — with 21
 * courses that is the difference between a second and a minute.
 *
 * BOUNDED, not unbounded. `Promise.all` over 21 courses × 4 categories fires 84
 * requests at Brightspace at once, which is a good way to get throttled by the
 * very institution whose session we are borrowing. A small pool gets nearly all
 * of the speedup and behaves like a browser tab rather than a scraper.
 *
 * `fn` rejecting rejects the whole call. Callers that want per-item tolerance
 * must catch inside `fn` — which every caller here does, because one course's
 * denied route must never cost the other twenty.
 */
export async function mapPool<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const out = new Array<R>(items.length);
  let next = 0;

  const worker = async (): Promise<void> => {
    for (;;) {
      const i = next++;
      if (i >= items.length) return;
      out[i] = await fn(items[i]!, i);
    }
  };

  await Promise.all(
    Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, worker),
  );
  return out;
}

/** How many courses to work on at once. Tuned for "feels instant", not throughput. */
export const COURSE_CONCURRENCY = 6;

/** Within one course — submission checks, category fetches. */
export const DETAIL_CONCURRENCY = 4;
