/**
 * The bridge between views and the tool layer.
 *
 * Views are a SECOND CONSUMER of the same tools the model calls — not a bypass.
 * Same dispatch, same `PANEL_TOOLS` split, same `coerceArgs`. That matters: if
 * a view read Brightspace directly it could drift from the rules the tools
 * enforce, and "the honest thing" would depend on which surface you looked at.
 *
 * It also means Home, Courses, and Course cost ZERO tokens. A student glancing
 * at their deadlines should not spend quota, and should still get an answer
 * when their API key is missing or exhausted.
 */

import { AvenueError } from "../../avenue/errors.js";
import { PANEL_TOOLS, TOOLS_BY_NAME, coerceArgs } from "../../tools/registry.js";

export type ToolResult =
  | { ok: true; result: unknown }
  | { ok: false; error: string; message: string; next_step?: string };

/**
 * Run a tool wherever it can actually run.
 *
 * Live-data tools go to the service worker, which owns network access. File
 * tools run in this page: a service worker has no DOMParser and no pdf.js
 * worker, and MV3 kills it long before a sync finishes.
 */
/**
 * Told to the worker once, when this module first loads — which is when the
 * panel opens.
 *
 * Responses are cached so moving between views costs nothing, but opening the
 * panel is the moment a student expects to be looking at current information.
 * The worker drops grades, quiz attempts and submissions on this signal and
 * keeps the slow structural reads that make the first paint fast.
 *
 * EVERY dispatch awaits it. Gating in the shell component instead would race:
 * views call their tools on mount, and losing that race serves exactly the
 * stale grades this exists to clear. One message round trip, paid once.
 */
const panelOpened: Promise<unknown> = chrome.runtime
  .sendMessage({ type: "panel-opened" })
  .catch(() => undefined);

export async function callTool(
  name: string,
  args: Record<string, unknown> = {},
): Promise<ToolResult> {
  await panelOpened;

  if (!PANEL_TOOLS.has(name)) {
    return chrome.runtime.sendMessage({ type: "tool", name, args });
  }

  const tool = TOOLS_BY_NAME[name];
  if (!tool) {
    return { ok: false, error: "UnknownTool", message: `No tool named ${name}.` };
  }

  try {
    return { ok: true, result: await tool.handler(coerceArgs(tool, args)) };
  } catch (err) {
    if (err instanceof AvenueError) return { ok: false, ...err.toResult() };
    console.error("panel tool failed", name, err);
    return {
      ok: false,
      error: "Unexpected",
      message: err instanceof Error ? err.message : String(err),
      next_step: "Check the side panel console for a traceback.",
    };
  }
}

// --- cache ------------------------------------------------------------------

interface Entry {
  at: number;
  value: ToolResult;
}

const cache = new Map<string, Entry>();

/**
 * Short TTLs, and NOT the same for everything.
 *
 * Grades and submission status are never cached in the Python client for a
 * reason: telling someone they got 92 when it was regraded to 78 is worse than
 * a second request. Course lists barely change within a session.
 */
const TTL_MS: Record<string, number> = {
  list_courses: 5 * 60_000,
  get_course_content: 5 * 60_000,
  list_announcements: 60_000,
  get_upcoming_deadlines: 30_000,
  get_status: 15_000,
  // Deliberately absent, so they fall to 0: get_grades, analyze_grade_summary,
  // list_assignments, list_quizzes, get_whats_new.
};

const keyOf = (name: string, args: Record<string, unknown>) =>
  `${name}:${JSON.stringify(args, Object.keys(args).sort())}`;

export async function cachedTool(
  name: string,
  args: Record<string, unknown> = {},
  opts: { force?: boolean } = {},
): Promise<ToolResult> {
  const key = keyOf(name, args);
  const ttl = TTL_MS[name] ?? 0;

  if (!opts.force && ttl > 0) {
    const hit = cache.get(key);
    if (hit && Date.now() - hit.at < ttl) return hit.value;
  }

  const value = await callTool(name, args);
  // Only cache success. A cached failure is the bug that put
  // "Course 759806" into every chunk header for a whole session.
  if (value.ok && ttl > 0) cache.set(key, { at: Date.now(), value });
  return value;
}

/** Called after anything that changes server or index state. */
export function invalidate(prefix?: string): void {
  if (!prefix) {
    cache.clear();
    return;
  }
  for (const key of [...cache.keys()]) {
    if (key.startsWith(prefix)) cache.delete(key);
  }
}
