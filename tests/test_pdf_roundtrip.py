"""End-to-end PDF extraction + rendering, using a PDF we generate ourselves.

Covers the two capabilities that justify the render path: text extraction with
page positions (for citations), and turning a page into an image (for diagrams
text extraction cannot represent).
"""

from __future__ import annotations

import pytest

pymupdf = pytest.importorskip("pymupdf")

from avenue_mcp.errors import RenderError  # noqa: E402
from avenue_mcp.rag.extract import extract  # noqa: E402
from avenue_mcp.rag.render import is_renderable, page_count, render_page  # noqa: E402


@pytest.fixture()
def outline_pdf(tmp_path):
    """A three-page stand-in for a course outline."""
    doc = pymupdf.open()
    pages = [
        "COMPSCI 2C03 Course Outline\nWinter 2026\nInstructor: Dr. Example",
        "Grading\nAssignments 40%\nMidterm 25%\nFinal 35%",
        "Late Policy\nLate assignments are penalized 10% per day, "
        "to a maximum of three days.",
    ]
    for body in pages:
        page = doc.new_page()
        page.insert_text((72, 100), body, fontsize=12)
    path = tmp_path / "2C03_outline_W26.pdf"
    doc.save(path)
    doc.close()
    return path


class TestPdfExtraction:
    def test_quality_ok(self, outline_pdf):
        assert extract(outline_pdf).quality == "ok"

    def test_page_count(self, outline_pdf):
        assert extract(outline_pdf).page_count == 3

    def test_one_segment_per_page(self, outline_pdf):
        assert len(extract(outline_pdf).segments) == 3

    def test_positions_are_page_numbers(self, outline_pdf):
        positions = [s.position for s in extract(outline_pdf).segments]
        assert positions == ["p.1", "p.2", "p.3"]

    def test_page_attribute_set_for_rendering(self, outline_pdf):
        assert [s.page for s in extract(outline_pdf).segments] == [1, 2, 3]

    def test_late_policy_lands_on_page_three(self, outline_pdf):
        """The canonical eval question: 'what's the late penalty in 2C03?'
        must be citable to a specific page."""
        segments = extract(outline_pdf).segments
        hit = next(s for s in segments if "10% per day" in s.text)
        assert hit.position == "p.3"
        assert hit.page == 3


class TestChunkingRealPdf:
    def test_chunks_carry_page_citation(self, outline_pdf):
        from avenue_mcp.rag.chunk import chunk_segments

        result = extract(outline_pdf)
        chunks = chunk_segments(
            result.segments,
            course_name="COMPSCI 2C03",
            module_path="Week 1",
            file_name=outline_pdf.name,
        )
        assert chunks
        joined = " ".join(c.body for c in chunks)
        assert "10% per day" in joined

        # The context header is prepended to the EMBEDDED text and stripped from
        # the displayed body. Assert on the bracketed header itself -- the bare
        # course name appears in the document's own text on page 1, so searching
        # for that would pass vacuously.
        for c in chunks:
            header_line = c.text.split("\n\n", 1)[0]
            assert header_line.startswith("[") and header_line.endswith("]")
            assert "COMPSCI 2C03" in header_line
            assert "Week 1" in header_line
            assert header_line not in c.body


class TestRendering:
    def test_pdf_is_renderable(self, outline_pdf):
        assert is_renderable(outline_pdf)

    def test_page_count_matches(self, outline_pdf):
        assert page_count(outline_pdf) == 3

    def test_renders_png(self, outline_pdf, tmp_path):
        png, meta = render_page(outline_pdf, 3, dpi=100, cache_dir=tmp_path / "r")
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert meta["page"] == 3
        assert meta["total_pages"] == 3
        assert meta["rendered_width"] > 0

    def test_render_is_cached_second_time(self, outline_pdf, tmp_path):
        cache = tmp_path / "r"
        first, m1 = render_page(outline_pdf, 1, dpi=100, cache_dir=cache)
        second, m2 = render_page(outline_pdf, 1, dpi=100, cache_dir=cache)
        assert first == second
        assert m1["cached"] is False
        assert m2["cached"] is True

    def test_out_of_range_page(self, outline_pdf, tmp_path):
        with pytest.raises(RenderError, match="out of range"):
            render_page(outline_pdf, 99, cache_dir=tmp_path)

    def test_zero_page_rejected(self, outline_pdf, tmp_path):
        with pytest.raises(RenderError, match="1-indexed"):
            render_page(outline_pdf, 0, cache_dir=tmp_path)

    def test_unrenderable_format(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("not renderable", encoding="utf-8")
        assert not is_renderable(p)
        with pytest.raises(RenderError):
            render_page(p, 1, cache_dir=tmp_path)

    def test_missing_file(self, tmp_path):
        with pytest.raises(RenderError, match="not cached"):
            render_page(tmp_path / "gone.pdf", 1, cache_dir=tmp_path)


class TestScannedPdfDetection:
    def test_image_only_pdf_reported_not_silently_empty(self, tmp_path):
        """A file that appears indexed but never retrieves leaves the user
        wondering why searching for their outline finds nothing."""
        doc = pymupdf.open()
        for _ in range(3):
            doc.new_page()  # no text layer at all
        path = tmp_path / "scanned.pdf"
        doc.save(path)
        doc.close()

        result = extract(path)
        assert result.quality in ("empty", "poor")
        assert result.note
        assert "OCR" in result.note or "No text" in result.note


class TestDocxPptx:
    def test_docx_headings_become_positions(self, tmp_path):
        docx = pytest.importorskip("docx")
        d = docx.Document()
        d.add_heading("Grading", level=1)
        d.add_paragraph("Assignments are worth 40%.")
        d.add_heading("Late Policy", level=1)
        d.add_paragraph("10% per day.")
        path = tmp_path / "a.docx"
        d.save(path)

        result = extract(path)
        assert result.quality == "ok"
        assert any("Late Policy" in (s.heading or "") for s in result.segments)

    def test_pptx_slides_and_notes(self, tmp_path):
        pptx = pytest.importorskip("pptx")
        prs = pptx.Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = "Amortized Analysis"
        slide.notes_slide.notes_text_frame.text = "Explain the accounting method."
        path = tmp_path / "deck.pptx"
        prs.save(path)

        result = extract(path)
        assert result.quality == "ok"
        assert result.segments[0].position == "slide 1"
        # Notes often carry the actual explanation.
        assert "accounting method" in result.full_text
