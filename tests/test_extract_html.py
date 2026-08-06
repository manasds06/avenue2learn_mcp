"""Extraction and HTML tests."""

from __future__ import annotations

import pytest

from avenue_mcp.errors import ExtractionError
from avenue_mcp.rag.extract import extract, is_supported, sha256_file
from avenue_mcp.util.html import extract_links, to_text, to_text_and_links, truncate


class TestHtml:
    def test_plain_text_passthrough(self):
        assert to_text("hello world") == "hello world"

    def test_tags_stripped(self):
        assert to_text("<p>Hello <b>world</b></p>").strip() == "Hello world"

    def test_scripts_dropped(self):
        out = to_text("<p>Keep</p><script>alert('no')</script>")
        assert "alert" not in out
        assert "Keep" in out

    def test_block_structure_becomes_newlines(self):
        out = to_text("<p>One</p><p>Two</p>")
        assert "One" in out and "Two" in out
        assert "\n" in out

    def test_list_items_marked(self):
        assert "-" in to_text("<ul><li>First</li><li>Second</li></ul>")

    def test_nbsp_normalized(self):
        assert "\xa0" not in to_text("<p>a&nbsp;b</p>")

    def test_links_extracted_and_deduped(self):
        html = '<a href="https://a.com">A</a><a href="https://a.com">again</a>'
        assert extract_links(html) == ["https://a.com"]

    def test_anchors_and_js_skipped(self):
        html = '<a href="#top">t</a><a href="javascript:void(0)">j</a>'
        assert extract_links(html) == []

    def test_text_and_links_together(self):
        text, links = to_text_and_links('<p>See <a href="https://x.com">this</a></p>')
        assert "See" in text
        assert links == ["https://x.com"]

    def test_empty_inputs(self):
        assert to_text(None) == ""
        assert to_text("") == ""
        assert extract_links(None) == []


class TestTruncate:
    def test_under_limit_untouched(self):
        text, cut = truncate("short", 100)
        assert text == "short" and cut is False

    def test_over_limit_flagged(self):
        text, cut = truncate("word " * 500, 100)
        assert cut is True
        assert "truncated" in text

    def test_zero_limit_disables(self):
        text, cut = truncate("anything", 0)
        assert cut is False and text == "anything"


class TestSupported:
    @pytest.mark.parametrize("name", ["a.pdf", "b.docx", "c.pptx", "d.txt", "e.md", "f.html"])
    def test_supported(self, name):
        assert is_supported(name)

    @pytest.mark.parametrize("name", ["a.mp4", "b.zip", "c.xlsx", "d.png", "noext"])
    def test_unsupported(self, name):
        assert not is_supported(name)


class TestExtractPlain:
    def test_txt(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("First block.\n\nSecond block.", encoding="utf-8")
        result = extract(p)
        assert result.quality == "ok"
        assert len(result.segments) == 2
        assert "First block." in result.full_text

    def test_markdown(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("# Title\n\nBody text here.", encoding="utf-8")
        assert extract(p).quality == "ok"

    def test_html_file(self, tmp_path):
        p = tmp_path / "a.html"
        p.write_text("<html><body><p>Hello</p></body></html>", encoding="utf-8")
        result = extract(p)
        assert "Hello" in result.full_text

    def test_empty_file_reports_empty_not_ok(self, tmp_path):
        """Silently indexing an empty document produces a file that appears
        indexed, never retrieves, and leaves the user confused."""
        p = tmp_path / "a.txt"
        p.write_text("", encoding="utf-8")
        result = extract(p)
        assert result.quality == "empty"
        assert result.note

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ExtractionError):
            extract(tmp_path / "nope.txt")

    def test_unsupported_raises(self, tmp_path):
        p = tmp_path / "a.zip"
        p.write_bytes(b"PK\x03\x04")
        with pytest.raises(ExtractionError):
            extract(p)

    def test_positions_recorded_for_citation(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("One.\n\nTwo.\n\nThree.", encoding="utf-8")
        result = extract(p)
        assert all(s.position for s in result.segments)


class TestHashing:
    def test_stable(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("content", encoding="utf-8")
        assert sha256_file(p) == sha256_file(p)

    def test_changes_with_content(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("one", encoding="utf-8")
        first = sha256_file(p)
        p.write_text("two", encoding="utf-8")
        assert sha256_file(p) != first
