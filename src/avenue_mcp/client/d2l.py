"""D2LClient — version negotiation, paging, throttling, retries, error mapping.

Knows nothing about MCP. The tool layer calls this; this calls httpx.

The error mapping is the part worth reading carefully. Brightspace signals an
expired session in three different ways (401, a 302 to the SSO host, or a 200
whose body is an HTML login form), and a naive client parses that last one as
JSON and reports a confusing failure. See docs/01 "Detecting expiry".
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from avenue_mcp.auth.base import AuthProvider
from avenue_mcp.auth.session import SessionManager
from avenue_mcp.client.paging import MAX_PAGES, extract_items, paging_info
from avenue_mcp.config import Settings, get_settings
from avenue_mcp.errors import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    SessionExpiredError,
    UpstreamError,
)
from avenue_mcp.util.throttle import Throttle

log = logging.getLogger(__name__)

VERSIONS_PATH = "/d2l/api/versions/"

# Used only until /d2l/api/versions/ answers. Never used to build a real
# request path — see negotiate_versions().
_FALLBACK_VERSIONS = {"lp": "1.0", "le": "1.0"}


class D2LClient:
    """Async client for the Valence REST API."""

    def __init__(
        self,
        session: SessionManager | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session = session or SessionManager(self.settings)
        self._versions: dict[str, str] | None = None
        self._throttle = Throttle(
            max_concurrency=self.settings.max_concurrency,
            min_interval_ms=self.settings.min_request_interval_ms,
        )

    # --- Versioning ------------------------------------------------------

    async def negotiate_versions(self) -> dict[str, str]:
        """Pin API versions from the instance. Never hardcode them.

        Versions advance with each Brightspace release and differ per
        institution, so a hardcoded `1.57` is a time bomb. Cached for the
        process lifetime; it changes only when McMaster upgrades.
        """
        if self._versions is not None:
            return self._versions

        client, _ = await self.session.require_session()
        response = await self._send(client, "GET", VERSIONS_PATH, provider=None)
        payload = self._parse_json(response, VERSIONS_PATH)

        versions: dict[str, str] = {}
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                code = entry.get("ProductCode") or entry.get("productCode")
                latest = entry.get("LatestVersion") or entry.get("latestVersion")
                if isinstance(code, str) and isinstance(latest, str):
                    versions[code.lower()] = latest

        self._versions = {**_FALLBACK_VERSIONS, **versions}
        log.info("Negotiated API versions: %s", self._versions)
        return self._versions

    async def path(self, component: str, route: str) -> str:
        """Build `/d2l/api/{component}/{version}/{route}`.

        Trailing slashes in `route` are preserved — several Valence collection
        routes behave differently without one.
        """
        versions = await self.negotiate_versions()
        version = versions.get(component.lower(), _FALLBACK_VERSIONS.get(component.lower(), "1.0"))
        return f"/d2l/api/{component.lower()}/{version}/{route.lstrip('/')}"

    # --- Request plumbing ------------------------------------------------

    def _is_login_redirect(self, response: httpx.Response) -> bool:
        if response.status_code not in (301, 302, 303, 307, 308):
            return False
        location = response.headers.get("location", "")
        if not location:
            return False
        host = urlsplit(location).netloc.lower() or urlsplit(str(response.url)).netloc.lower()
        base_host = urlsplit(self.settings.base_url).netloc.lower()
        # A redirect off-host, or to a login path on-host, means SSO.
        return host != base_host or "login" in location.lower()

    def _raise_for_status(self, response: httpx.Response, route: str) -> None:
        status = response.status_code

        if self._is_login_redirect(response):
            raise SessionExpiredError()

        if status == 200:
            content_type = response.headers.get("content-type", "").lower()
            # A JSON route answering with HTML is the login page.
            if "text/html" in content_type:
                raise SessionExpiredError()
            return

        if status in (401,):
            raise SessionExpiredError()
        if status == 403:
            # Deliberately NOT SessionExpiredError. A 403 on a healthy session
            # is a permissions fact; telling the user to log in again sends
            # them down a dead end.
            raise PermissionDeniedError(
                f"Your Avenue account doesn't have access to {route} — it may be instructor-only."
            )
        if status == 404:
            raise NotFoundError(f"Avenue has no such item at {route}.")
        if status == 400:
            raise InvalidRequestError(
                f"Avenue rejected the request to {route}: {response.text[:200]}"
            )
        if status >= 500:
            raise UpstreamError(f"Avenue returned {status} for {route}.")
        if status >= 400:
            raise UpstreamError(f"Avenue returned {status} for {route}.")

    def _parse_json(self, response: httpx.Response, route: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(
                f"Avenue returned a non-JSON body for {route}."
            ) from exc

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        provider: AuthProvider | None,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """One request, throttled, with jittered backoff on 429/5xx."""
        attempt = 0
        while True:
            attempt += 1

            async def _once() -> httpx.Response:
                request = client.build_request(method, path, params=params, **kwargs)
                if provider is not None:
                    await provider.authorize(request)
                return await client.send(request)

            try:
                response = await self._throttle.run(_once)
            except httpx.HTTPError as exc:
                if attempt >= self._throttle.max_attempts:
                    raise UpstreamError(f"Network error calling {path}: {exc}") from exc
                await asyncio.sleep(self._throttle.backoff_delay(attempt))
                continue

            if response.status_code in (429,) or response.status_code >= 500:
                if attempt >= self._throttle.max_attempts:
                    self._raise_for_status(response, path)
                    return response
                retry_after = response.headers.get("retry-after")
                delay = self._throttle.backoff_delay(
                    attempt,
                    float(retry_after) if retry_after and retry_after.isdigit() else None,
                )
                log.debug("Retrying %s after %.1fs (status %d)", path, delay, response.status_code)
                await asyncio.sleep(delay)
                continue

            return response

    # --- Public API ------------------------------------------------------

    async def get_json(
        self,
        component: str,
        route: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET a versioned Valence route and return parsed JSON."""
        client, provider = await self.session.require_session()
        path = await self.path(component, route)
        response = await self._send(client, "GET", path, provider=provider, params=params)
        self._raise_for_status(response, path)
        return self._parse_json(response, path)

    async def get_paged(
        self,
        component: str,
        route: str,
        *,
        params: dict[str, Any] | None = None,
        max_pages: int = MAX_PAGES,
    ) -> list[Any]:
        """Follow bookmark paging to exhaustion and return every item."""
        client, provider = await self.session.require_session()
        path = await self.path(component, route)

        items: list[Any] = []
        query = dict(params or {})
        seen_bookmarks: set[str] = set()

        for page in range(max_pages):
            response = await self._send(client, "GET", path, provider=provider, params=query)
            self._raise_for_status(response, path)
            payload = self._parse_json(response, path)

            items.extend(extract_items(payload))

            bookmark, has_more = paging_info(payload)
            if not has_more or not bookmark:
                break
            # A server that echoes the same bookmark forever would otherwise
            # spin until max_pages; stop as soon as we notice.
            if bookmark in seen_bookmarks:
                log.warning("Paging bookmark repeated on %s; stopping at page %d", path, page + 1)
                break
            seen_bookmarks.add(bookmark)
            query["bookmark"] = bookmark
        else:
            log.warning("Hit the %d-page cap on %s; results may be truncated.", max_pages, path)

        return items

    async def download_file(
        self,
        component: str,
        route: str,
        *,
        destination: Path | None = None,
        max_bytes: int | None = None,
    ) -> tuple[dict[str, str], bytes | Path]:
        """Fetch raw file bytes from a route that returns a body, not JSON.

        Streams rather than buffering — lecture decks routinely run 20-80 MB,
        and a whole course's worth held in memory at once hurts. Writes to
        `destination` when given, otherwise returns the bytes.

        Aborts once `max_bytes` is exceeded rather than reading to the end:
        the point of a cap is to not pay for the download.
        """
        client, provider = await self.session.require_session()
        path = await self.path(component, route)
        cap = max_bytes if max_bytes is not None else self.settings.max_file_bytes

        request = client.build_request("GET", path)
        if provider is not None:
            await provider.authorize(request)

        async def _open() -> httpx.Response:
            return await client.send(request, stream=True)

        response = await self._throttle.run(_open)
        try:
            self._raise_for_status(response, path)

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > cap:
                raise InvalidRequestError(
                    f"File at {route} is {int(declared) // (1024 * 1024)} MB, over the "
                    f"{cap // (1024 * 1024)} MB cap. Raise AVENUE_MCP_MAX_FILE_MB to fetch it."
                )

            headers = dict(response.headers)
            total = 0

            if destination is not None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > cap:
                            fh.close()
                            destination.unlink(missing_ok=True)
                            raise InvalidRequestError(
                                f"File at {route} exceeded the {cap // (1024 * 1024)} MB cap."
                            )
                        fh.write(chunk)
                return headers, destination

            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > cap:
                    raise InvalidRequestError(
                        f"File at {route} exceeded the {cap // (1024 * 1024)} MB cap."
                    )
                buffer.extend(chunk)
            return headers, bytes(buffer)
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        await self.session.aclose()
