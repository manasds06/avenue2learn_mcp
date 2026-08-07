/**
 * list_announcements — ported from src/avenue_mcp/tools/announcements.py.
 */

import { BASE_URL, avenue } from "../avenue/client.js";
import { describe, nowUtc, parseD2L } from "../avenue/dates.js";
import { asInt, isObj, pick, richText, toTextAndLinks, truncate } from "../avenue/models.js";
import { courseName } from "./context.js";

const BODY_CAP = 4000;

export async function listAnnouncements(args: {
  org_unit_id: number;
  limit?: number;
  since?: string;
}) {
  const { org_unit_id } = args;
  const limit = args.limit ?? 20;
  const now = nowUtc();
  const cutoff = args.since ? parseD2L(args.since) : null;

  const raw = await avenue.getPaged("le", `${org_unit_id}/news/`);
  const items = [];

  for (const entry of raw) {
    if (!isObj(entry)) continue;

    const posted = parseD2L(pick(entry, "StartDate", "DatePosted", "CreatedDate", "PostedDate"));
    const ends = parseD2L(pick(entry, "EndDate"));

    if (ends && ends < now) continue; // expired
    if (cutoff && posted && posted <= cutoff) continue;

    const { text, links } = toTextAndLinks(richText(pick(entry, "Body", "Content")), BASE_URL);
    const capped = truncate(text, BODY_CAP);

    items.push({
      id: asInt(pick(entry, "Id", "NewsId")),
      title: String(pick(entry, "Title", "Subject", "Name") ?? ""),
      body_text: capped.text,
      truncated: capped.truncated,
      posted_at: describe(posted),
      links,
    });
  }

  items.sort((a, b) => (b.posted_at?.utc ?? "").localeCompare(a.posted_at?.utc ?? ""));
  const limited = limit > 0 ? items.slice(0, limit) : items;

  return {
    org_unit_id,
    course_name: await courseName(org_unit_id),
    announcements: limited,
    count: limited.length,
    total_available: items.length,
    since: args.since ?? null,
  };
}
