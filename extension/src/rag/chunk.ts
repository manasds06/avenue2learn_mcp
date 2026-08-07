/**
 * Chunking — structure-aware, not fixed-size.
 *
 * The single highest-leverage quality decision in the pipeline. A naive
 * sliding window splits a course outline's late policy across two chunks and
 * neither retrieves cleanly for "what's the late penalty?".
 *
 * EVERY CHUNK CARRIES A CONTEXT HEADER before embedding:
 *
 *     [SFWRENG 2DA4 — Week 1 / Course Outline — 2DA4_outline.pdf, p.3]
 *     Late assignments are penalized 10% per day …
 *
 * This matters more than it looks. Without it a chunk reading "10% per day" is
 * semantically identical whether it came from 2DA4 or MATH 2Z03, and
 * cross-course contamination is the most damaging retrieval error available:
 * it produces an answer that is confidently wrong rather than obviously empty.
 * The header is embedded but stripped from the text shown to the user.
 */

import type { Segment } from "./extract.js";

export const TARGET_TOKENS = 600;
export const MAX_TOKENS = 1000;
export const MIN_TOKENS = 50;
export const OVERLAP_TOKENS = 80;

/** Rough estimate. Good enough for sizing; we are not billing on it. */
const CHARS_PER_TOKEN = 4;

export const estTokens = (text: string): number =>
  Math.max(1, Math.floor(text.length / CHARS_PER_TOKEN));

const maxChars = (tokens: number): number => tokens * CHARS_PER_TOKEN;

export interface Chunk {
  /** Shown to the user. No header. */
  text: string;
  /** Sent to the embedder. Header included. */
  embedded: string;
  position: string;
  tokenCount: number;
}

export interface ChunkContext {
  courseName: string;
  modulePath: string;
  title: string;
  fileName: string;
}

function header(ctx: ChunkContext, position: string): string {
  const where = [ctx.courseName, ctx.modulePath, ctx.title].filter(Boolean).join(" — ");
  return `[${where} — ${ctx.fileName}, ${position}]`;
}

/**
 * Group segments into chunks, splitting on structure first.
 *
 * 1. Segments sharing a position (a page, a slide, a heading section) stay
 *    together while they fit.
 * 2. A section over the max is split on sentence boundaries with overlap.
 * 3. Orphan fragments under MIN_TOKENS merge into the previous chunk —
 *    a stray line is retrieval noise, not a passage.
 */
export function chunkSegments(segments: Segment[], ctx: ChunkContext): Chunk[] {
  const chunks: Chunk[] = [];

  let buffer: string[] = [];
  let bufferPos: string | null = null;

  const flush = () => {
    if (!buffer.length || bufferPos === null) return;
    const text = buffer.join("\n").trim();
    buffer = [];
    if (!text) return;

    for (const piece of splitToSize(text, MAX_TOKENS)) {
      chunks.push(make(piece, bufferPos!, ctx));
    }
  };

  for (const seg of segments) {
    const text = seg.text.trim();
    if (!text) continue;

    // A new position is a structural boundary — flush rather than run on.
    if (bufferPos !== null && seg.position !== bufferPos) flush();
    bufferPos = seg.position;

    const prospective = [...buffer, text].join("\n");
    if (estTokens(prospective) > TARGET_TOKENS && buffer.length) {
      flush();
      bufferPos = seg.position;
    }
    buffer.push(seg.kind === "notes" ? `[speaker notes] ${text}` : text);
  }
  flush();

  return mergeOrphans(chunks, ctx);
}

function make(text: string, position: string, ctx: ChunkContext): Chunk {
  const embedded = `${header(ctx, position)}\n\n${text}`;
  return { text, embedded, position, tokenCount: estTokens(text) };
}

/** Sentence-boundary split with overlap, used only when a section is too big. */
function splitToSize(text: string, limit: number): string[] {
  if (estTokens(text) <= limit) return [text];

  const cap = maxChars(limit);
  const overlap = maxChars(OVERLAP_TOKENS);
  const sentences = text.split(/(?<=[.!?])\s+/);
  const out: string[] = [];
  let current = "";

  for (const sentence of sentences) {
    if (current && (current + " " + sentence).length > cap) {
      out.push(current.trim());
      // Carry a tail forward so an answer straddling the boundary survives.
      current = current.slice(-overlap) + " " + sentence;
    } else {
      current = current ? `${current} ${sentence}` : sentence;
    }
  }
  if (current.trim()) out.push(current.trim());

  // A single sentence longer than the cap still has to be broken somewhere.
  return out.flatMap((piece) =>
    piece.length <= cap * 1.5 ? [piece] : hardSplit(piece, cap, overlap),
  );
}

function hardSplit(text: string, cap: number, overlap: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < text.length; i += cap - overlap) {
    out.push(text.slice(i, i + cap));
  }
  return out;
}

function mergeOrphans(chunks: Chunk[], ctx: ChunkContext): Chunk[] {
  const out: Chunk[] = [];
  for (const chunk of chunks) {
    const prev = out[out.length - 1];
    if (
      prev &&
      chunk.tokenCount < MIN_TOKENS &&
      prev.tokenCount + chunk.tokenCount <= MAX_TOKENS
    ) {
      const text = `${prev.text}\n${chunk.text}`;
      out[out.length - 1] = make(text, prev.position, ctx);
    } else {
      out.push(chunk);
    }
  }
  return out;
}

/**
 * Discussions chunk differently: the unit is a question-and-reply pair, not a
 * post. An instructor's "Either is fine, but document your choice" is
 * meaningless alone; paired with the question it answers, it is exactly the
 * passage a student needs.
 */
export function chunkThread(
  posts: Array<{ post_id: number | null; parent_post_id: number | null; author_role: string; body_text: string }>,
  ctx: ChunkContext,
): Chunk[] {
  const byId = new Map(posts.filter((p) => p.post_id !== null).map((p) => [p.post_id!, p]));
  const chunks: Chunk[] = [];

  for (const post of posts) {
    const parent = post.parent_post_id !== null ? byId.get(post.parent_post_id) : undefined;
    const text = parent
      ? `Q (${parent.author_role}): ${parent.body_text}\nA (${post.author_role}): ${post.body_text}`
      : `${post.author_role}: ${post.body_text}`;

    for (const piece of splitToSize(text.trim(), MAX_TOKENS)) {
      chunks.push(make(piece, `post ${post.post_id ?? "?"}`, ctx));
    }
  }
  return chunks;
}
