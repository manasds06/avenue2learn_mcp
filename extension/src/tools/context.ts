/**
 * Cross-tool helpers: course names.
 *
 * `courseName` deliberately does NOT call `GET /lp/{v}/courses/{id}` — that
 * route is 403 for student accounts on this instance (docs/08), and it would
 * be an N+1 anyway. Names come from the enrollment list.
 *
 * THIS MATTERS MORE THAN A LABEL. The course name goes into every chunk's
 * context header before embedding, which is what stops a passage from one
 * course being retrieved for a question about another. A header reading
 * "[Course 759806 — …]" instead of "[SFWRENG 2FA3 — …]" quietly weakens the
 * single guard against cross-course contamination.
 */

import { listCourses } from "./courses.js";

let nameCache: Map<number, string> | null = null;

export async function courseName(orgUnitId: number): Promise<string> {
  const cached = nameCache?.get(orgUnitId);
  if (cached) return cached;

  try {
    const { courses } = await listCourses({ include_inactive: true });
    // Only keep a cache that actually has something in it. An earlier version
    // stored an empty Map on failure, which turned one transient error into a
    // permanent "Course 759806" for the rest of the session — including in the
    // chunk headers of everything indexed afterwards.
    if (courses.length) {
      nameCache = new Map(courses.map((c) => [c.org_unit_id, c.name]));
    }
  } catch {
    // A name is not worth failing a whole tool over, and NOT caching the
    // failure means the next call gets another chance.
  }

  return nameCache?.get(orgUnitId) ?? `Course ${orgUnitId}`;
}

/**
 * True when the name is a placeholder rather than the real thing.
 *
 * Callers that write the name somewhere durable — the index, a citation —
 * should check this rather than silently persisting a fallback.
 */
export function isPlaceholderName(name: string): boolean {
  return /^Course \d+$/.test(name);
}

/** Test hook, and used after a sign-in changes which courses are visible. */
export function clearCourseNames(): void {
  nameCache = null;
}
