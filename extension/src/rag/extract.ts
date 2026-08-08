/**
 * Text extraction, with position information — because positions become
 * citations, and an unattributed passage about a late penalty is worse than no
 * answer: the student cannot check it, and if retrieval was wrong they act on
 * a policy from a different course.
 *
 * | Format   | Library            | Position unit |
 * |----------|--------------------|---------------|
 * | PDF      | pdf.js             | page number   |
 * | PPTX     | JSZip + DOMParser  | slide number  |
 * | DOCX     | JSZip + DOMParser  | paragraph run |
 * | HTML/TXT | DOMParser / raw    | section       |
 *
 * SCANNED PDFs ARE A REAL FAILURE CASE. Some course outlines are photocopies,
 * and pdf.js returns near-empty text for them. Detected and reported rather
 * than silently indexed: a document that appears indexed but never retrieves
 * leaves the user wondering why searching for their outline finds nothing.
 * OCR is out of scope.
 */

import JSZip from "jszip";

export interface Segment {
  /** "p.3" / "slide 14" / "§ Grading" */
  position: string;
  text: string;
  /** Slide notes and headings are worth keeping distinct while chunking. */
  kind?: "body" | "notes" | "heading";
}

export interface Extraction {
  segments: Segment[];
  pageCount: number | null;
  quality: "ok" | "poor";
  /** Populated when quality is "poor", so the sync can report a real reason. */
  reason?: string;
}

export class ExtractionError extends Error {}

const SUPPORTED = [".pdf", ".pptx", ".docx", ".html", ".htm", ".txt", ".md", ".csv"];

export function isSupported(fileName: string): boolean {
  const lower = fileName.toLowerCase();
  return SUPPORTED.some((ext) => lower.endsWith(ext));
}

export function isRenderable(fileName: string): boolean {
  return fileName.toLowerCase().endsWith(".pdf");
}

export async function extract(bytes: ArrayBuffer, fileName: string): Promise<Extraction> {
  const lower = fileName.toLowerCase();

  if (lower.endsWith(".pdf")) return extractPdf(bytes);
  if (lower.endsWith(".pptx")) return extractPptx(bytes);
  if (lower.endsWith(".docx")) return extractDocx(bytes);
  if (lower.endsWith(".html") || lower.endsWith(".htm")) return extractHtml(bytes);
  if (lower.endsWith(".txt") || lower.endsWith(".md") || lower.endsWith(".csv")) {
    return extractText(bytes);
  }

  throw new ExtractionError(
    `Cannot extract text from ${fileName}. Supported: PDF, PPTX, DOCX, HTML, TXT, MD, CSV.`,
  );
}

// --- PDF --------------------------------------------------------------------

let pdfjsPromise: Promise<typeof import("pdfjs-dist")> | null = null;

async function getPdfjs() {
  if (!pdfjsPromise) {
    // The LEGACY build: it avoids the newest syntax and is the one that
    // survives an extension's CSP without eval.
    pdfjsPromise = import("pdfjs-dist/legacy/build/pdf.mjs").then((lib) => {
      // Bundled, not fetched — MV3 will not load a worker from a CDN. The file
      // is deliberately named .js: Chrome serves extension files by extension
      // and rejects a module worker fetched from a .mjs URL on MIME grounds,
      // which silently broke EVERY PDF while DOCX kept working.
      lib.GlobalWorkerOptions.workerSrc = chrome.runtime.getURL("vendor/pdf.worker.js");
      return lib as unknown as typeof import("pdfjs-dist");
    });
  }
  return pdfjsPromise;
}

/**
 * Open a document, falling back to main-thread parsing if the worker will not
 * start.
 *
 * Slower, but a whole course of PDFs failing is a much worse outcome than a
 * sync that takes longer. `isEvalSupported: false` is required either way:
 * an extension's CSP forbids eval, and pdf.js otherwise reaches for it.
 */
async function openPdf(bytes: ArrayBuffer) {
  const pdfjs = await getPdfjs();
  const common = { isEvalSupported: false, useWorkerFetch: false, useSystemFonts: false };

  // The LOADING TASK owns destroy(), not the document proxy. Calling
  // doc.destroy() throws "t.destroy is not a function" after minification —
  // which failed every single PDF in a course while DOCX kept working.
  // TypeScript flagged exactly this and an earlier version cast the error
  // away; the cast was the bug.
  let task = pdfjs.getDocument({ data: new Uint8Array(bytes.slice(0)), ...common });
  try {
    return { doc: await task.promise, task };
  } catch (err) {
    console.warn("pdf.js worker unavailable, retrying on the main thread", err);
    await task.destroy().catch(() => {});
    task = pdfjs.getDocument({
      data: new Uint8Array(bytes.slice(0)),
      ...common,
      disableWorker: true,
    } as Parameters<typeof pdfjs.getDocument>[0]);
    return { doc: await task.promise, task };
  }
}

async function extractPdf(bytes: ArrayBuffer): Promise<Extraction> {
  // pdf.js takes ownership of the buffer, so it gets a copy — the caller still
  // needs the original to hash.
  const { doc, task } = await openPdf(bytes);

  const segments: Segment[] = [];
  try {
    for (let page = 1; page <= doc.numPages; page++) {
      const p = await doc.getPage(page);
      const content = await p.getTextContent();
      const text = content.items
        .map((item) => ("str" in item ? item.str : ""))
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
      if (text) segments.push({ position: `p.${page}`, text });
      p.cleanup();
    }
  } finally {
    await task.destroy().catch(() => {});
  }

  const total = segments.reduce((n, s) => n + s.text.length, 0);
  // A multi-page document yielding almost nothing is a scan.
  if (doc.numPages > 1 && total < 100) {
    return {
      segments,
      pageCount: doc.numPages,
      quality: "poor",
      reason:
        `Only ${total} characters of text across ${doc.numPages} pages — this looks ` +
        `like scanned images. OCR is not supported, so this file is not searchable.`,
    };
  }

  return { segments, pageCount: doc.numPages, quality: "ok" };
}

/**
 * Render one page so a diagram can actually be looked at.
 *
 * Returns raw base64 rather than a data URL, because the caller sends it to
 * the model as an inlineData part. Dimensions are capped: an uncapped A4 page
 * at scale 3 is a multi-megabyte PNG, and every megabyte is roughly 350k
 * tokens of quota.
 */
const MAX_RENDER_PX = 1400;

export async function renderPdfPage(
  bytes: ArrayBuffer,
  pageNumber: number,
  scale = 1.5,
): Promise<{
  base64: string;
  mimeType: string;
  width: number;
  height: number;
  pageCount: number;
}> {

  const { doc, task } = await openPdf(bytes);

  try {
    if (pageNumber < 1 || pageNumber > doc.numPages) {
      throw new ExtractionError(
        `Page ${pageNumber} is out of range; this document has ${doc.numPages}.`,
      );
    }
    const page = await doc.getPage(pageNumber);

    // Cap the long edge before rendering rather than downscaling after: a
    // 3000px canvas costs the memory whether or not we keep the pixels.
    const base = page.getViewport({ scale: 1 });
    const longEdge = Math.max(base.width, base.height);
    const capped = Math.min(scale, MAX_RENDER_PX / longEdge);
    const viewport = page.getViewport({ scale: Math.max(0.5, capped) });

    const canvas = new OffscreenCanvas(viewport.width, viewport.height);
    const context = canvas.getContext("2d") as unknown as CanvasRenderingContext2D;

    // White background: PDF pages are transparent, and JPEG has no alpha, so
    // without this the text renders on black.
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, viewport.width, viewport.height);

    await page.render({
      canvas: canvas as unknown as HTMLCanvasElement,
      canvasContext: context,
      viewport,
    }).promise;

    // JPEG, not PNG. A text-heavy page is 4-8x smaller as JPEG, and the model
    // is reading a diagram, not inspecting pixels.
    const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.82 });
    return {
      base64: await blobToBase64(blob),
      mimeType: "image/jpeg",
      width: viewport.width,
      height: viewport.height,
      pageCount: doc.numPages,
    };
  } finally {
    await task.destroy().catch(() => {});
  }
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result);
      resolve(result.slice(result.indexOf(",") + 1));
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

// --- OOXML (PPTX / DOCX) ----------------------------------------------------

/**
 * Created on demand, never at module scope. A service worker has no DOM, so a
 * top-level `new DOMParser()` makes the whole module unimportable there — and
 * the worker imports the tool registry.
 */
let _parser: DOMParser | null = null;
function getParser(): DOMParser {
  if (!_parser) _parser = new DOMParser();
  return _parser;
}

/** Pull every <a:t> / <w:t> text node in document order. */
function ooxmlText(xml: string, tag: string): string {
  const doc = getParser().parseFromString(xml, "application/xml");
  const nodes = doc.getElementsByTagName(tag);
  const parts: string[] = [];
  for (let i = 0; i < nodes.length; i++) {
    const t = nodes[i]?.textContent ?? "";
    if (t) parts.push(t);
  }
  return parts.join(" ").replace(/\s+/g, " ").trim();
}

async function extractPptx(bytes: ArrayBuffer): Promise<Extraction> {
  const zip = await JSZip.loadAsync(bytes);
  const segments: Segment[] = [];

  const slideNames = Object.keys(zip.files)
    .filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n))
    .sort((a, b) => slideNumber(a) - slideNumber(b));

  for (const name of slideNames) {
    const n = slideNumber(name);
    const body = ooxmlText(await zip.files[name]!.async("string"), "a:t");
    if (body) segments.push({ position: `slide ${n}`, text: body });

    // Speaker notes carry the actual explanation surprisingly often — the
    // slide says "Amortized Analysis" and the notes explain it.
    const notesFile = zip.files[`ppt/notesSlides/notesSlide${n}.xml`];
    if (notesFile) {
      const notes = ooxmlText(await notesFile.async("string"), "a:t");
      // Notes slides repeat the slide number as a text run; drop that noise.
      if (notes && notes !== String(n)) {
        segments.push({ position: `slide ${n}`, text: notes, kind: "notes" });
      }
    }
  }

  const total = segments.reduce((acc, s) => acc + s.text.length, 0);
  if (slideNames.length > 1 && total < 100) {
    return {
      segments,
      pageCount: slideNames.length,
      quality: "poor",
      reason: `Almost no text across ${slideNames.length} slides — likely an image-only deck.`,
    };
  }

  return { segments, pageCount: slideNames.length, quality: "ok" };
}

const slideNumber = (name: string): number => Number(name.match(/(\d+)\.xml$/)?.[1] ?? 0);

async function extractDocx(bytes: ArrayBuffer): Promise<Extraction> {
  const zip = await JSZip.loadAsync(bytes);
  const file = zip.files["word/document.xml"];
  if (!file) throw new ExtractionError("This DOCX has no word/document.xml.");

  const doc = getParser().parseFromString(await file.async("string"), "application/xml");
  const paragraphs = doc.getElementsByTagName("w:p");
  const segments: Segment[] = [];
  let heading = "start";

  for (let i = 0; i < paragraphs.length; i++) {
    const p = paragraphs[i]!;
    const runs = p.getElementsByTagName("w:t");
    const parts: string[] = [];
    for (let j = 0; j < runs.length; j++) parts.push(runs[j]?.textContent ?? "");
    const text = parts.join("").replace(/\s+/g, " ").trim();
    if (!text) continue;

    // Heading styles are what make structure-aware chunking possible in a
    // DOCX: an outline's "Late Policy" section should stay in one chunk.
    const style = p.getElementsByTagName("w:pStyle")[0]?.getAttribute("w:val") ?? "";
    if (/^heading/i.test(style)) {
      heading = text;
      segments.push({ position: `§ ${text}`, text, kind: "heading" });
    } else {
      segments.push({ position: `§ ${heading}`, text });
    }
  }

  if (!segments.length) {
    return { segments, pageCount: null, quality: "poor", reason: "No text found in this DOCX." };
  }
  return { segments, pageCount: null, quality: "ok" };
}

// --- HTML / text ------------------------------------------------------------

function decode(bytes: ArrayBuffer): string {
  return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
}

async function extractHtml(bytes: ArrayBuffer): Promise<Extraction> {
  const doc = getParser().parseFromString(decode(bytes), "text/html");
  for (const el of [...doc.querySelectorAll("script, style, nav, noscript")]) el.remove();

  const segments: Segment[] = [];
  let heading = "start";

  for (const el of [...doc.body.querySelectorAll("h1, h2, h3, h4, p, li, td, pre")]) {
    const text = (el.textContent ?? "").replace(/\s+/g, " ").trim();
    if (!text) continue;
    if (/^h[1-4]$/i.test(el.tagName)) {
      heading = text;
      segments.push({ position: `§ ${text}`, text, kind: "heading" });
    } else {
      segments.push({ position: `§ ${heading}`, text });
    }
  }

  if (!segments.length) {
    return { segments, pageCount: null, quality: "poor", reason: "No readable text in this page." };
  }
  return { segments, pageCount: null, quality: "ok" };
}

async function extractText(bytes: ArrayBuffer): Promise<Extraction> {
  const text = decode(bytes);
  const segments: Segment[] = text
    .split(/\n{2,}/)
    .map((block, i) => ({ position: `¶ ${i + 1}`, text: block.replace(/\s+/g, " ").trim() }))
    .filter((s) => s.text);

  if (!segments.length) {
    return { segments, pageCount: null, quality: "poor", reason: "File is empty." };
  }
  return { segments, pageCount: null, quality: "ok" };
}
