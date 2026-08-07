/**
 * list_quizzes — metadata only, deliberately.
 * Ported from src/avenue_mcp/tools/quizzes.py.
 *
 * Names, dates, availability windows, and whether you've attempted. Quiz
 * QUESTIONS and ANSWERS are out of scope and stay that way: knowing a quiz is
 * due Friday is calendar information; retrieving its contents while it is open
 * is the thing academic-integrity policies exist to prohibit.
 *
 * On this instance `quizzes/{id}/attempts/` is 403 (docs/08), so attempt counts
 * come back null rather than zero — "I don't know" and "you haven't taken it"
 * are different answers.
 */

import { avenue } from "../avenue/client.js";
import { AvenueError } from "../avenue/errors.js";
import { daysUntil, describe, nowUtc, parseD2L } from "../avenue/dates.js";
import { asFloat, asInt, isObj, pick } from "../avenue/models.js";
import { calendarEvents } from "./calendar.js";
import { courseName } from "./context.js";

export async function listQuizzes(args: {
  org_unit_id: number;
  include_completed?: boolean;
}) {
  const { org_unit_id } = args;
  const includeCompleted = args.include_completed ?? true;
  const now = nowUtc();

  let raw: unknown[];
  try {
    raw = await avenue.getPaged("le", `${org_unit_id}/quizzes/`);
  } catch (err) {
    if (err instanceof AvenueError && err.kind === "PermissionDenied") {
      return calendarFallback(org_unit_id, err.message);
    }
    throw err;
  }

  const quizzes = [];
  for (const q of raw) {
    if (!isObj(q)) continue;
    const quizId = asInt(pick(q, "QuizId", "Id"));
    if (quizId === null) continue;

    const start = parseD2L(pick(q, "StartDate", "AvailabilityStartDate"));
    const due = parseD2L(pick(q, "DueDate", "Due"));
    const end = parseD2L(pick(q, "EndDate", "AvailabilityEndDate"));

    const { used, best } = await attempts(org_unit_id, quizId);

    // Three dates, three meanings. A quiz can be submittable AFTER due but
    // BEFORE end — telling a student it is closed when it is merely late is as
    // damaging as the reverse.
    const openNow = (!start || start <= now) && (!end || now <= end);
    const pastDueOpen = Boolean(due && now > due && (!end || now <= end));

    const status = used === null ? "unknown" : used > 0 ? "attempted" : "not_attempted";
    if (status === "attempted" && !includeCompleted) continue;

    quizzes.push({
      quiz_id: quizId,
      name: String(pick(q, "Name", "Title") ?? ""),
      start_date: describe(start),
      due_date: describe(due),
      end_date: describe(end),
      days_until_due: daysUntil(due, now),
      attempts_allowed: asInt(pick(q, "AttemptsAllowed", "MaxAttempts", "NumberOfAttemptsAllowed")),
      attempts_used: used,
      status,
      is_available_now: openNow,
      is_past_due_but_open: pastDueOpen,
      is_closed: Boolean(end && now > end),
      best_score: best,
    });
  }

  quizzes.sort(
    (a, b) =>
      (a.due_date?.utc ?? "9999").localeCompare(b.due_date?.utc ?? "9999") ||
      a.name.localeCompare(b.name),
  );

  const statusUnknown = quizzes.some((q) => q.status === "unknown");

  return {
    org_unit_id,
    course_name: await courseName(org_unit_id),
    quizzes,
    count: quizzes.length,
    degraded: false,
    attempt_status_available: !statusUnknown,
    note:
      "Quiz metadata only — questions and answers are never retrieved." +
      (statusUnknown
        ? " Attempt status is unavailable on this instance, so status is 'unknown'. Do not tell the user whether they have taken a quiz; point them at Avenue."
        : ""),
  };
}

/**
 * Attempt count and best score.
 *
 * Returns null (not 0) when the route is denied. Reporting zero attempts on
 * silence would tell a student they haven't taken a quiz they may well have.
 */
async function attempts(
  orgUnitId: number,
  quizId: number,
): Promise<{ used: number | null; best: number | null }> {
  let data: unknown[];
  try {
    data = await avenue.getPaged("le", `${orgUnitId}/quizzes/${quizId}/attempts/`);
  } catch {
    return { used: null, best: null };
  }

  let used = 0;
  let best: number | null = null;
  for (const att of data) {
    if (!isObj(att)) continue;
    used++;
    const score = asFloat(pick(att, "Score", "PointsEarned"));
    if (score !== null && (best === null || score > best)) best = score;
  }
  return { used, best };
}

/** Deadlines survive a blocked quizzes route; attempt status does not. */
async function calendarFallback(orgUnitId: number, reason: string) {
  let events: Awaited<ReturnType<typeof calendarEvents>> = [];
  try {
    events = await calendarEvents(orgUnitId);
  } catch {
    events = [];
  }

  const quizzes = events
    .filter((ev) => ev.kind === "quiz")
    .map((ev) => ({
      quiz_id: null,
      name: ev.title,
      due_date: ev.due_date,
      days_until_due: daysUntil(ev.dueAt),
      attempts_allowed: null,
      attempts_used: null,
      status: "unknown" as const,
      is_available_now: null,
      is_past_due_but_open: null,
    }));

  return {
    org_unit_id: orgUnitId,
    course_name: await courseName(orgUnitId),
    quizzes,
    count: quizzes.length,
    degraded: true,
    attempt_status_available: false,
    note:
      "The quizzes API is not available to student accounts on this instance, so " +
      "attempt status and availability windows are unknown. These entries come " +
      `from the course calendar. (${reason})`,
  };
}
