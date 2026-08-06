"""Page rendering -- the visual escape hatch.

Text extraction has a hard ceiling: it gets the words and loses the picture. A
slide reading "Red-Black Tree Rotations" with a diagram beneath it extracts to
five words and no information, while the student's actual question is about the
diagram.

One page per call, deliberately. A tool that could render thirty slides at once
would get used that way, and thirty images is an enormous amount of context for
a question that needed one.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from avenue_mcp.errors import RenderError

log = logging.getLogger(__name__)

RENDERABLE = {".pdf", ".pptx", ".ppt"}


def _slug(name: str) -> str:
    """A filesystem-safe stem for a cache filename."""
    keep = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    return keep.strip("-")[:60] or "render"


def is_renderable(path: Path | str) -> bool:
    return Path(path).suffix.lower() in RENDERABLE


def page_count(path: Path | str) -> int:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _pdf_page_count(p)
    if suffix in (".pptx", ".ppt"):
        try:
            from pptx import Presentation  # type: ignore

            return len(Presentation(str(p)).slides)
        except Exception as exc:  # noqa: BLE001
            raise RenderError(f"Could not read {p.name}: {exc}") from exc
    raise RenderError(f"Cannot render {suffix or 'unknown'} files.")


def render_page(
    path: Path | str,
    page: int,
    *,
    dpi: int = 120,
    cache_dir: Path | None = None,
) -> tuple[bytes, dict[str, object]]:
    """Render one 1-indexed page/slide to PNG bytes.

    Returns (png_bytes, metadata). Caches renders on disk keyed by page and dpi.
    """
    p = Path(path)
    if not p.is_file():
        raise RenderError(f"File not cached locally: {p.name}")
    if page < 1:
        raise RenderError("Page numbers are 1-indexed.")

    suffix = p.suffix.lower()
    if suffix not in RENDERABLE:
        raise RenderError(
            f"Cannot render {suffix or 'unknown'} files. Only PDF and PPTX are supported."
        )

    cached = None
    if cache_dir is not None:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        # A stable digest, not builtin hash(): string hashing is
        # PYTHONHASHSEED-randomized per process, so the on-disk cache never hit
        # after a restart and the render directory grew without bound.
        key = hashlib.sha256(
            f"{p.resolve()}|{p.stat().st_mtime_ns}|{page}|{dpi}".encode()
        ).hexdigest()[:16]
        stem = f"{_slug(p.stem)}-{key}.png"
        cached = cache_dir / stem
        if cached.is_file():
            return cached.read_bytes(), _meta(p, page, dpi, cached, from_cache=True)

    pdf_path = p if suffix == ".pdf" else _pptx_to_pdf(p)
    try:
        png, width, height = _render_pdf_page(pdf_path, page, dpi)
        total = _pdf_page_count(pdf_path)
    finally:
        if pdf_path != p:
            shutil.rmtree(pdf_path.parent, ignore_errors=True)

    if cached is not None:
        try:
            cached.write_bytes(png)
        except OSError:
            pass

    meta = _meta(p, page, dpi, cached, from_cache=False)
    meta.update({"total_pages": total, "rendered_width": width, "rendered_height": height})
    return png, meta


def _meta(
    p: Path, page: int, dpi: int, cached: Path | None, *, from_cache: bool
) -> dict[str, object]:
    return {
        "file_name": p.name,
        "page": page,
        "dpi": dpi,
        "cached": from_cache,
        "cache_path": str(cached) if cached else None,
    }


def _pdf_page_count(path: Path) -> int:
    pymupdf = _pymupdf()
    with pymupdf.open(path) as doc:
        return int(doc.page_count)


def _render_pdf_page(path: Path, page: int, dpi: int) -> tuple[bytes, int, int]:
    pymupdf = _pymupdf()
    with pymupdf.open(path) as doc:
        if page > doc.page_count:
            raise RenderError(
                f"Page {page} is out of range -- the document has {doc.page_count}."
            )
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        return pix.tobytes("png"), int(pix.width), int(pix.height)


def _pymupdf():  # noqa: ANN202
    try:
        import pymupdf  # type: ignore

        return pymupdf
    except ImportError:  # pragma: no cover
        try:
            import fitz  # type: ignore

            return fitz
        except ImportError as exc:
            raise RenderError("pymupdf is not installed") from exc


def _pptx_to_pdf(path: Path) -> Path:
    """Convert via headless LibreOffice.

    Degrades to a clear message rather than an obscure failure -- PDF rendering,
    the common case, needs no extra system dependency.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RenderError(
            "Rendering PowerPoint slides needs LibreOffice. Install it "
            "(`sudo apt install libreoffice-impress`), or ask for the slide's "
            "text with read_content_file instead."
        )
    tmp = Path(tempfile.mkdtemp(prefix="avenue-render-"))
    try:
        subprocess.run(  # noqa: S603 -- fixed binary, no shell
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(path)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RenderError("LibreOffice timed out converting the slides.") from exc
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        detail = (exc.stderr or b"").decode("utf-8", "replace")[:200]
        raise RenderError(f"LibreOffice failed to convert the slides. {detail}") from exc

    pdfs = list(tmp.glob("*.pdf"))
    if not pdfs:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RenderError("LibreOffice produced no PDF output.")
    return pdfs[0]
