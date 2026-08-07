"""HTML to clean text.

Announcement and discussion bodies are HTML. Dumping raw markup wastes
context; stripping links loses information the user often needs. So we return
text plus a separate list of link targets.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")
_DROP = ("script", "style", "noscript", "nav", "header", "footer", "iframe")
_BLOCK = (
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "section", "article",
)


def _parser() -> str:
    try:
        import lxml  # noqa: F401

        return "lxml"
    except ImportError:
        return "html.parser"


def to_text(html: str | None) -> str:
    """Extract readable text, preserving block structure as newlines."""
    if not html:
        return ""
    if "<" not in html:
        return _tidy(html)

    soup = BeautifulSoup(html, _parser())
    for tag in soup(_DROP):
        tag.decompose()
    for name in _BLOCK:
        for tag in soup.find_all(name):
            tag.insert_before("\n")
            tag.insert_after("\n")
    for li in soup.find_all("li"):
        li.insert_before("- ")

    return _tidy(soup.get_text())


def extract_links(html: str | None) -> list[str]:
    """Absolute-ish link targets, de-duplicated, order preserved."""
    if not html or "<" not in html:
        return []
    soup = BeautifulSoup(html, _parser())
    seen: dict[str, None] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        seen.setdefault(href, None)
    return list(seen)


def to_text_and_links(html: str | None) -> tuple[str, list[str]]:
    return to_text(html), extract_links(html)


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "")
    text = _WS.sub(" ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate on a word boundary. Returns (text, was_truncated)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    space = cut.rfind(" ")
    if space > max_chars * 0.8:
        cut = cut[:space]
    return cut.rstrip() + "\n\n[...truncated...]", True
