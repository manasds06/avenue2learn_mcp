/**
 * Assignments and the unified deadline view.
 * Ported from src/avenue_mcp/tools/assignments.py.
 *
 * `dropbox/folders/` may be instructor-only, so this degrades: if the folder
 * listing is denied, deadlines still come from the calendar, and the response
 * says plainly what was lost rather than silently returning less.
 *
 * THE RULE THAT COST THE MOST TO LEARN: submission status has THREE states,
 * not two. `mysubmissions` is 403 on the probed instances, and defaulting to
 * "not_submitted" on that silence tells a student they have not handed in work
 * they may well have. In the deadline view it was worse — items marked
 * submitted get filtered out by default, so a denied route silently emptied
 * "what's due this week". Both bugs shipped in the Python version and are
 * fixed there too.
 */

import { avenue } from "../avenue/client.js";
import { AvenueError } from "../avenue/errors.js";
import { addDays, daysUntil, describe, nowUtc, parseD2L, toUtcIso } from "../avenue/dates.js";
import { asFloat, asInt, isObj, pick, richText, toText, truncate } from "../avenue/models.js";
import { describeDenial } from "../institutions.js";
import { getCurrentInstitution } from "../settings.js";
import { calendarEvents } from "./calendar.js";
import { courseName } from "./context.js";
import { listCourses } from "./courses.js";

const INSTRUCTIONS_CAP = 2000;

/** Distinguishes "route denied" from "nothing submitted". */
const UNAVAILABLE = Symbol("submissions-unavailable");
type SubmissionResult = { submittedAt: Date | null; files: string[] } | null | typeof UNAVAILABLE;

export async function listAssignments(args: {
  org_unit_id: number;
  include_submitted?: boolean;
}) {
  const { org_unit_id } = args;
  const includeSubmitted = args.include_submitted ?? true;
  const inst = await getCurrentInstitution();

  let folders: unknown[];
  try {
    folders = await avenue.getPaged("le", `${org_unit_id}/dropbox/folders/`);
  } catch (err) {
    if (err instanceof AvenueError && err.kind === "PermissionDenied") {
      return degradedFromCalendar(org_unit_id, describeDenial(inst, "dropbox_folders"));
    }
    throw err;
  }

  const assignments = [];
  let statusUnavailable = false;

  for (const folder of folders) {
    if (!isObj(folder)) continue;
    const folderId = asInt(pick(folder, "Id", "FolderId"));
    if (folderId === null) continue;

    const due = parseD2L(pick(folder, "DueDate", "Due"));
    const instructions = toText(richText(pick(folder, "Instructions", "CustomInstructions")));

    const sub = await mySubmission(org_unit_id, folderId);
    let status: string;
    if (sub === UNAVAILABLE) {
      status = "unknown";
      statusUnavailable = true;
    } else if (sub !== null) {
      status = "submitted";
    } else {
      status = "not_submitted";
    }

    if (status === "submitted" && !includeSubmitted) continue;

    assignments.push({
      folder_id: folderId,
      name: String(pick(folder, "Name", "Title") ?? ""),
      due_date: describe(due),
      days_until_due: daysUntil(due),
      points_possible: asFloat(pick(folder, "OutOf", "PointsPossible", "TotalPoints")),
      instructions_text: truncate(instructions, INSTRUCTIONS_CAP).text || null,
      submission_status: status,
      submitted_at:
        sub !== UNAVAILABLE && sub !== null ? describe(sub.submittedAt) : null,
      submitted_files: sub !== UNAVAILABLE && sub !== null ? sub.files : [],
    });
  }

  assignments.sort(
    (a, b) =>
      (a.due_date?.utc ?? "9999").localeCompare(b.due_date?.utc ?? "9999") ||
      a.name.localeCompare(b.name),
  );

  return {
    org_unit_id,
    course_name: await courseName(org_unit_id),
    assignments,
    count: assignments.length,
    degraded: false,
    submission_status_available: !statusUnavailable,
    note: statusUnavailable
      ? `Submission status is not available — the learner submissions route was denied. ` +
        `${describeDenial(inst, "dropbox_mysubmissions")} Every assignment shows ` +
        `submission_status 'unknown': do NOT tell the user whether they have handed ` +
        `anything in. Due dates, points, and instructions above are accurate.`
      : null,
  };
}

async function degradedFromCalendar(orgUnitId: number, weight: string) {
  let events: Awaited<ReturnType<typeof calendarEvents>> = [];
  try {
    events = await calendarEvents(orgUnitId);
  } catch {
    events = [];
  }

  const items = events
    .filter((ev) => ev.kind === "assignment" || ev.kind === "event")
    .map((ev) => ({
      folder_id: null,
      name: ev.title,
      due_date: ev.due_date,
      days_until_due: daysUntil(ev.dueAt),
      points_possible: null,
      instructions_text: null,
      submission_status: "unknown",
      submitted_at: null,
      submitted_files: [],
    }));

  return {
    org_unit_id: orgUnitId,
    course_name: await courseName(orgUnitId),
    assignments: items,
    count: items.length,
    degraded: true,
    submission_status_available: false,
    note:
      `The assignment folder list could not be read, so point values, instructions, ` +
      `and submission status are unavailable. ${weight} These entries are ` +
      `calendar-derived — tell the user what is missing rather than inventing it.`,
  };
}

/**
 * The caller's own submissions.
 *
 * Returns UNAVAILABLE (not null) when the route is denied. Collapsing those
 * two is what produced the "you haven't submitted this" bug.
 */
async function mySubmission(orgUnitId: number, folderId: number): Promise<SubmissionResult> {
  let data: unknown;
  try {
    data = await avenue.get(
      "le",
      `${orgUnitId}/dropbox/folders/${folderId}/submissions/mysubmissions/`,
    );
  } catch (err) {
    if (err instanceof AvenueError) return UNAVAILABLE;
    throw err;
  }

  const entries = Array.isArray(data) ? data : isObj(data) ? [data] : [];
  let latest: { submittedAt: Date | null; files: string[] } | null = null;

  for (const entry of entries) {
    if (!isObj(entry)) continue;
    const subs = pick(entry, "Submissions");
    const records = Array.isArray(subs) ? subs : [entry];
    for (const rec of records) {
      if (!isObj(rec)) continue;
      const when = parseD2L(pick(rec, "SubmissionDate", "DateSubmitted", "Date"));
      if (latest === null || (when && (!latest.submittedAt || when > latest.submittedAt))) {
        const files = pick(rec, "Files");
        latest = {
          submittedAt: when,
          files: Array.isArray(files)
            ? files.filter(isObj).map((f) => String(pick(f, "FileName", "Name") ?? ""))
            : [],
        };
      }
    }
  }
  return latest;
}

// --- unified deadline view --------------------------------------------------

export async function getUpcomingDeadlines(args: {
  days_ahead?: number;
  include_submitted?: boolean;
}) {
  const daysAhead = args.days_ahead ?? 14;
  const includeSubmitted = args.include_submitted ?? false;
  const now = nowUtc();
  const windowEnd = addDays(now, daysAhead);

  const { courses } = await listCourses({});
  const deadlines: Array<Record<string, unknown>> = [];
  let anyStatus = false;

  for (const course of courses) {
    const oid = course.org_unit_id;

    // Calendar first: it covers quizzes and anything else an instructor dated.
    try {
      for (const ev of await calendarEvents(oid, { daysBack: 0, daysAhead })) {
        if (ev.dueAt < now || ev.dueAt > windowEnd) continue;
        deadlines.push({
          course_name: course.name,
          org_unit_id: oid,
          title: ev.title,
          type: ev.kind,
          due_date: ev.due_date,
          days_until: daysUntil(ev.dueAt, now),
          submission_status: "unknown",
        });
      }
    } catch {
      // One course's calendar failing must not cost the rest.
    }

    // Then assignment folders, which carry points and real status.
    try {
      for (const folder of await avenue.getPaged("le", `${oid}/dropbox/folders/`)) {
        if (!isObj(folder)) continue;
        const fid = asInt(pick(folder, "Id", "FolderId"));
        const due = parseD2L(pick(folder, "DueDate", "Due"));
        if (fid === null || !due || due < now || due > windowEnd) continue;

        const sub = await mySubmission(oid, fid);
        // THREE outcomes. UNAVAILABLE must not read as "submitted", or these
        // get filtered out below and the answer silently empties.
        let status: string;
        if (sub === UNAVAILABLE) status = "unknown";
        else if (sub !== null) status = "submitted";
        else status = "not_submitted";
        if (status !== "unknown") anyStatus = true;

        const title = String(pick(folder, "Name", "Title") ?? "");
        const existing = deadlines.find(
          (d) =>
            d["org_unit_id"] === oid &&
            String(d["title"] ?? "").trim().toLowerCase() === title.trim().toLowerCase(),
        );

        if (existing) {
          // Match on TITLE, never the timestamp alone: a quiz and an assignment
          // routinely share an 11:59 PM deadline, and matching on time made the
          // quiz inherit the assignment's "submitted" and vanish from the view.
          existing["submission_status"] = status;
          existing["points_possible"] = asFloat(pick(folder, "OutOf", "PointsPossible"));
          existing["folder_id"] = fid;
        } else {
          deadlines.push({
            course_name: course.name,
            org_unit_id: oid,
            title,
            type: "assignment",
            due_date: describe(due),
            days_until: daysUntil(due, now),
            submission_status: status,
            points_possible: asFloat(pick(folder, "OutOf", "PointsPossible")),
            folder_id: fid,
          });
        }
      }
    } catch {
      // Folder listing denied for this course; calendar entries still stand.
    }
  }

  const visible = includeSubmitted
    ? deadlines
    : deadlines.filter((d) => d["submission_status"] !== "submitted");

  visible.sort((a, b) => {
    const au = (a["due_date"] as { utc?: string } | null)?.utc ?? "9999";
    const bu = (b["due_date"] as { utc?: string } | null)?.utc ?? "9999";
    return au.localeCompare(bu) || String(a["course_name"]).localeCompare(String(b["course_name"]));
  });

  return {
    window_days: daysAhead,
    generated_at: toUtcIso(now),
    courses_checked: courses.length,
    deadlines: visible,
    count: visible.length,
    submission_status_available: anyStatus,
    note: anyStatus
      ? "Reflects the course calendar and assignment folders. Items an instructor never dated will not appear."
      : "Submission status is unavailable here, so work you have already handed in may still be listed. Report these as 'due', not as 'not done'.",
  };
}
