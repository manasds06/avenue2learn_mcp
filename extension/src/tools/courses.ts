/**
 * list_courses — ported from src/avenue_mcp/tools/courses.py.
 *
 * The root tool: every course-scoped tool needs an org_unit_id and this is
 * where one comes from.
 *
 * Note what this deliberately does NOT do: enrich each course via
 * `GET /lp/{v}/courses/{id}`. That route is 403 for students on this instance
 * (docs/08), and `myenrollments` already returns name, code, and an Access
 * block with the dates — so enrichment would be an N+1 against data we were
 * already handed, and a failing one at that.
 */

import { avenue } from "../avenue/client.js";
import { describe, parseD2L } from "../avenue/dates.js";
import { asInt, isObj, pick } from "../avenue/models.js";

const COURSE_OFFERING = "Course Offering";

export interface Course {
  org_unit_id: number;
  name: string;
  code: string;
  start_date: ReturnType<typeof describe>;
  end_date: ReturnType<typeof describe>;
  is_active: boolean;
}

export async function listCourses(args: { include_inactive?: boolean } = {}) {
  const includeInactive = args.include_inactive ?? false;

  // Filter server-side where the API supports it — cheaper than pulling every
  // past term and sifting locally. A student with several years of history
  // spans multiple pages.
  const params: Record<string, string> = {};
  if (!includeInactive) {
    params["isActive"] = "true";
    params["canAccess"] = "true";
  }

  const raw = await avenue.getPaged("lp", "enrollments/myenrollments/", params);

  const courses: Course[] = [];
  for (const entry of raw) {
    if (!isObj(entry)) continue;
    const org = pick(entry, "OrgUnit");
    if (!isObj(org)) continue;

    const id = asInt(pick(org, "Id", "OrgUnitId"));
    if (id === null) continue;

    // Enrollments also contain departments and semester containers. Only
    // course offerings are useful; the rest is noise in every result.
    const type = pick(org, "Type");
    const typeName = isObj(type) ? String(pick(type, "Name") ?? "") : String(type ?? "");
    if (typeName && typeName !== COURSE_OFFERING) continue;

    const access = pick(entry, "Access");
    const isActive = isObj(access) ? Boolean(access["IsActive"]) : true;
    if (!includeInactive && !isActive) continue;

    courses.push({
      org_unit_id: id,
      name: String(pick(org, "Name") ?? ""),
      code: String(pick(org, "Code") ?? ""),
      start_date: describe(parseD2L(isObj(access) ? access["StartDate"] : null)),
      end_date: describe(parseD2L(isObj(access) ? access["EndDate"] : null)),
      is_active: isActive,
    });
  }

  courses.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));

  return {
    courses,
    count: courses.length,
    include_inactive: includeInactive,
  };
}
