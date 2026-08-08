/**
 * Parity with the Python server.
 *
 * Two implementations of the same product is the cost we accepted to get a
 * browser extension, and drift is the risk that decision carries. These tests
 * read the Python source directly, so a tool added on one side and not the
 * other fails here rather than being discovered by a user getting different
 * answers from Claude Code and the extension.
 *
 * They check NAMES and BEHAVIOURAL RULES, not implementation — the two are
 * written in different languages against different runtimes, and demanding
 * structural similarity would be noise.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const REPO = join(ROOT, "..");

const py = (rel: string) => readFileSync(join(REPO, "src/avenue_mcp", rel), "utf8");
const ts = (rel: string) => readFileSync(join(ROOT, "src", rel), "utf8");

describe("the two implementations expose the same tools", () => {
  const pythonTools = new Set(
    [...py("server.py").matchAll(/^async def ([a-z_]+)\(/gm)].map((m) => m[1]!),
  );
  const extensionTools = new Set(
    [...ts("tools/registry.ts").matchAll(/^    name: "([a-z_]+)"/gm)].map((m) => m[1]!),
  );

  it("finds tools on both sides", () => {
    expect(pythonTools.size).toBeGreaterThan(10);
    expect(extensionTools.size).toBeGreaterThan(10);
  });

  it("has no tool the extension is missing", () => {
    const missing = [...pythonTools].filter((t) => !extensionTools.has(t));
    expect(missing, `Python has tools the extension lacks: ${missing.join(", ")}`).toEqual([]);
  });

  it("has no tool the Python server is missing", () => {
    const extra = [...extensionTools].filter((t) => !pythonTools.has(t));
    expect(extra, `the extension has tools Python lacks: ${extra.join(", ")}`).toEqual([]);
  });
});

describe("the honesty rules survived the port", () => {
  /**
   * Each of these was a shipped bug, in Python or TypeScript or both. They all
   * have the same shape: SILENCE REPORTED AS A DEFINITE ANSWER. Losing one in
   * translation is the most likely way this port goes quietly wrong, because
   * the wrong answer looks perfectly confident.
   */

  it("submission status has three states, not two", () => {
    const source = ts("tools/assignments.ts");
    expect(source).toContain("UNAVAILABLE");
    expect(source).toMatch(/status = "unknown"/);
    // A denied route must never read as "not submitted".
    expect(source).not.toMatch(/=\s*sub\s*\?\s*"submitted"\s*:\s*"not_submitted"/);
  });

  it("quiz attempt status has three states, not two", () => {
    const source = ts("tools/quizzes.ts");
    expect(source).toMatch(/used === null \? "unknown"/);
  });

  it("the grade projection refuses when weights do not reconcile", () => {
    const source = ts("tools/grades.ts");
    expect(source).toContain("projectionSafe");
    expect(source).toContain("Target projection withheld");
    // And never claims a letter grade.
    expect(source).toContain("letter_estimate");
    expect(source).toMatch(/cutoffs vary by faculty/i);
  });

  it("the student roster is never returned", () => {
    const source = ts("tools/classlist.ts");
    expect(source).toMatch(/students: \[\] as Person\[\]/);
    expect(source).toContain("student_roster_returned: false");
  });

  it("discussion posts carry roles, never author names", () => {
    const source = ts("tools/discussions.ts");
    expect(source).toContain("author_role");
    expect(source).not.toMatch(/author_name|authorName/);
  });

  it("search always reports what is indexed, so empty is unambiguous", () => {
    const source = ts("tools/materials.ts");
    // "the materials don't say" and "you never synced that course" are
    // completely different answers to give a user.
    expect(source).toContain("indexed_courses");
  });

  it("a watermark advances only for a category that succeeded", () => {
    const source = ts("tools/whatsnew.ts");
    expect(source).toContain("Watermark deliberately NOT advanced");
    expect(source).toContain("mark_seen");
  });
});

describe("facts measured against the live instance are carried over", () => {
  it("the calendar window uses millisecond timestamps", () => {
    // Valence rejects second precision with the SAME 400 it returns for
    // omitting the parameter entirely.
    expect(ts("avenue/dates.ts")).toMatch(/toUtcParam/);
    expect(ts("tools/calendar.ts")).toContain("toUtcParam");
  });

  it("classlist is requested under le, not lp", () => {
    expect(ts("tools/classlist.ts")).toMatch(/getPaged\("le",/);
    expect(ts("tools/classlist.ts")).not.toMatch(/getPaged\("lp",\s*`\$\{org_unit_id\}\/classlist/);
  });

  it("roles are read from ClasslistRoleDisplayName", () => {
    // The only key actually present; without it every entry normalized to
    // Unknown and a course with 11 staff reported none.
    expect(ts("tools/classlist.ts")).toContain("ClasslistRoleDisplayName");
  });

  it("a file topic is identified by type, not by having a Url", () => {
    // content/root/ embeds stubs with no Url; requiring one found zero files
    // in a course with 26.
    expect(ts("tools/content.ts")).toContain("FILE_TOPIC_TYPES");
  });
});
