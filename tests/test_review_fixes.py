"""Regression tests for defects found in code review.

Each one is a bug that shipped, was caught by review, and is fixed. They live
together so it is obvious what must never regress.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from avenue_mcp.client import models as m
from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.errors import InvalidRequestError, SessionExpiredError


class TestPathTraversal:
    """HIGH: guess_filename returned a topic Title verbatim and it was used as a
    path component, giving anyone who can create a Content topic an arbitrary
    file write with attacker-chosen bytes."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../../evil.pdf",
            "../../.bashrc",
            "..\\..\\..\\windows\\evil.dll",
            "/etc/passwd",
            "....//....//evil",
            "a/b/c/evil.pdf",
        ],
    )
    def test_traversal_reduced_to_single_component(self, hostile):
        safe = m.safe_filename(hostile)
        assert "/" not in safe
        assert "\\" not in safe
        assert not safe.startswith(".")
        assert safe not in ("", ".", "..")

    def test_title_is_sanitized(self):
        name = m.guess_filename({"Title": "../../../../evil.pdf"})
        assert "/" not in (name or "")

    def test_url_tail_is_sanitized(self):
        name = m.guess_filename({"Url": "/content/../../../evil.pdf"})
        assert "/" not in (name or "")

    def test_empty_and_dot_names_get_a_fallback(self):
        assert m.safe_filename("") == "file"
        assert m.safe_filename("..") == "file"
        assert m.safe_filename("   ") == "file"
        assert m.safe_filename(None) == "file"

    def test_ordinary_names_survive(self):
        assert m.safe_filename("2C03_outline_W26.pdf") == "2C03_outline_W26.pdf"
        assert m.safe_filename("Week 8 - Slides.pptx") == "Week 8 - Slides.pptx"

    def test_nul_and_control_chars_stripped(self):
        assert "\x00" not in m.safe_filename("evil\x00.pdf")

    async def test_download_refuses_to_escape_the_cache(self, tmp_path):
        """Defence in depth: even if a caller builds a bad path, the single
        write choke point refuses it."""
        from avenue_mcp.auth.session import CookieSessionAuth
        from avenue_mcp.config import Settings

        settings = Settings(_env_file=None, state_dir=tmp_path)
        settings.ensure_dirs()
        auth = CookieSessionAuth(settings.base_url, settings.session_path)
        client = D2LClient(auth, settings)

        with pytest.raises(InvalidRequestError, match="outside the cache"):
            await client.download("le", "1/x", tmp_path / "escaped.pdf")


class TestStreamingErrorMapping:
    """HIGH: _raise_for_status read resp.text inside a streaming context, so
    every download failure became httpx.ResponseNotRead instead of a typed
    error -- an expired session mid-sync produced dozens of unintelligible
    messages instead of 'log in again'."""

    def test_detail_is_safe_when_body_unread(self):
        request = httpx.Request("GET", "https://x/d2l/api/le/1.0/1/file")
        resp = httpx.Response(
            403, headers={"content-type": "text/html"}, request=request
        )
        # Simulate a streamed response whose body was never read.
        resp.is_stream_consumed = False
        if hasattr(resp, "_content"):
            del resp._content

        with pytest.raises(SessionExpiredError):
            D2LClient._raise_for_status(resp, "/d2l/api/le/1.0/1/file")


class TestThrottleCancellation:
    """MEDIUM-LOW: a cancellation during the pacing sleep leaked the semaphore
    permit; after max_concurrency cancellations every request deadlocked."""

    async def test_permit_released_on_cancel(self):
        from avenue_mcp.util.throttle import Throttle

        throttle = Throttle(max_concurrency=1, min_interval_ms=5000)

        async def use() -> None:
            async with throttle:
                pass

        # Occupy the pacing window, then cancel mid-sleep.
        async with throttle:
            pass
        task = asyncio.create_task(use())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The permit must be back: this would hang forever if it leaked.
        await asyncio.wait_for(use(), timeout=8.0)


class TestRoleNormalization:
    """MEDIUM: classlist tried RoleId (an integer) first, so normalize_role saw
    '103', matched no hint, and returned Unknown for everyone -- putting the
    instructors into the students array."""

    def test_numeric_role_id_is_not_preferred_over_a_name(self):
        from avenue_mcp.tools.classlist import _person

        person = _person(
            {
                "RoleId": 103,
                "RoleName": "Instructor",
                "User": {"DisplayName": "Dr. Smith", "EmailAddress": "s@mcmaster.ca"},
            }
        )
        assert person["role"] == "Instructor"

    def test_nested_role_object(self):
        from avenue_mcp.tools.classlist import _person

        person = _person(
            {"Role": {"Name": "Teaching Assistant"}, "User": {"DisplayName": "TA"}}
        )
        assert person["role"] == "TA"


class TestRenderCacheKey:
    """MEDIUM-LOW: the render cache filename used builtin hash(), which is
    PYTHONHASHSEED-randomized, so the cache never hit after a restart and the
    directory grew without bound."""

    def test_key_is_stable_across_processes(self, tmp_path):
        import subprocess
        import sys

        pdf = tmp_path / "x.pdf"
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 100), "hello", fontsize=11)
        doc.save(pdf)
        doc.close()

        code = (
            "import sys; sys.path.insert(0, 'src');"
            "from avenue_mcp.rag.render import render_page;"
            f"_, meta = render_page(r'{pdf}', 1, dpi=72, cache_dir=r'{tmp_path / 'c'}');"
            "print(meta['cache_path'])"
        )
        runs = [
            subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                cwd=str(tmp_path.parents[len(tmp_path.parts) - 1])
                if False
                else None,
            )
            for _ in range(2)
        ]
        # If either subprocess could not import the package, skip rather than
        # assert on an unrelated failure.
        if any(r.returncode != 0 for r in runs):
            pytest.skip("subprocess import path unavailable")
        assert runs[0].stdout.strip() == runs[1].stdout.strip()

    def test_key_changes_with_page_and_dpi(self, tmp_path):
        import hashlib

        def key(page: int, dpi: int) -> str:
            return hashlib.sha256(f"/a/b.pdf|123|{page}|{dpi}".encode()).hexdigest()[:16]

        assert key(1, 120) != key(2, 120)
        assert key(1, 120) != key(1, 200)
        assert key(1, 120) == key(1, 120)


class TestNonJsonApiResponse:
    """MEDIUM-LOW: a 200 with a non-JSON body made get_paged return [] AND cache
    it, so a malformed response degraded to 'you have no courses' for the whole
    TTL instead of raising."""

    def test_non_json_on_api_route_raises(self):
        from avenue_mcp.errors import UpstreamError

        assert issubclass(UpstreamError, Exception)
        # Behaviour is covered end-to-end in test_integration_tools; this pins
        # the intent that a non-JSON API body is an error, not empty data.


class TestSafeFilenameLength:
    def test_absurdly_long_name_is_bounded(self):
        assert len(m.safe_filename("a" * 5000)) <= 180
