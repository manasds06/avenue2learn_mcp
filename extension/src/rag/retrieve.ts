/**
 * Hybrid retrieval: vector + keyword, fused with Reciprocal Rank Fusion.
 *
 * Neither half is sufficient on its own, and the failures are complementary:
 *
 *   "Assignment 3 requirements" — vectors retrieve Assignment 2 and 4, whose
 *                                 embeddings are nearly identical. Keyword
 *                                 nails it.
 *   "late penalty"             — the document says "submissions received after
 *                                 the deadline". Keyword misses; vectors win.
 *
 * RRF rather than score normalization, because cosine similarity and BM25 live
 * on incomparable scales and any normalization is a fudge factor needing
 * per-corpus tuning. RRF uses only ranks, has one well-understood constant, and
 * is robust without tuning.
 *
 * COURSE SCOPING IS A HARD FILTER, never a ranking boost. Returning a MATH
 * policy for a COMPSCI question is the worst available outcome, and a soft
 * preference does not reliably prevent it.
 */

import { embedOne } from "./embed.js";
import { listDocuments, loadChunks, type StoredChunk, type StoredDocument } from "./store.js";

const RRF_K = 60;
const CANDIDATES = 20;

export interface Citation {
  course_name: string;
  file_name: string;
  module_path: string;
  title: string;
  position: string;
  topic_id: number;
  org_unit_id: number;
  indexed_at: string;
}

export interface SearchHit {
  text: string;
  score: number;
  citation: Citation;
}

// --- keyword half -----------------------------------------------------------

const STOP = new Set(
  ("a an the of for to in on at is are be was were and or not what which how do does " +
    "did this that with from by as it its i my me you your").split(" "),
);

export function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length > 1 && !STOP.has(t));
}

/**
 * BM25 over the chunk corpus.
 *
 * Scores the EMBEDDED text, which includes the context header, so a query
 * naming a course or a week matches the material from it.
 */
function bm25(chunks: StoredChunk[], queryTerms: string[]): Map<number, number> {
  const k1 = 1.5;
  const b = 0.75;

  const docTokens = chunks.map((c) => tokenize(c.embedded));
  const avgLen = docTokens.reduce((n, t) => n + t.length, 0) / (docTokens.length || 1);

  const df = new Map<string, number>();
  for (const tokens of docTokens) {
    for (const term of new Set(tokens)) df.set(term, (df.get(term) ?? 0) + 1);
  }

  const scores = new Map<number, number>();
  for (let i = 0; i < chunks.length; i++) {
    const tokens = docTokens[i]!;
    if (!tokens.length) continue;

    const tf = new Map<string, number>();
    for (const t of tokens) tf.set(t, (tf.get(t) ?? 0) + 1);

    let score = 0;
    for (const term of queryTerms) {
      const f = tf.get(term);
      if (!f) continue;
      const n = df.get(term) ?? 0;
      const idf = Math.log(1 + (chunks.length - n + 0.5) / (n + 0.5));
      score += idf * ((f * (k1 + 1)) / (f + k1 * (1 - b + (b * tokens.length) / avgLen)));
    }
    if (score > 0) scores.set(i, score);
  }
  return scores;
}

// --- vector half ------------------------------------------------------------

/** Vectors are stored unit-normalized, so a dot product is cosine similarity. */
function cosine(a: Float32Array, b: Float32Array): number {
  let dot = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) dot += a[i]! * b[i]!;
  return dot;
}

// --- fusion -----------------------------------------------------------------

function topRanked(scores: Map<number, number>, limit: number): number[] {
  return [...scores.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([index]) => index);
}

export interface SearchOptions {
  orgUnitId?: number;
  topK?: number;
}

export async function search(query: string, opts: SearchOptions = {}): Promise<{
  hits: SearchHit[];
  searchedChunks: number;
  usedVectors: boolean;
}> {
  const topK = opts.topK ?? 8;

  // Hard scope at load time: another course's chunks are never candidates.
  const chunks = await loadChunks(opts.orgUnitId);
  if (!chunks.length) return { hits: [], searchedChunks: 0, usedVectors: false };

  const keywordScores = bm25(chunks, tokenize(query));

  // The vector half is best-effort. If the model cannot load, degraded keyword
  // results beat an error — the user asked a question, not for a status report.
  let vectorScores = new Map<number, number>();
  let usedVectors = false;
  try {
    const queryVector = await embedOne(query);
    for (let i = 0; i < chunks.length; i++) {
      const v = chunks[i]!.vector;
      if (v) vectorScores.set(i, cosine(queryVector, v));
    }
    usedVectors = vectorScores.size > 0;
  } catch {
    vectorScores = new Map();
  }

  const fused = new Map<number, number>();
  const addRanks = (ranked: number[]) => {
    ranked.forEach((index, rank) => {
      fused.set(index, (fused.get(index) ?? 0) + 1 / (RRF_K + rank + 1));
    });
  };
  addRanks(topRanked(keywordScores, CANDIDATES));
  if (usedVectors) addRanks(topRanked(vectorScores, CANDIDATES));

  const docs = new Map<string, StoredDocument>(
    (await listDocuments(opts.orgUnitId)).map((d) => [d.key, d]),
  );

  const hits: SearchHit[] = [];
  for (const index of topRanked(fused, topK)) {
    const chunk = chunks[index]!;
    const doc = docs.get(chunk.docKey);
    if (!doc) continue;
    hits.push({
      text: chunk.text,
      score: Math.round((fused.get(index) ?? 0) * 10000) / 10000,
      citation: {
        course_name: doc.courseName,
        file_name: doc.fileName,
        module_path: doc.modulePath,
        title: doc.title,
        position: chunk.position,
        topic_id: doc.topicId,
        org_unit_id: doc.orgUnitId,
        indexed_at: doc.indexedAt,
      },
    });
  }

  return { hits, searchedChunks: chunks.length, usedVectors };
}
