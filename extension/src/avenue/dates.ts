/**
 * UTC <-> America/Toronto, ported from src/avenue_mcp/util/dates.py.
 *
 * Brightspace returns UTC; McMaster deadlines are set in Eastern and typically
 * land at 11:59 PM local, which is 03:59 or 04:59 UTC *the next day* depending
 * on DST. Reporting "due March 16" for a March 15 deadline is worse than
 * reporting nothing, so every date we surface carries both.
 *
 * One thing the browser makes easier than Python: Intl has the tz database
 * built in, so there is no `tzdata` dependency to forget (which broke every
 * deadline on Windows — see the merge notes).
 */

export const TZ = "America/Toronto";

export interface DateBlock {
  /** ISO-8601 UTC. What a model should sort and compare on. */
  utc: string;
  /** e.g. "Sun 15 Mar 2026, 11:59 PM EDT". What a human should be told. */
  local: string;
  /** e.g. "2026-03-15". The local calendar day, which is the trap. */
  local_date: string;
}

/**
 * Parse a Valence date field. Returns null for null/blank/garbage rather than
 * throwing — a missing due date is normal data, not an error.
 */
export function parseD2L(value: unknown): Date | null {
  if (value === null || value === undefined || value === "") return null;

  if (typeof value === "number") {
    // Epoch milliseconds. Unambiguous in practice: a seconds value this large
    // would be year 33658.
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  if (typeof value !== "string") return null;

  const d = new Date(value.trim());
  return Number.isNaN(d.getTime()) ? null : d;
}

/** ISO-8601 UTC with a `Z`, matching Brightspace's own style. */
export function toUtcIso(d: Date | null): string | null {
  return d ? d.toISOString().replace(/\.\d{3}Z$/, "Z") : null;
}

/**
 * Format for a Valence UTCDateTime *query parameter*.
 *
 * The milliseconds are REQUIRED, not cosmetic. calendar/events/myEvents/
 * rejects the second-precision form with the same 400 it returns for omitting
 * the parameter entirely — measured against avenue.cllmcmaster.ca, le 1.96:
 *
 *     ...T13:34:45Z      -> 400 Invalid Parameters
 *     ...T13:34:45.000Z  -> 200
 */
export function toUtcParam(d: Date): string {
  return d.toISOString(); // always ...THH:MM:SS.sssZ
}

const partsOf = (d: Date) =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ,
    weekday: "short",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZoneName: "short",
  }).formatToParts(d);

/** "Sun 15 Mar 2026, 11:59 PM EDT" */
export function renderLocal(d: Date | null): string | null {
  if (!d) return null;
  const p = Object.fromEntries(partsOf(d).map((x) => [x.type, x.value]));
  return `${p.weekday} ${p.day} ${p.month} ${p.year}, ${p.hour}:${p.minute} ${p.dayPeriod} ${p.timeZoneName}`;
}

/** The local calendar date, "2026-03-15". */
export function localDate(d: Date | null): string | null {
  if (!d) return null;
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

/** The standard block every tool returns for a date. */
export function describe(d: Date | null): DateBlock | null {
  if (!d) return null;
  return {
    utc: toUtcIso(d)!,
    local: renderLocal(d)!,
    local_date: localDate(d)!,
  };
}

/** Fractional days from now until `d`. Negative when already past. */
export function daysUntil(d: Date | null, now: Date = new Date()): number | null {
  if (!d) return null;
  return Math.round(((d.getTime() - now.getTime()) / 86_400_000) * 100) / 100;
}

export function nowUtc(): Date {
  return new Date();
}

export function addDays(d: Date, days: number): Date {
  return new Date(d.getTime() + days * 86_400_000);
}
