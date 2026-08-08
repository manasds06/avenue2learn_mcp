/**
 * The content tree is what the whole file-search feature stands on: if a topic
 * is not reported downloadable, sync never sees it, and the course silently
 * reports "no materials found".
 *
 * That happened. Two causes, both pinned here, and both only visible against
 * the REAL response shape:
 *
 *   1. `content/root/` embeds a STUB Structure — topic nodes carry only
 *      Id/Title/Type and no Url. The `content/modules/{id}/structure/`
 *      endpoint returns the full object. Measured on org unit 759806:
 *      5 keys embedded vs 22 fetched.
 *   2. Deciding "is this a file" from the Url therefore marked every topic
 *      not-downloadable. D2L answers with TopicType/Type (1 = File), which is
 *      present on both shapes.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const getMock = vi.fn();

vi.mock("../src/avenue/client.js", () => ({
  avenue: {
    get: (...args: unknown[]) => getMock(...args),
    getPaged: async () => [],
    path: async (c: string, s: string) => `/d2l/api/${c}/1.0/${s}`,
    baseUrl: async () => "https://avenue.cllmcmaster.ca",
  },
}));

vi.mock("../src/tools/context.js", () => ({
  courseName: async () => "SFWRENG 2FA3",
  clearCourseNames: () => {},
}));

/** Exactly what content/root/ returns: a module whose Structure is stubs. */
const ROOT = [
  {
    Id: 5478228,
    Title: "Course Information",
    Type: 0,
    Structure: [{ Id: 5478232, Title: "2FA3_outline_2026", Type: 1, ShortTitle: "" }],
  },
];

/** What the structure ENDPOINT returns for that module: the full object. */
const STRUCTURE = [
  {
    Id: 5478232,
    Title: "2FA3_outline_2026",
    Type: 1,
    TopicType: 1,
    Url: "/content/enforced/759806-SFWRENG_2FA3_macciov_2261/2FA3_outline_2026.docx",
    LastModifiedDate: "2026-01-04T18:22:11.000Z",
  },
];

describe("get_course_content finds real files", () => {
  beforeEach(() => {
    getMock.mockReset();
    getMock.mockImplementation(async (_component: string, suffix: string) => {
      if (suffix.endsWith("/content/root/")) return ROOT;
      if (suffix.includes("/structure/")) return STRUCTURE;
      return [];
    });
  });

  it("fetches the structure endpoint rather than trusting the embedded stub", async () => {
    const { getCourseContent } = await import("../src/tools/content.js");
    await getCourseContent({ org_unit_id: 759806 });

    const fetched = getMock.mock.calls.map((c) => String(c[1]));
    expect(
      fetched.some((s) => s.includes("/content/modules/5478228/structure/")),
      "the stub Structure has no Url, so it must not be used as-is",
    ).toBe(true);
  });

  it("marks a Type 1 topic downloadable, with the extension from its Url", async () => {
    const { getCourseContent } = await import("../src/tools/content.js");
    const tree = await getCourseContent({ org_unit_id: 759806 });

    const topics = tree.modules.flatMap((m) => m.topics);
    expect(topics).toHaveLength(1);
    expect(topics[0]!.is_downloadable, "a Type 1 topic is a file").toBe(true);
    // Without the extension, extract() cannot dispatch and sync counts the
    // file as "unsupported" — which is how 26 files became 0.
    expect(topics[0]!.file_name).toBe("2FA3_outline_2026.docx");
  });

  it("still recognizes a file topic when no Url is present at all", async () => {
    getMock.mockImplementation(async (_c: string, suffix: string) => {
      if (suffix.endsWith("/content/root/")) return ROOT;
      // Some instances answer the structure endpoint with stubs too.
      if (suffix.includes("/structure/")) return ROOT[0]!.Structure;
      return [];
    });

    const { getCourseContent } = await import("../src/tools/content.js");
    const tree = await getCourseContent({ org_unit_id: 759806 });
    const topics = tree.modules.flatMap((m) => m.topics);

    // Type alone is enough to know it is a file; sync resolves the name later.
    expect(topics[0]!.is_downloadable).toBe(true);
  });

  it("does not treat a Link topic as downloadable", async () => {
    getMock.mockImplementation(async (_c: string, suffix: string) => {
      if (suffix.endsWith("/content/root/")) return ROOT;
      if (suffix.includes("/structure/")) {
        return [{ Id: 99, Title: "Course website", Type: 3, TopicType: 3, Url: "https://example.com" }];
      }
      return [];
    });

    const { getCourseContent } = await import("../src/tools/content.js");
    const tree = await getCourseContent({ org_unit_id: 759806 });
    expect(tree.modules.flatMap((m) => m.topics)[0]!.is_downloadable).toBe(false);
  });
});
