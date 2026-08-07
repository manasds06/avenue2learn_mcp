"""HTML -> clean text, with links preserved separately.

Announcement and assignment-instruction bodies are HTML. Dumping raw markup
wastes context; stripping links loses information the user often needs (the
Zoom link, the reading, the form). So we return both.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")

# Elements whose text is never content.
_DROP = ("script", "style", "noscript", "head", "meta", "link")

# Elements that force a line break so paragraphs don't run together.
_BLOCK = (
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "section", "article", "table",
)


def html_to_text(html: str | None) -> str:
    """Flatten HTML to readable plain text, preserving block structure."""
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_DROP):
        tag.decompose()

    for tag in soup.find_all(_BLOCK):
        tag.insert_before("\n")
        tag.insert_after("\n")

    text = _WS.sub(" ", soup.get_text())
    lines = (line.strip() for line in text.split("\n"))
    text = "\n".join(line for line in lines if line)
    return _BLANKS.sub("\n\n", text).strip()


def extract_links(html: str | None, *, base_url: str | None = None) -> list[dict[str, str]]:
    """Pull out anchors as {text, url} pairs.

    Relative hrefs are resolved against `base_url` when given — Brightspace
    emits plenty of `/d2l/...` paths that are useless without the host prefix.
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[dict[str, str]] = []

    for anchor in soup.find_all("a"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "#")):
            continue
        url = urljoin(base_url, href) if base_url else href
        if url in seen:
            continue
        seen.add(url)
        links.append({"text": anchor.get_text(strip=True) or url, "url": url})

    return links


def clean_body(
    html: str | None, *, base_url: str | None = None
) -> tuple[str, list[dict[str, str]]]:
    """Convenience: the text and the links from one source."""
    return html_to_text(html), extract_links(html, base_url=base_url)
