"""Text extraction with position information.

Positions become citations, so every extractor records where text came from:
page number for PDF, slide number for PPTX, heading path for DOCX/HTML.

Scanned PDFs are a real failure case -- some course outlines are photocopies.
These are detected and reported rather than silently indexed as empty
documents, because a file that appears indexed but never retrieves leaves the
user wondering why searching for their outline finds nothing.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from avenue_mcp.errors import ExtractionError
from avenue_mcp.util.html import to_text

log = logging.getLogger(__name__)

# Below this many characters across a multi-page document, assume the text
# layer is missing (scanned image).
_POOR_TEXT_THRESHOLD = 100

SUPPORTED_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".txt", ".md", ".markdown",
    ".html", ".htm", ".csv", ".log", ".rst",
}


@dataclass
class Segment:
    """A positioned run of text."""

    text: str
    position: str          # "p.3" | "slide 14" | "§ Grading" | "line 1"
    heading: str | None = None
    page: int | None = None


@dataclass
class Extraction:
    segments: list[Segment] = field(default_factory=list)
    page_count: int | None = None
    quality: str = "ok"     # ok | poor | empty
    note: str | None = None

    @property
    def full_text(self) -> str:
        return "\n\n".join(s.text for s in self.segments if s.text.strip())

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)


def is_supported(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(131072), b""):
            h.update(block)
    return h.hexdigest()


def extract(path: Path | str, mime_type: str | None = None) -> Extraction:
    """Dispatch on suffix, falling back to MIME type."""
    p = Path(path)
    if not p.is_file():
        raise ExtractionError(f"No such file: {p}")

    suffix = p.suffix.lower()
    if not suffix and mime_type:
        suffix = {
            "application/pdf": ".pdf",
            "text/plain": ".txt",
            "text/html": ".html",
        }.get(mime_type.split(";")[0].strip(), "")

    try:
        if suffix == ".pdf":
            result = _pdf(p)
        elif suffix == ".docx":
            result = _docx(p)
        elif suffix == ".pptx":
            result = _pptx(p)
        elif suffix in (".html", ".htm"):
            result = _html(p)
        elif suffix in (".txt", ".md", ".markdown", ".csv", ".log", ".rst"):
            result = _plain(p)
        else:
            raise ExtractionError(f"Unsupported file type: {suffix or 'unknown'}")
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 -- one bad file must not kill a sync
        raise ExtractionError(f"Could not read {p.name}: {exc}") from exc

    return _assess(result)


def _assess(result: Extraction) -> Extraction:
    chars = result.char_count
    if chars == 0:
        result.quality = "empty"
        result.note = "No text could be extracted."
    elif chars < _POOR_TEXT_THRESHOLD and (result.page_count or 1) > 1:
        result.quality = "poor"
        result.note = (
            "Almost no text extracted from a multi-page document -- this is "
            "likely a scanned image. OCR is not supported."
        )
    return result


# --- PDF ------------------------------------------------------------------


def _pdf(path: Path) -> Extraction:
    try:
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise ExtractionError("pymupdf is not installed") from exc

    segments: list[Segment] = []
    with pymupdf.open(path) as doc:
        pages = doc.page_count
        for i, page in enumerate(doc, start=1):
            text = (page.get_text("text") or "").strip()
            if text:
                segments.append(Segment(text=text, position=f"p.{i}", page=i))
    return Extraction(segments=segments, page_count=pages)


# --- DOCX -----------------------------------------------------------------


def _docx(path: Path) -> Extraction:
    try:
        import docx  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("python-docx is not installed") from exc

    document = docx.Document(str(path))
    segments: list[Segment] = []
    heading = None
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            body = "\n".join(buffer).strip()
            if body:
                segments.append(
                    Segment(
                        text=body,
                        position=f"§ {heading}" if heading else "§ body",
                        heading=heading,
                    )
                )
            buffer.clear()

    for para in document.paragraphs:
        text = (para.text or "").strip()
        style = (para.style.name or "").lower() if para.style is not None else ""
        if style.startswith("heading") and text:
            flush()
            heading = text
            buffer.append(text)
        elif text:
            buffer.append(text)
    flush()

    for ti, table in enumerate(document.tables, start=1):
        rows = []
        for row in table.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            segments.append(
                Segment(text="\n".join(rows), position=f"table {ti}", heading=heading)
            )

    return Extraction(segments=segments)


# --- PPTX -----------------------------------------------------------------


def _pptx(path: Path) -> Extraction:
    try:
        from pptx import Presentation  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ExtractionError("python-pptx is not installed") from exc

    prs = Presentation(str(path))
    segments: list[Segment] = []
    slides = 0

    for idx, slide in enumerate(prs.slides, start=1):
        slides = idx
        parts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                t = (shape.text_frame.text or "").strip()
                if t:
                    parts.append(t)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [(c.text or "").strip() for c in row.cells]
                    if any(cells):
                        parts.append(" | ".join(cells))

        # Speaker notes often carry the actual explanation -- the slide says
        # "Amortized Analysis" and the notes explain it.
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
                if notes:
                    parts.append(f"[Speaker notes] {notes}")
        except Exception:  # noqa: BLE001
            pass

        if parts:
            segments.append(
                Segment(text="\n".join(parts), position=f"slide {idx}", page=idx)
            )

    return Extraction(segments=segments, page_count=slides)


# --- HTML / plain ---------------------------------------------------------


def _html(path: Path) -> Extraction:
    raw = path.read_text("utf-8", errors="replace")
    text = to_text(raw)
    segments = (
        [Segment(text=text, position="§ document")] if text.strip() else []
    )
    return Extraction(segments=segments)


def _plain(path: Path) -> Extraction:
    text = path.read_text("utf-8", errors="replace").strip()
    if not text:
        return Extraction(segments=[])
    # Split on blank lines so long text files still chunk sensibly.
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    segments = [
        Segment(text=b, position=f"block {i}") for i, b in enumerate(blocks, start=1)
    ]
    return Extraction(segments=segments)
