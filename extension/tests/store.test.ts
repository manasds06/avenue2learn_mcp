/**
 * Does the index actually persist?
 *
 * A sync reported 26 files and 128 chunks, then the very next search returned
 * 0 results and the model re-synced to recover. Either the writes never
 * committed or the reads could not see them — and both look identical from
 * outside: "your course materials don't mention that".
 *
 * IndexedDB transactions auto-commit as soon as the task queue drains with no
 * pending request. Awaiting anything non-IDB mid-transaction can therefore
 * close it early, and the writes after that point are silently lost. That is
 * exactly the shape of this store's helper, so it needs a real round-trip test
 * rather than a reading.
 */

import "fake-indexeddb/auto";
import { beforeEach, describe, expect, it } from "vitest";

import {
  clearAll,
  countChunks,
  docKey,
  getDocument,
  listDocuments,
  loadChunks,
  putDocument,
  getMeta,
  setMeta,
  type StoredChunk,
  type StoredDocument,
} from "../src/rag/store.js";

function doc(orgUnitId: number, topicId: number, name = "outline.docx"): StoredDocument {
  return {
    key: docKey(orgUnitId, topicId),
    orgUnitId,
    topicId,
    courseName: "SFWRENG 2FA3",
    modulePath: "Course Info",
    title: name,
    fileName: name,
    mimeType: "",
    lastModified: "2026-01-04T18:22:11.000Z",
    contentHash: "abc123",
    indexedAt: new Date().toISOString(),
    extractionQuality: "ok",
    pageCount: 3,
    text: "Assignment 1 is worth 5%.",
  };
}

function chunks(orgUnitId: number, topicId: number, n: number): StoredChunk[] {
  return Array.from({ length: n }, (_, i) => ({
    docKey: docKey(orgUnitId, topicId),
    orgUnitId,
    position: `p.${i + 1}`,
    text: `chunk ${i}`,
    embedded: `[SFWRENG 2FA3 — Course Info — outline.docx, p.${i + 1}]\n\nchunk ${i}`,
    tokenCount: 10,
    vector: new Float32Array([i, 1, 0]),
  }));
}

describe("a written document survives and can be read back", () => {
  beforeEach(async () => {
    await clearAll();
  });

  it("round-trips a document and its chunks", async () => {
    await putDocument(doc(759806, 5478232), chunks(759806, 5478232, 5));

    expect(await getDocument(759806, 5478232)).toMatchObject({
      courseName: "SFWRENG 2FA3",
      fileName: "outline.docx",
    });
    expect(await countChunks(759806)).toBe(5);
    expect(await loadChunks(759806)).toHaveLength(5);
  });

  it("keeps vectors as real Float32Arrays, not plain objects", async () => {
    await putDocument(doc(759806, 1), chunks(759806, 1, 2));
    const [first] = await loadChunks(759806);
    // A vector deserialized as {0:…,1:…} silently scores 0 against every
    // query, which reads as "nothing relevant found".
    expect(first!.vector).toBeInstanceOf(Float32Array);
    expect(first!.vector!.length).toBe(3);
  });

  it("survives many documents written in sequence, as a sync does", async () => {
    // The real failure was at scale: 26 files, one after another.
    for (let topic = 1; topic <= 26; topic++) {
      await putDocument(doc(759806, topic, `file${topic}.pdf`), chunks(759806, topic, 5));
    }
    expect(await listDocuments(759806)).toHaveLength(26);
    expect(await countChunks(759806)).toBe(130);
  });

  it("replaces a document's chunks rather than duplicating them", async () => {
    await putDocument(doc(759806, 1), chunks(759806, 1, 5));
    await putDocument(doc(759806, 1), chunks(759806, 1, 3));
    // A re-sync must not leave the old chunks behind, or every search returns
    // stale passages alongside current ones.
    expect(await countChunks(759806)).toBe(3);
  });

  it("scopes by course, so one course's chunks are never another's candidates", async () => {
    await putDocument(doc(759806, 1), chunks(759806, 1, 4));
    await putDocument(doc(719899, 1), chunks(719899, 1, 6));

    expect(await loadChunks(759806)).toHaveLength(4);
    expect(await loadChunks(719899)).toHaveLength(6);
    expect(await loadChunks()).toHaveLength(10);
  });

  it("persists the embed-model marker", async () => {
    await setMeta("embed_model", "bge-small-en-v1.5");
    expect(await getMeta("embed_model")).toBe("bge-small-en-v1.5");
  });
});

describe("model-supplied arguments are coerced before they reach a key lookup", () => {
  it("a string org_unit_id still finds the course's chunks", async () => {
    const { TOOLS_BY_NAME, coerceArgs } = await import("../src/tools/registry.js");

    await clearAll();
    await putDocument(doc(759806, 1), chunks(759806, 1, 4));

    // An LLM returning JSON will sometimes hand back "759806" where the schema
    // says integer. IndexedDB key equality is TYPE-STRICT, so the raw string
    // matches ZERO rows — a fully populated index reporting "nothing found".
    const raw = { org_unit_id: "759806" as unknown as number };
    expect(await loadChunks(raw.org_unit_id)).toHaveLength(0);

    const fixed = coerceArgs(TOOLS_BY_NAME["search_course_materials"]!, raw);
    expect(fixed["org_unit_id"]).toBe(759806);
    expect(await loadChunks(fixed["org_unit_id"] as number)).toHaveLength(4);
  });

  it("coerces booleans and leaves unparseable values alone", async () => {
    const { TOOLS_BY_NAME, coerceArgs } = await import("../src/tools/registry.js");

    const sync = coerceArgs(TOOLS_BY_NAME["sync_course_materials"]!, {
      org_unit_id: "42",
      force: "true",
    });
    expect(sync).toEqual({ org_unit_id: 42, force: true });

    // Garbage is passed through so the handler can report something useful
    // rather than operating on NaN.
    const bad = coerceArgs(TOOLS_BY_NAME["get_grades"]!, { org_unit_id: "not-a-number" });
    expect(bad["org_unit_id"]).toBe("not-a-number");
  });

  it("is applied at BOTH dispatch points, not just one", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const root = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
    // Tools are split across the worker and the panel; coercing in only one
    // would leave the file tools — the ones that hit IndexedDB — unprotected.
    for (const file of ["src/background/worker.ts", "src/ui/sidepanel.ts"]) {
      expect(readFileSync(join(root, file), "utf8"), file).toContain("coerceArgs(tool, args)");
    }
  });
});
