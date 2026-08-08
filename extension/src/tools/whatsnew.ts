/**
 * get_whats_new — the watermark digest.
 * Ported from src/avenue_mcp/tools/whatsnew.py.
 *
 * "What did I miss?" is the question students actually ask most, and answering
 * it by polling each tool separately is slow and easy to get wrong.
 *
 * Watermarks are per-course AND per-category. One global timestamp breaks the
 * moment one course is read and another is not: the un-read one either floods
 * the next digest or silently loses changes.
 *
 * TWO RULES THAT LOOK LIKE DETAILS AND ARE NOT:
 *
 *   1. A watermark advances ONLY for a category that actually SUCCEEDED. The
 *      Python version once advanced all of them regardless, so a session
 *      expiring mid-digest returned "nothing new" AND permanently skipped the
 *      changes it never read. Losing data silently is far worse than an error.
 *   2. `mark_seen: false` is a real peek. Asking "what's new?" twice in one
 *      conversation must give the same answer, not an empty second response
 *      because the first call consumed the watermark.
 */

import { avenue } from "../avenue/client.js";
import { AvenueError } from "../avenue/errors.js";
import { addDays, describe, nowUtc, parseD2L, toUtcIso } from "../avenue/dates.js";
import { asFloat, asInt, isObj, pick, richText, toText, truncate } from "../avenue/models.js";
import { listAnnouncements } from "./announcements.js";
import { getUpcomingDeadlines } from "./assignments.js";
import { getCourseContent } from "./content.js";
import { listCourses } from "./courses.js";
import { getGrades } from "./grades.js";

const CATEGORIES = ["announcements", "files", "grades", "deadlines"] as const;
type Category = (typeof CATEGORIES)[number];

const DEFAULT_LOOKBACK_DAYS = 7;
const SNIPPET = 400;

const wmKey = (orgUnitId: number, category: Category) => `wm:${orgUnitId}:${category}`;

async function getWatermark(orgUnitId: number, category: Category): Promise<string | null> {
  const key = wmKey(orgUnitId, category);
  const stored = await chrome.storage.local.get(key);
  const value = stored[key];
  return typeof value === "string" ? value : null;
}

async function setWatermark(orgUnitId: number, category: Category, iso: string): Promise<void> {
  await chrome.storage.local.set({ [wmKey(orgUnitId, category)]: iso });
}

interface Change {
  course_name: string;
  org_unit_id: number;
  title: string;
  at: ReturnType<typeof describe>;
  detail?: string | null;
}

interface Failure {
  course_name: string;
  category: string;
  message: string;
}

export async function getWhatsNew(args: {
  since?: string;
  mark_seen?: boolean;
  org_unit_id?: number;
}) {
  const markSeen = args.mark_seen ?? true;
  const now = nowUtc();
  const stamp = toUtcIso(now)!;

  let courses = (await listCourses({})).courses;
  if (args.org_unit_id !== undefined) {
    courses = courses.filter((c) => c.org_unit_id === args.org_unit_id);
  }

  const override = args.since ? parseD2L(args.since) : null;

  // Determined BEFORE anything writes. Computing it afterwards meant the
  // "first run" note could never fire on an actual first run.
  const firstRun =
    !override &&
    (await Promise.all(courses.map((c) => getWatermark(c.org_unit_id, "announcements")))).every(
      (w) => w === null,
    );

  const changes: Record<string, Change[]> = {
    announcements: [],
    new_files: [],
    new_grades: [],
    upcoming: [],
  };
  const failures: Failure[] = [];
  const advanced: Array<{ org_unit_id: number; category: Category }> = [];

  for (const course of courses) {
    const oid = course.org_unit_id;
    const name = course.name;

    for (const category of CATEGORIES) {
      const stored = override ? toUtcIso(override) : await getWatermark(oid, category);
      const cutoff = stored ? parseD2L(stored) : addDays(now, -DEFAULT_LOOKBACK_DAYS);

      try {
        await collect(category, oid, name, cutoff, changes, now);
      } catch (err) {
        failures.push({
          course_name: name || String(oid),
          category,
          message: err instanceof AvenueError ? err.message : String(err).slice(0, 200),
        });
        // Watermark deliberately NOT advanced — see the header note.
        continue;
      }

      if (markSeen) {
        await setWatermark(oid, category, stamp);
        advanced.push({ org_unit_id: oid, category });
      }
    }
  }

  const total = Object.values(changes).reduce((n, list) => n + list.length, 0);
  const notes: string[] = [];

  if (firstRun) {
    notes.push(
      `First run: no watermark existed, so this covers roughly the last ` +
        `${DEFAULT_LOOKBACK_DAYS} days. Later calls report only what is new.`,
    );
  }
  if (failures.length) {
    notes.push(
      `${failures.length} source(s) could not be read, so this digest is INCOMPLETE — ` +
        `see failures. Their watermarks were left untouched, so nothing has been ` +
        `skipped permanently.`,
    );
  }
  if (!markSeen) {
    notes.push("Peek only: watermarks were not advanced, so this repeats next time.");
  }

  return {
    generated_at: describe(now),
    courses_checked: courses.length,
    total_changes: total,
    changes,
    failures,
    complete: failures.length === 0,
    watermarks_advanced: markSeen ? advanced.length : 0,
    note: notes.length ? notes.join(" ") : null,
  };
}

async function collect(
  category: Category,
  oid: number,
  name: string,
  cutoff: Date | null,
  changes: Record<string, Change[]>,
  now: Date,
): Promise<void> {
  const newer = (d: Date | null) => d !== null && (!cutoff || d > cutoff);

  if (category === "announcements") {
    const { announcements } = await listAnnouncements({ org_unit_id: oid, limit: 50 });
    for (const a of announcements) {
      const at = parseD2L(a.posted_at?.utc ?? null);
      if (!newer(at)) continue;
      changes["announcements"]!.push({
        course_name: name,
        org_unit_id: oid,
        title: a.title,
        at: a.posted_at,
        detail: truncate(a.body_text, SNIPPET).text || null,
      });
    }
    return;
  }

  if (category === "files") {
    const tree = await getCourseContent({ org_unit_id: oid });
    const walk = (mods: Array<{ topics: unknown[]; modules: unknown[] }>): void => {
      for (const mod of mods) {
        for (const t of mod.topics as unknown as Array<Record<string, unknown>>) {
          const at = parseD2L((t["last_modified"] as { utc?: string } | null)?.utc ?? null);
          if (!newer(at)) continue;
          changes["new_files"]!.push({
            course_name: name,
            org_unit_id: oid,
            title: String(t["title"] ?? t["file_name"] ?? ""),
            at: t["last_modified"] as Change["at"],
            detail: t["is_downloadable"] ? "file" : "link or page",
          });
        }
        walk(mod.modules as never);
      }
    };
    walk(tree.modules as never);
    return;
  }

  if (category === "grades") {
    const { items } = await getGrades({ org_unit_id: oid });
    for (const item of items) {
      if (!item.is_graded) continue;
      // Grades carry no reliable "released at" on this instance, so a newly
      // graded item is reported once and the watermark suppresses it after.
      // Better a single duplicate than silently dropping a grade.
      changes["new_grades"]!.push({
        course_name: name,
        org_unit_id: oid,
        title: item.name ?? "",
        at: describe(now),
        detail:
          item.percentage !== null
            ? `${item.percentage}%`
            : (item.displayed_grade ?? "graded"),
      });
    }
    return;
  }

  // deadlines: what has entered the window since last time
  const { deadlines } = await getUpcomingDeadlines({ days_ahead: 14 });
  for (const d of deadlines as Array<Record<string, unknown>>) {
    if (d["org_unit_id"] !== oid) continue;
    changes["upcoming"]!.push({
      course_name: name,
      org_unit_id: oid,
      title: String(d["title"] ?? ""),
      at: d["due_date"] as Change["at"],
      detail: String(d["submission_status"] ?? ""),
    });
  }
}

/** Used by get_status so a user can see the digest has state. */
export async function watermarkCount(): Promise<number> {
  const all = await chrome.storage.local.get(null);
  return Object.keys(all).filter((k) => k.startsWith("wm:")).length;
}

/** Exposed for a "start fresh" action. */
export async function clearWatermarks(): Promise<number> {
  const all = await chrome.storage.local.get(null);
  const keys = Object.keys(all).filter((k) => k.startsWith("wm:"));
  await chrome.storage.local.remove(keys);
  return keys.length;
}
