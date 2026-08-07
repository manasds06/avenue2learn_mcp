/**
 * Shared calendar access, used by assignments, deadlines, and quizzes.
 *
 * In the Python version this lives inside tools/assignments.py and the other
 * modules reach across with a function-body import — a wart its own HANDOFF
 * flags as a cleanup. Extracted here instead.
 *
 * TWO THINGS THIS ROUTE WILL PUNISH YOU FOR (both measured, docs/08):
 *
 *   1. `startDateTime` and `endDateTime` are REQUIRED. Omit them and you get
 *      400, which reads as "no deadlines" rather than "malformed request".
 *   2. The timestamps need MILLISECONDS. `...T13:34:45Z` is rejected with the
 *      exact same 400 as omitting the parameter, which makes the mistake very
 *      hard to spot.
 */

import { avenue } from "../avenue/client.js";
import { addDays, describe, nowUtc, parseD2L, toUtcParam } from "../avenue/dates.js";
import { isObj, pick, richText, toText } from "../avenue/models.js";

export type EventKind = "assignment" | "quiz" | "discussion" | "event";

export interface CalendarEvent {
  title: string;
  dueAt: Date;
  due_date: ReturnType<typeof describe>;
  kind: EventKind;
  description: string | null;
}

const QUIZ_WORDS = ["quiz", "test", "exam", "midterm"];

/** Best-effort classification from whatever text the event carries. */
export function classify(raw: unknown, title: string): EventKind {
  const hay = [
    title,
    String(pick(raw, "AssociationType") ?? ""),
    String(pick(raw, "EventType") ?? ""),
  ]
    .join(" ")
    .toLowerCase();

  if (hay.includes("dropbox") || hay.includes("assignment")) return "assignment";
  if (QUIZ_WORDS.some((w) => hay.includes(w))) return "quiz";
  if (hay.includes("discussion")) return "discussion";
  return "event";
}

export async function calendarEvents(
  orgUnitId: number,
  opts: { daysBack?: number; daysAhead?: number } = {},
): Promise<CalendarEvent[]> {
  const now = nowUtc();
  const raw = await avenue.getPaged("le", `${orgUnitId}/calendar/events/myEvents/`, {
    startDateTime: toUtcParam(addDays(now, -(opts.daysBack ?? 30))),
    endDateTime: toUtcParam(addDays(now, opts.daysAhead ?? 120)),
  });

  const out: CalendarEvent[] = [];
  for (const ev of raw) {
    if (!isObj(ev)) continue;
    const title = String(pick(ev, "Title", "Name") ?? "").trim();
    const dueAt = parseD2L(
      pick(ev, "EndDateTime", "EndDate", "StartDateTime", "StartDate", "DueDate"),
    );
    if (!title || !dueAt) continue;

    const desc = toText(richText(pick(ev, "Description")));
    out.push({
      title,
      dueAt,
      due_date: describe(dueAt),
      kind: classify(ev, title),
      description: desc || null,
    });
  }
  return out;
}
