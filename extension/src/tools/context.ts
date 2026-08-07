/**
 * Cross-tool helpers: course names, and the shared deadline window.
 *
 * `courseName` deliberately does NOT call `GET /lp/{v}/courses/{id}` — that
 * route is 403 for student accounts on this instance (docs/08), and it would
 * be an N+1 anyway. Names come from the enrollment list, fetched once and kept
 * for the worker's lifetime.
 */

import { listCourses } from "./courses.js";

let nameCache: Map<number, string> | null = null;

export async function courseName(orgUnitId: number): Promise<string> {
  if (!nameCache) {
    try {
      const { courses } = await listCourses({ include_inactive: true });
      nameCache = new Map(courses.map((c) => [c.org_unit_id, c.name]));
    } catch {
      // A name is not worth failing a whole tool over.
      nameCache = new Map();
    }
  }
  return nameCache.get(orgUnitId) ?? `Course ${orgUnitId}`;
}

/** Test hook, and used after a sign-in changes which courses are visible. */
export function clearCourseNames(): void {
  nameCache = null;
}
