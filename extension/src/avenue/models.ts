/**
 * Shared normalization helpers, ported from src/avenue_mcp/client/models.py.
 *
 * Valence is inconsistent about field names across routes and versions, so
 * every read goes through `pick` with the observed aliases rather than
 * indexing a key directly.
 */

export type Json = Record<string, unknown>;

export const isObj = (v: unknown): v is Json =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** First present, non-empty value among `keys`. */
export function pick(obj: unknown, ...keys: string[]): unknown {
  if (!isObj(obj)) return undefined;
  for (const k of keys) {
    const v = obj[k];
    if (v !== undefined && v !== null && v !== "") return v;
  }
  return undefined;
}

export function asInt(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return Math.trunc(v);
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    if (Number.isFinite(n)) return Math.trunc(n);
  }
  return null;
}

export function asFloat(v: unknown): number | null {
  if (typeof v === "boolean") return null;
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/** Unwrap a D2L RichText block ({Html, Text}) or a bare string. */
export function richText(v: unknown): string {
  if (typeof v === "string") return v;
  if (isObj(v)) {
    const html = v["Html"];
    const text = v["Text"];
    if (typeof html === "string" && html) return html;
    if (typeof text === "string" && text) return text;
  }
  return "";
}

const INSTRUCTOR_HINTS = ["instructor", "teacher", "professor", "faculty", "coordinator"];
const TA_HINTS = ["teaching assistant", "assistant", "marker", "grader"];

/**
 * Map a D2L role label onto Instructor / TA / Student / Unknown.
 *
 * Only the role is ever stored — never the author's name. See docs/07.
 */
export function normalizeRole(raw: unknown): "Instructor" | "TA" | "Student" | "Unknown" {
  if (raw === null || raw === undefined) return "Unknown";
  const text = String(isObj(raw) ? (pick(raw, "Name", "Code", "RoleName") ?? "") : raw)
    .trim()
    .toLowerCase();
  if (!text) return "Unknown";
  if (INSTRUCTOR_HINTS.some((h) => text.includes(h))) return "Instructor";
  if (text === "ta" || text.startsWith("ta ") || TA_HINTS.some((h) => text.includes(h))) return "TA";
  if (text.includes("student") || text.includes("learner")) return "Student";
  return "Unknown";
}

/** HTML -> readable plain text. Announcement and instruction bodies are HTML. */
export function toText(html: string): string {
  if (!html) return "";
  return html
    .replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|li|tr|h[1-6]|blockquote|section|article)>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/[ \t]+/g, " ")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .join("\n")
    .trim();
}

export function truncate(text: string, cap: number): { text: string; truncated: boolean } {
  if (text.length <= cap) return { text, truncated: false };
  return { text: text.slice(0, cap), truncated: true };
}
