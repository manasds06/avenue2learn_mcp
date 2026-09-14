/**
 * A citation you can actually follow.
 *
 * `search_course_materials` already returns everything needed — course, file,
 * module path, page/slide, and topic_id. Flattened into prose that becomes
 * "according to the outline", which the student cannot verify. As a chip it
 * expands to the passage, and for a PDF it can render the actual page.
 *
 * That verifiability is the point. An unattributed passage about a late
 * penalty is worse than no answer: if retrieval was wrong, the student acts on
 * a policy from a different course and has no way to notice.
 */

import { useState } from "preact/hooks";

import { callTool } from "../data/tools.js";

export interface CitationData {
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
  citation: CitationData;
}

/** "p.3" / "slide 14" → a page number we can render. */
function pageOf(position: string): number | null {
  const match = position.match(/(?:p\.|slide )(\d+)/i);
  return match ? Number(match[1]) : null;
}

export function CitationChip({ hit }: { hit: SearchHit }) {
  const [open, setOpen] = useState(false);
  const [image, setImage] = useState<string | null>(null);
  const [imageError, setImageError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const { citation } = hit;
  const page = pageOf(citation.position);
  const renderable = page !== null && citation.file_name.toLowerCase().endsWith(".pdf");

  async function showPage(): Promise<void> {
    if (image || loading || page === null) return;
    setLoading(true);
    const outcome = await callTool("get_page_image", {
      org_unit_id: citation.org_unit_id,
      topic_id: citation.topic_id,
      page,
    });
    setLoading(false);

    if (outcome.ok) {
      const result = outcome.result as { _inlineImage?: { mimeType: string; data: string } };
      // The image travels out-of-band precisely so it never costs LLM tokens;
      // here in the UI we just need the bytes.
      if (result._inlineImage) {
        setImage(`data:${result._inlineImage.mimeType};base64,${result._inlineImage.data}`);
      } else {
        setImageError("That page could not be rendered.");
      }
    } else {
      setImageError(outcome.message);
    }
  }

  return (
    <div class={`citation${open ? " citation-open" : ""}`}>
      <button type="button" class="citation-chip" onClick={() => setOpen(!open)}>
        <span class="citation-file">{citation.file_name}</span>
        <span class="citation-pos">{citation.position}</span>
      </button>

      {open ? (
        <div class="citation-body">
          <div class="citation-where">
            {citation.module_path ? `${citation.module_path} · ` : ""}
            {citation.course_name}
          </div>

          <blockquote class="citation-text">{hit.text}</blockquote>

          {renderable && !image ? (
            <button
              type="button"
              class="btn btn-sm btn-quiet"
              onClick={() => void showPage()}
              disabled={loading}
            >
              {loading ? "Rendering…" : `Show ${citation.position}`}
            </button>
          ) : null}

          {image ? <img class="citation-image" src={image} alt={`Page ${page}`} /> : null}
          {imageError ? <p class="citation-error">{imageError}</p> : null}
        </div>
      ) : null}
    </div>
  );
}

export function CitationList({ hits }: { hits: SearchHit[] }) {
  if (!hits.length) return null;
  return (
    <div class="citations">
      {hits.map((hit, i) => (
        <CitationChip key={`${hit.citation.topic_id}-${hit.citation.position}-${i}`} hit={hit} />
      ))}
    </div>
  );
}
