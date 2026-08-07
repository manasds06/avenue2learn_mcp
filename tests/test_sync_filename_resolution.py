"""discover_files must find the real filename even when the listing hides it.

Some Brightspace instances omit `Url` from the module *structure* listing while
returning it from `content/topics/{id}`. Carleton does; McMaster does not.

Without back-filling, `guess_filename` falls back to the display title, which
carries no extension, so `extractor.is_supported()` rejects everything and a
course full of real PDFs reports "48 found, 0 indexed, 48 unsupported" -- a
silent, total failure of the search feature that looks like a content problem
rather than a bug. Measured live on Carleton before the fix; see docs/09.
"""

from __future__ import annotations

import pytest

from avenue_mcp.config import Settings
from avenue_mcp.rag.sync import Syncer

ORG = 418

# Carleton-shaped: topics carry a title and a TopicType, but NO Url.
LISTING_WITHOUT_URL = [
    {
        "Id": 900,
        "Title": "Week 1",
        "Type": "Module",
        "Structure": [
            {"Id": 11, "Title": "01_Lecture_Notes", "Type": "Topic", "TopicType": 1},
            {"Id": 12, "Title": "02_Slide_Deck", "Type": "Topic", "TopicType": 1},
            # An external link. Its detail record returns an absolute Url, so it
            # must stay unsupported rather than being downloaded as HTML.
            {"Id": 13, "Title": "Course outline", "Type": "Topic", "TopicType": 1},
        ],
    }
]

TOPIC_DETAIL = {
    11: {"Id": 11, "Title": "01_Lecture_Notes",
         "Url": "/content/enforced/418-X/01_Lecture_Notes.pdf",
         "LastModifiedDate": "2026-01-05T12:00:00.000Z"},
    12: {"Id": 12, "Title": "02_Slide_Deck",
         "Url": "/content/enforced/418-X/02_Slide_Deck.pptx"},
    13: {"Id": 13, "Title": "Course outline",
         "Url": "https://syllabus.example.com/doc/abc123?mode=view"},
}


class FakeClient:
    """Minimal stand-in: discover_files only ever calls .get()."""

    def __init__(self, listing, details, *, detail_ok=True):
        self.listing = listing
        self.details = details
        self.detail_ok = detail_ok
        self.detail_calls = 0

    async def get(self, component, suffix, **kw):
        if suffix.endswith("/content/root/") or suffix.endswith("content/root/"):
            return self.listing
        if "/content/topics/" in suffix:
            self.detail_calls += 1
            if not self.detail_ok:
                from avenue_mcp.errors import PermissionDeniedError

                raise PermissionDeniedError("nope")
            topic_id = int(suffix.rstrip("/").split("/")[-1])
            return self.details[topic_id]
        return []


def make_syncer(client) -> Syncer:
    return Syncer(client, store=None, embedder=None, settings=Settings())


@pytest.fixture
def syncer_without_url():
    client = FakeClient(LISTING_WITHOUT_URL, TOPIC_DETAIL)
    return make_syncer(client), client


class TestBackfill:
    async def test_extensions_recovered_from_topic_detail(self, syncer_without_url):
        syncer, _ = syncer_without_url
        topics = await syncer.discover_files(ORG)
        names = {t["topic_id"]: t["file_name"] for t in topics}

        assert names[11] == "01_Lecture_Notes.pdf"
        assert names[12] == "02_Slide_Deck.pptx"

    async def test_mime_type_is_filled_in_too(self, syncer_without_url):
        # Without this, extraction falls back to suffix-only dispatch.
        syncer, _ = syncer_without_url
        topics = {t["topic_id"]: t for t in await syncer.discover_files(ORG)}

        assert topics[11]["mime_type"] == "application/pdf"
        assert "presentationml" in topics[12]["mime_type"]

    async def test_external_links_stay_unsupported(self, syncer_without_url):
        # An absolute Url is a publisher/syllabus site, not a file in
        # Brightspace. Downloading it would fetch someone else's HTML.
        syncer, _ = syncer_without_url
        topics = {t["topic_id"]: t for t in await syncer.discover_files(ORG)}

        from avenue_mcp.rag import extract as extractor

        assert not extractor.is_supported(topics[13]["file_name"] or "")

    async def test_last_modified_backfilled_when_listing_omits_it(
        self, syncer_without_url
    ):
        # Incremental resync depends on this; without it every sync re-downloads.
        syncer, _ = syncer_without_url
        topics = {t["topic_id"]: t for t in await syncer.discover_files(ORG)}

        assert topics[11]["last_modified"] == "2026-01-05T12:00:00.000Z"


class TestNoRegressionForInstancesThatAlreadyWork:
    async def test_listing_with_url_costs_no_extra_requests(self):
        """McMaster's shape must not pay for Carleton's problem."""
        listing = [
            {
                "Id": 900,
                "Title": "Week 1",
                "Type": "Module",
                "Structure": [
                    {
                        "Id": 21,
                        "Title": "Course Outline",
                        "Type": "Topic",
                        "TopicType": 1,
                        "Url": "/content/enforced/418-X/outline.pdf",
                        "LastModifiedDate": "2026-01-05T12:00:00.000Z",
                    }
                ],
            }
        ]
        client = FakeClient(listing, {})
        topics = await make_syncer(client).discover_files(ORG)

        assert topics[0]["file_name"] == "outline.pdf"
        assert client.detail_calls == 0, "no detail fetch needed when Url is present"


class TestDegradation:
    async def test_unreadable_detail_does_not_break_discovery(self):
        """A denied detail route must degrade to "unsupported", not raise --
        otherwise one bad topic takes down the whole sync."""
        client = FakeClient(LISTING_WITHOUT_URL, TOPIC_DETAIL, detail_ok=False)
        topics = await make_syncer(client).discover_files(ORG)

        assert len(topics) == 3
        from avenue_mcp.rag import extract as extractor

        assert all(not extractor.is_supported(t["file_name"] or "") for t in topics)
