/**
 * Display formatting that is UI-only and must never touch stored data.
 *
 * Specifically: the course code split. D2L hands back one field that reads
 * `CHEM 1E03:General Chemistry for Engineering I` — code and title jammed
 * together with a colon. The handoff renders them as separate lines, the code
 * in mono, which is how a student actually scans a course list.
 *
 * This is presentation ONLY. The unsplit name is what goes into the chunk
 * context headers and citations, because that is the string a person sees in
 * Brightspace itself and a citation that does not match the source is a
 * citation you cannot check.
 */

export interface CourseTitle {
  /** `CHEM 1E03`, or null when the name carries no code. */
  code: string | null;
  title: string;
}

/**
 * Split on the FIRST colon only.
 *
 * "MATH 1A03:All Sections of Math 1A03 and 1ZA3 — MATH 1ZA3" has to keep
 * everything after the first colon as the title. Splitting on every colon
 * would truncate titles that legitimately contain one.
 *
 * Names with no colon — "Engineering Co-Op Success Course" — get no code
 * rather than a fabricated one, and the card renders without that line.
 */
export function splitCourseTitle(name: string): CourseTitle {
  const at = name.indexOf(":");
  if (at < 0) return { code: null, title: name.trim() };

  const code = name.slice(0, at).trim();
  const title = name.slice(at + 1).trim();

  // A colon with nothing useful on one side is not a code/title pair. Better
  // to show the whole string than to invent a split that mangles it.
  if (!code || !title || code.length > 24) return { code: null, title: name.trim() };

  return { code, title };
}
