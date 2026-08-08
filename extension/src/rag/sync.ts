/**
 * sync_course_materials: discover -> diff -> download -> extract -> chunk ->
 * embed -> store.
 *
 * The one heavyweight operation. Always user-initiated — never on a timer,
 * never triggered automatically by a search miss. A background daemon hitting
 * a university system on a schedule is exactly the traffic pattern this
 * project committed to avoiding.
 *
 * Runs in the SIDE PANEL, not the service worker: MV3 kills a worker after
 * ~30s idle and a first sync takes minutes. The panel lives while it is open,
 * which is honest — the user watches the progress of a job they started.
 */

import { avenue } from "../avenue/client.js";
import { AvenueError } from "../avenue/errors.js";
import { asInt, isObj, pick } from "../avenue/models.js";
import { getCourseContent } from "../tools/content.js";
import { courseName } from "../tools/context.js";
import { chunkSegments, type ChunkContext } from "./chunk.js";
import { assertModelMatches, embed, recordModel } from "./embed.js";
import { extract, ExtractionError, isSupported } from "./extract.js";
import {
  docKey,
  getDocument,
  hashBytes,
  putDocument,
  type StoredChunk,
} from "./store.js";

/** One pathological file must not stall a whole course. */
const MAX_FILE_BYTES = 40 * 1024 * 1024;

export interface SyncProgress {
  phase: string;
  done: number;
  total: number;
}

export interface SyncError {
  file_name: string;
  reason: string;
}

export interface SyncResult {
  org_unit_id: number;
  course_name: string;
  files_found: number;
  files_indexed: number;
  files_skipped_unchanged: number;
  files_skipped_unsupported: number;
  chunks_created: number;
  errors: SyncError[];
  duration_seconds: number;
  note: string | null;
}

interface Topic {
  topicId: number;
  title: string;
  fileName: string;
  modulePath: string;
  lastModified: string | null;
}

/** Walk the content tree, flattening to downloadable file topics. */
async function discover(orgUnitId: number): Promise<{ topics: Topic[]; complete: boolean }> {
  const tree = await getCourseContent({ org_unit_id: orgUnitId });
  const topics: Topic[] = [];

  const walk = (
    modules: Array<{ title: string; topics: unknown[]; modules: unknown[] }>,
    path: string[],
  ): void => {
    for (const mod of modules) {
      const here = [...path, mod.title].filter(Boolean);
      for (const t of mod.topics as unknown as Array<Record<string, unknown>>) {
        if (!t["is_downloadable"]) continue;
        const id = asInt(t["id"]);
        if (id === null) continue;
        const fileName = String(t["file_name"] ?? "");
        topics.push({
          topicId: id,
          title: String(t["title"] ?? fileName),
          fileName,
          modulePath: here.join(" / "),
          lastModified:
            (t["last_modified"] as { utc?: string } | null)?.utc ?? null,
        });
      }
      walk(mod.modules as never, here);
    }
  };

  walk(tree.modules as never, []);
  for (const t of tree.topics as unknown as Array<Record<string, unknown>>) {
    if (!t["is_downloadable"]) continue;
    const id = asInt(t["id"]);
    const fileName = String(t["file_name"] ?? "");
    if (id !== null) {
      topics.push({
        topicId: id,
        title: String(t["title"] ?? fileName),
        fileName,
        modulePath: "",
        lastModified: (t["last_modified"] as { utc?: string } | null)?.utc ?? null,
      });
    }
  }

  // A file name without an extension cannot be parsed — extract() dispatches
  // on it — and would be silently counted as "unsupported". That is exactly
  // how a whole course reported zero files. Resolve the real Url from the
  // topic's own metadata instead of guessing.
  for (const topic of topics) {
    if (topic.fileName.includes(".")) continue;
    try {
      const meta = await avenue.get("le", `${orgUnitId}/content/topics/${topic.topicId}`);
      const url = String(pick(meta, "Url", "Location") ?? "");
      const tail = url.split("?")[0]?.split("/").pop() ?? "";
      if (tail.includes(".")) topic.fileName = decodeURIComponent(tail);
    } catch {
      // Leave it; it will be reported as unsupported rather than crashing.
    }
  }

  return { topics, complete: tree.complete };
}

/** Raw bytes for one topic. The content API returns a body, not JSON. */
export async function downloadTopic(orgUnitId: number, topicId: number): Promise<ArrayBuffer> {
  const path = await avenue.path("le", `${orgUnitId}/content/topics/${topicId}/file`);
  const base = await avenue.baseUrl();

  const resp = await fetch(new URL(path, base).toString(), {
    credentials: "include",
    headers: { Accept: "*/*" },
  });

  if (!resp.ok) {
    if (resp.status === 403 || resp.status === 401) {
      throw new AvenueError(
        "PermissionDenied",
        `Could not download topic ${topicId} (HTTP ${resp.status}).`,
        "You may not have access to this file, or your session may have expired.",
      );
    }
    throw new AvenueError(
      "Upstream",
      `Download of topic ${topicId} failed with HTTP ${resp.status}.`,
      "Try again in a moment.",
    );
  }

  const length = Number(resp.headers.get("content-length") ?? 0);
  if (length > MAX_FILE_BYTES) {
    throw new ExtractionError(
      `File is ${Math.round(length / 1e6)} MB, over the ${MAX_FILE_BYTES / 1e6} MB cap.`,
    );
  }
  return resp.arrayBuffer();
}

export async function syncCourse(
  orgUnitId: number,
  opts: { force?: boolean; onProgress?: (p: SyncProgress) => void } = {},
): Promise<SyncResult> {
  const started = performance.now();
  const report = opts.onProgress ?? (() => {});

  // Refuse before doing work if the index was built by a different model.
  await assertModelMatches();

  const name = await courseName(orgUnitId);
  report({ phase: "Reading the course content tree", done: 0, total: 1 });

  const { topics, complete } = await discover(orgUnitId);
  const errors: SyncError[] = [];
  let indexed = 0;
  let unchanged = 0;
  let unsupported = 0;
  let chunksCreated = 0;

  for (const [i, topic] of topics.entries()) {
    report({ phase: `Indexing ${topic.fileName}`, done: i, total: topics.length });

    if (!isSupported(topic.fileName)) {
      unsupported++;
      continue;
    }

    try {
      // Timestamp first, so the common case — nothing changed — costs one tree
      // walk and zero downloads.
      const existing = await getDocument(orgUnitId, topic.topicId);
      if (
        !opts.force &&
        existing &&
        topic.lastModified &&
        existing.lastModified === topic.lastModified
      ) {
        unchanged++;
        continue;
      }

      const bytes = await downloadTopic(orgUnitId, topic.topicId);
      const hash = await hashBytes(bytes);

      // Hash catches a re-upload that reset the timestamp without changing
      // anything.
      if (!opts.force && existing && existing.contentHash === hash) {
        unchanged++;
        continue;
      }

      const extraction = await extract(bytes, topic.fileName);

      if (extraction.quality === "poor") {
        // Raised BEFORE anything is stored. The Python side had this backwards
        // once: a failed extraction wrote the document row anyway, so the
        // course looked indexed forever while search returned nothing and the
        // model reported "not in the materials".
        errors.push({
          file_name: topic.fileName,
          reason: extraction.reason ?? "No usable text could be extracted.",
        });
        continue;
      }

      const ctx: ChunkContext = {
        courseName: name,
        modulePath: topic.modulePath,
        title: topic.title,
        fileName: topic.fileName,
      };
      const chunks = chunkSegments(extraction.segments, ctx);
      if (!chunks.length) {
        errors.push({ file_name: topic.fileName, reason: "Produced no chunks." });
        continue;
      }

      report({
        phase: `Embedding ${topic.fileName} (${chunks.length} chunks)`,
        done: i,
        total: topics.length,
      });
      const vectors = await embed(
        chunks.map((c) => c.embedded),
        (msg) => report({ phase: msg, done: i, total: topics.length }),
      );

      const stored: StoredChunk[] = chunks.map((c, j) => ({
        docKey: docKey(orgUnitId, topic.topicId),
        orgUnitId,
        position: c.position,
        text: c.text,
        embedded: c.embedded,
        tokenCount: c.tokenCount,
        vector: vectors[j],
      }));

      await putDocument(
        {
          key: docKey(orgUnitId, topic.topicId),
          orgUnitId,
          topicId: topic.topicId,
          courseName: name,
          modulePath: topic.modulePath,
          title: topic.title,
          fileName: topic.fileName,
          mimeType: "",
          lastModified: topic.lastModified,
          contentHash: hash,
          indexedAt: new Date().toISOString(),
          extractionQuality: extraction.quality,
          pageCount: extraction.pageCount,
          text: extraction.segments.map((s) => s.text).join("\n\n"),
        },
        stored,
      );

      indexed++;
      chunksCreated += stored.length;
    } catch (err) {
      // Per-file errors are collected, never fatal: one corrupt PDF must not
      // cost the other 41 files.
      errors.push({
        file_name: topic.fileName,
        reason: err instanceof Error ? err.message : String(err),
      });
    }
  }

  await recordModel();
  report({ phase: "Done", done: topics.length, total: topics.length });

  const notes: string[] = [];
  if (!complete) {
    notes.push(
      "Part of the content tree could not be read, so some files were never seen.",
    );
  }

  // Group by cause. Twenty-five files failing for ONE reason is a single
  // environmental problem; listing it twenty-five times buries that. The first
  // real sync failed exactly this way and the shape of it was invisible.
  const byReason = new Map<string, string[]>();
  for (const e of errors) {
    const key = e.reason.slice(0, 120);
    byReason.set(key, [...(byReason.get(key) ?? []), e.file_name]);
  }
  const grouped = [...byReason.entries()]
    .sort((a, b) => b[1].length - a[1].length)
    .map(([reason, files]) => ({
      reason,
      count: files.length,
      examples: files.slice(0, 3),
    }));

  if (errors.length) {
    const top = grouped[0]!;
    notes.push(
      errors.length === top.count && errors.length > 2
        ? `All ${errors.length} failures share one cause: ${top.reason} — that is an ` +
          `environment problem, not a problem with the files.`
        : `${errors.length} file(s) could not be indexed; see failure_summary.`,
    );
  }

  return {
    org_unit_id: orgUnitId,
    course_name: name,
    files_found: topics.length,
    files_indexed: indexed,
    files_skipped_unchanged: unchanged,
    files_skipped_unsupported: unsupported,
    chunks_created: chunksCreated,
    errors,
    duration_seconds: Math.round((performance.now() - started) / 100) / 10,
    note: notes.length ? notes.join(" ") : null,
  };
}
