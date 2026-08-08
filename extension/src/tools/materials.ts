/**
 * The RAG tools: sync, search, read one file, render one page.
 *
 * These are the reason the extension can answer "what's the assignment weight"
 * when the gradebook does not carry it — the answer is on a slide, and nothing
 * in the live API returns it.
 */

import { AvenueError } from "../avenue/errors.js";
import { extract, isRenderable, renderPdfPage } from "../rag/extract.js";
import { indexedModel } from "../rag/embed.js";
import { search } from "../rag/retrieve.js";
import { downloadTopic, syncCourse, type SyncProgress } from "../rag/sync.js";
import {
  countChunks,
  getDocument,
  listDocuments,
  type StoredDocument,
} from "../rag/store.js";
import { courseName } from "./context.js";

/** Set by the panel so a long sync can report progress. */
let progressSink: ((p: SyncProgress) => void) | null = null;
export function setSyncProgressSink(fn: ((p: SyncProgress) => void) | null): void {
  progressSink = fn;
}

export async function syncCourseMaterials(args: {
  org_unit_id: number;
  force?: boolean;
}) {
  return syncCourse(args.org_unit_id, {
    force: args.force ?? false,
    onProgress: (p) => progressSink?.(p),
  });
}

export async function searchCourseMaterials(args: {
  query: string;
  org_unit_id?: number;
  top_k?: number;
}) {
  const { hits, searchedChunks, usedVectors } = await search(args.query, {
    orgUnitId: args.org_unit_id,
    topK: args.top_k ?? 8,
  });

  // Returned on EVERY call, empty or not. An empty result is otherwise
  // ambiguous in a way that matters: "the materials don't say" and "you never
  // indexed that course" are completely different answers, and confusing them
  // produces a confident "your outline doesn't mention late penalties" when
  // the truth is nothing was ever synced.
  const docs = await listDocuments();
  const indexed = [...new Set(docs.map((d) => d.courseName))].sort();

  return {
    query: args.query,
    results: hits,
    count: hits.length,
    indexed_courses: indexed,
    searched_chunks: searchedChunks,
    // Keyword-only still works if the model could not load; say so rather than
    // quietly returning weaker results.
    semantic_search_used: usedVectors,
    note: indexed.length
      ? usedVectors
        ? null
        : "Semantic search was unavailable, so these are keyword matches only. Paraphrased questions may miss."
      : "Nothing has been indexed yet. Run sync_course_materials for a course first — there is nothing to search.",
  };
}

export async function readContentFile(args: {
  org_unit_id: number;
  topic_id: number;
  max_chars?: number;
  start_page?: number;
}) {
  const maxChars = args.max_chars ?? 12_000;
  const startPage = args.start_page ?? 1;

  // Prefer the indexed copy: it is already extracted, and re-downloading a
  // 40MB deck to answer a follow-up is wasteful.
  let doc: StoredDocument | undefined = await getDocument(args.org_unit_id, args.topic_id);
  let text: string;
  let fileName: string;
  let pageCount: number | null;

  if (doc) {
    text = doc.text;
    fileName = doc.fileName;
    pageCount = doc.pageCount;
  } else {
    const bytes = await downloadTopic(args.org_unit_id, args.topic_id);
    fileName = `topic-${args.topic_id}`;
    const tree = await listDocuments(args.org_unit_id);
    fileName = tree.find((d) => d.topicId === args.topic_id)?.fileName ?? fileName;

    const extraction = await extract(bytes, fileName);
    if (extraction.quality === "poor") {
      return {
        org_unit_id: args.org_unit_id,
        topic_id: args.topic_id,
        file_name: fileName,
        text: "",
        truncated: false,
        extraction_quality: "poor",
        note:
          extraction.reason ??
          "No usable text could be extracted. Try get_page_image to look at it instead.",
      };
    }
    text = extraction.segments.map((s) => `[${s.position}] ${s.text}`).join("\n\n");
    pageCount = extraction.pageCount;
  }

  // Paginate by character offset derived from the requested page, so a long
  // deck can be walked rather than truncated silently.
  const marker = new RegExp(`\\[(p\\.|slide )${startPage}\\]`);
  const offset = startPage > 1 ? Math.max(0, text.search(marker)) : 0;
  const slice = text.slice(offset, offset + maxChars);
  const truncated = offset + maxChars < text.length;

  return {
    org_unit_id: args.org_unit_id,
    topic_id: args.topic_id,
    file_name: fileName,
    course_name: await courseName(args.org_unit_id),
    page_count: pageCount,
    start_page: startPage,
    text: slice,
    truncated,
    // Explicit rather than silent: a model that does not know it got a partial
    // document will answer confidently from the part it received.
    next_start_page: truncated ? startPage + 1 : null,
    extraction_quality: doc?.extractionQuality ?? "ok",
    note: truncated
      ? "Truncated. Use next_start_page to continue, or search_course_materials if you are hunting for something specific."
      : null,
  };
}

export async function getPageImage(args: {
  org_unit_id: number;
  topic_id: number;
  page: number;
  dpi?: number;
}) {
  const docs = await listDocuments(args.org_unit_id);
  const fileName = docs.find((d) => d.topicId === args.topic_id)?.fileName ?? "";

  if (fileName && !isRenderable(fileName)) {
    throw new AvenueError(
      "InvalidRequest",
      `${fileName} cannot be rendered as an image — only PDFs can.`,
      "Use read_content_file for its text instead.",
    );
  }

  const bytes = await downloadTopic(args.org_unit_id, args.topic_id);
  const scale = Math.min(3, Math.max(1, (args.dpi ?? 110) / 72));
  const { base64, mimeType, width, height, pageCount } = await renderPdfPage(
    bytes,
    args.page,
    scale,
  );

  return {
    org_unit_id: args.org_unit_id,
    topic_id: args.topic_id,
    file_name: fileName || null,
    page: args.page,
    page_count: pageCount,
    width,
    height,
    note: "Rendered page image, attached separately. Requires a vision-capable model.",
    // NOT part of the JSON the model reads. The loop lifts this out and sends
    // it as an inlineData part.
    //
    // Putting a base64 PNG in the tool RESULT costs 100k-500k tokens for one
    // page, and the conversation resends it on every later turn — enough to
    // exhaust a free API key in a single question. It cost one, in testing.
    _inlineImage: { mimeType, data: base64 },
  };
}

/** Index state, folded into get_status. */
export async function indexStatus() {
  const docs = await listDocuments();
  const byCourse = new Map<number, { course_name: string; files: number }>();
  for (const d of docs) {
    const row = byCourse.get(d.orgUnitId) ?? { course_name: d.courseName, files: 0 };
    row.files++;
    byCourse.set(d.orgUnitId, row);
  }

  return {
    documents: docs.length,
    chunks: await countChunks(),
    embed_model: await indexedModel(),
    courses_indexed: [...byCourse.entries()].map(([org_unit_id, row]) => ({
      org_unit_id,
      ...row,
    })),
  };
}
