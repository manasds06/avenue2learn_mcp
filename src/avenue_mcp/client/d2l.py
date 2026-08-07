"""D2L Valence REST client.

Knows nothing about MCP -- usable from a script or a test. Handles:

* API version negotiation (never hardcode a version)
* bookmark pagination (implemented once, generically)
* throttling and jittered backoff
* expiry detection that does not trust status codes alone
* mapping HTTP to the typed error taxonomy
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from avenue_mcp.auth.session import CookieSessionAuth
from avenue_mcp.config import Settings
from avenue_mcp.errors import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    SessionExpiredError,
    UpstreamError,
)
from avenue_mcp.util.throttle import Throttle, TTLCache, with_backoff

log = logging.getLogger(__name__)

Component = Literal["lp", "le"]

# Fallbacks used only if /d2l/api/versions/ is unreachable. Conservative
# on purpose -- 1.0 is universally supported.
_FALLBACK_VERSIONS: dict[str, str] = {"lp": "1.0", "le": "1.0"}



class _Ambiguous403(Exception):
    """A 403 with an HTML body: either the sign-in wall or a real permission
    denial. Only a liveness probe separates them, so this never escapes the
    client -- D2LClient._resolve_403 converts it."""

    def __init__(self, url: str, detail: str = "") -> None:
        super().__init__(url)
        self.url = url
        self.detail = detail


class D2LClient:
    """Thin async wrapper over the Valence API."""

    def __init__(self, auth: CookieSessionAuth, settings: Settings) -> None:
        self.auth = auth
        # Cache for the 403 disambiguation above.
        self._session_alive: bool = True
        self._liveness_checked_at: float | None = None
        self.settings = settings
        self._versions: dict[str, str] | None = None
        self._throttle = Throttle(
            settings.max_concurrency, settings.min_request_interval_ms
        )
        self._cache = TTLCache(settings.cache_ttl_seconds)

    # --- versions ---------------------------------------------------------

    async def versions(self) -> dict[str, str]:
        """Negotiate versions once per process, then cache."""
        if self._versions is not None:
            return self._versions
        try:
            data = await self._raw_json("GET", "/d2l/api/versions/")
        except Exception as exc:  # noqa: BLE001 -- fall back, don't die
            log.warning("version negotiation failed (%s); using fallbacks", exc)
            self._versions = dict(_FALLBACK_VERSIONS)
            return self._versions

        found: dict[str, str] = {}
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                code = str(entry.get("ProductCode", "")).lower()
                latest = entry.get("LatestVersion")
                if code and isinstance(latest, str):
                    found[code] = latest
        self._versions = {**_FALLBACK_VERSIONS, **found}
        log.info("API versions: %s", self._versions)
        return self._versions

    async def version_for(self, component: Component) -> str:
        return (await self.versions()).get(component, _FALLBACK_VERSIONS[component])

    async def path(self, component: Component, suffix: str) -> str:
        v = await self.version_for(component)
        return f"/d2l/api/{component}/{v}/{suffix.lstrip('/')}"

    # --- requests ---------------------------------------------------------

    async def get(
        self,
        component: Component,
        suffix: str,
        *,
        params: dict[str, Any] | None = None,
        cache: bool = True,
    ) -> Any:
        url = await self.path(component, suffix)
        key = f"{url}?{sorted((params or {}).items())}"
        if cache:
            hit = self._cache.get(key)
            if hit is not None:
                return hit
        data = await self._raw_json("GET", url, params=params)
        if cache:
            self._cache.put(key, data)
        return data

    async def get_paged(
        self,
        component: Component,
        suffix: str,
        *,
        params: dict[str, Any] | None = None,
        cache: bool = True,
    ) -> list[Any]:
        """Follow bookmark pagination to exhaustion.

        Implemented once here. A per-call-site paging loop is how you silently
        truncate a course list at page one.
        """
        url = await self.path(component, suffix)
        base = dict(params or {})
        key = f"PAGED {url}?{sorted(base.items())}"
        if cache:
            hit = self._cache.get(key)
            if isinstance(hit, list):
                return hit

        items: list[Any] = []
        bookmark: str | None = None
        for page in range(self.settings.max_pages):
            q = dict(base)
            if bookmark:
                q["bookmark"] = bookmark
            data = await self._raw_json("GET", url, params=q)

            if isinstance(data, list):
                items.extend(data)
                break  # unpaged route
            if not isinstance(data, dict):
                break

            chunk = data.get("Items")
            if isinstance(chunk, list):
                items.extend(chunk)
            elif isinstance(data.get("Objects"), list):
                items.extend(data["Objects"])

            paging = data.get("PagingInfo") or {}
            has_more = bool(paging.get("HasMoreItems"))
            next_bm = paging.get("Bookmark")
            if not has_more or not next_bm or next_bm == bookmark:
                break
            bookmark = str(next_bm)
            if page == self.settings.max_pages - 1:
                log.warning("paging cap (%d) hit for %s", self.settings.max_pages, url)

        if cache:
            self._cache.put(key, items)
        return items

    async def post_multipart(
        self,
        component: Component,
        suffix: str,
        *,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """Used only by the gated write path."""
        url = await self.path(component, suffix)
        result = await self._raw_json("POST", url, data=data, files=files)
        # A write invalidates anything we may have cached about this course --
        # otherwise a submission can succeed while list_assignments keeps
        # reporting "not_submitted" for the rest of the TTL.
        self.clear_cache()
        return result

    async def download(self, component: Component, suffix: str, dest: Any) -> dict[str, Any]:
        """Stream a file body to `dest` (a Path).

        Streams rather than buffers: lecture decks run 20-80 MB and buffering a
        whole course hurts. Enforces the size cap so one pathological file
        cannot stall a sync.
        """
        from pathlib import Path

        dest = Path(dest)

        # Defence in depth. Callers sanitize the filename (models.safe_filename),
        # but this is the single choke point where remote-controlled bytes get
        # written to a remote-influenced path, so it refuses to write outside the
        # cache root regardless of how the path was built.
        root = Path(self.settings.cache_dir).resolve()
        try:
            resolved = dest.resolve()
            resolved.relative_to(root)
        except (ValueError, OSError) as exc:
            raise InvalidRequestError(
                f"Refusing to write outside the cache directory: {dest}"
            ) from exc
        dest = resolved

        dest.parent.mkdir(parents=True, exist_ok=True)
        url = await self.path(component, suffix)
        cap = self.settings.max_file_mb * 1024 * 1024
        client = self.auth.client(self.settings.download_timeout_seconds)

        async with self._throttle:
            async with client.stream(
                "GET", url, timeout=self.settings.download_timeout_seconds
            ) as resp:
                self._raise_for_session(resp, url)
                if resp.status_code >= 400:
                    # The body has not been read on a streaming response, and
                    # _raise_for_status touches resp.text to build its message.
                    # Without this, every download failure surfaced as
                    # httpx.ResponseNotRead instead of SessionExpiredError /
                    # PermissionDeniedError -- i.e. an expired session mid-sync
                    # produced 40 unintelligible errors instead of "log in again".
                    await resp.aread()
                try:
                    self._raise_for_status(resp, url)
                except _Ambiguous403 as amb:
                    await self._resolve_403(amb)

                ctype = resp.headers.get("content-type", "")
                filename = _filename_from(resp.headers.get("content-disposition"))
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > cap:
                    raise InvalidRequestError(
                        f"File is {int(declared) // 1048576} MB, over the "
                        f"{self.settings.max_file_mb} MB cap."
                    )

                total = 0
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as fh:
                    async for block in resp.aiter_bytes(65536):
                        total += len(block)
                        if total > cap:
                            fh.close()
                            tmp.unlink(missing_ok=True)
                            raise InvalidRequestError(
                                f"File exceeded the {self.settings.max_file_mb} MB cap."
                            )
                        fh.write(block)
                tmp.replace(dest)

        return {
            "path": str(dest),
            "bytes": total,
            "mime_type": ctype.split(";")[0].strip() or None,
            "filename": filename,
        }

    # --- plumbing ---------------------------------------------------------

    async def _raw_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        client = self.auth.client(self.settings.request_timeout_seconds)
        headers = await self.auth.headers_for(method)

        async def attempt() -> Any:
            async with self._throttle:
                resp = await client.request(
                    method,
                    url,
                    params=_clean(params),
                    data=data,
                    files=files,
                    headers=headers,
                )
            self._raise_for_session(resp, url)
            try:
                self._raise_for_status(resp, url)
            except _Ambiguous403 as amb:
                await self._resolve_403(amb)
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                # A 200 that isn't JSON on an API route is not data. Returning
                # the raw text meant get_paged matched neither list nor dict,
                # returned [], and CACHED that empty list -- so a malformed
                # response degraded to "you have no courses" for five minutes
                # instead of surfacing an error. (The HTML case is already
                # caught earlier as an expired session.)
                if url.startswith("/d2l/api/"):
                    raise UpstreamError(
                        f"Avenue returned a non-JSON body for {url}."
                    ) from exc
                return resp.text

        return await with_backoff(
            attempt,
            max_attempts=self.settings.max_retries,
            should_retry=_retryable,
            retry_after=_retry_after,
        )

    @staticmethod
    def _raise_for_session(resp: httpx.Response, url: str) -> None:
        """Expiry detection that does not trust the status code alone.

        Brightspace may answer an expired session with a 302 to SSO, or a 200
        whose body is an HTML login form, rather than a clean 401. A naive
        client parses that HTML as JSON and reports a confusing error.
        """
        if 300 <= resp.status_code < 400:
            loc = resp.headers.get("location", "")
            host = urlparse(loc).netloc.lower() if loc else ""
            api_host = urlparse(str(resp.request.url)).netloc.lower()
            if not loc or (host and host != api_host) or "login" in loc.lower():
                raise SessionExpiredError(
                    f"Avenue redirected {url} to sign-in; the session has expired."
                )
        if resp.status_code == 200 and url.startswith("/d2l/api/"):
            ctype = resp.headers.get("content-type", "").lower()
            if "text/html" in ctype:
                raise SessionExpiredError(
                    f"Avenue returned a login page for {url}; the session has expired."
                )

    async def _resolve_403(self, exc: "_Ambiguous403") -> None:
        """Turn an ambiguous 403+HTML into the right typed error.

        One extra request, only on a 403, and the answer is cached briefly so a
        burst (a sync touching 40 topics) probes once rather than 40 times.
        """
        now = time.monotonic()
        if self._liveness_checked_at is None or now - self._liveness_checked_at > 30.0:
            try:
                self._session_alive = await self.auth.is_alive()
            except Exception:  # noqa: BLE001 - a failed probe is not an answer
                self._session_alive = False
            self._liveness_checked_at = now

        if self._session_alive:
            raise PermissionDeniedError(
                f"Access denied for {exc.url}. Your account cannot read this on "
                f"Avenue -- it is likely instructor-only. {exc.detail}".strip()
            )
        raise SessionExpiredError(
            f"Avenue returned its sign-in wall for {exc.url}; the session is not valid."
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response, url: str) -> None:
        code = resp.status_code
        if code < 400:
            return
        # Belt and braces: on a streaming response the body may still be
        # unread, and reaching for it would raise ResponseNotRead and mask the
        # real status.
        try:
            detail = _short(resp.text)
        except httpx.ResponseNotRead:
            detail = ""
        ctype = resp.headers.get("content-type", "").lower()
        if code == 401:
            raise SessionExpiredError(f"Not authenticated for {url}.")
        if code == 403:
            # A 403 means two very different things, and getting it backwards
            # sends the user down a dead end either way.
            #
            # Verified against the live host: an ANONYMOUS request to an API
            # route returns 403 with a text/html body -- Brightspace's auth
            # wall. Treating that as "permission denied" would tell a logged-out
            # user that signing in will not help, which is exactly wrong.
            #
            # A genuine permission denial from an authenticated session comes
            # back as JSON, because the API is answering us rather than
            # bouncing us to a login page.
            if "html" in ctype:
                # ...but content-type alone cannot tell them apart. Measured on
                # the live host with a VALID session:
                #
                #   GET /d2l/api/lp/1.62/courses/{id}
                #   -> 403, content-type: text/html, body: "Forbidden"
                #
                # while whoami returns 200 JSON on that same session. So an
                # authenticated permission denial *also* arrives as 403+HTML,
                # and reporting "the session is not valid" sends the user to
                # re-login against a wall that will never move.
                #
                # Worse, SessionExpiredError is an AuthError, not an APIError,
                # so it slips past every `except PermissionDeniedError` /
                # `except APIError` degradation arm in tools/ and fails the
                # whole call instead of degrading.
                #
                # Liveness is the only signal that actually separates them, and
                # the caller resolves it -- this function is sync.
                raise _Ambiguous403(url, detail)
            raise PermissionDeniedError(f"Access denied for {url}. {detail}".strip())
        if code == 404:
            raise NotFoundError(f"Not found: {url}. {detail}".strip())
        if code in (400, 422):
            raise InvalidRequestError(f"Bad request to {url}. {detail}".strip())
        if code == 413:
            raise InvalidRequestError(f"Request too large for {url}.")
        if code == 429:
            raise _RateLimited(resp)
        raise UpstreamError(f"Avenue returned {code} for {url}. {detail}".strip())

    def clear_cache(self) -> None:
        self._cache.clear()


class _RateLimited(UpstreamError):
    """Internal: carries Retry-After so backoff can honor it."""

    def __init__(self, resp: httpx.Response) -> None:
        super().__init__("Rate limited by Avenue.")
        self.retry_after = resp.headers.get("retry-after")


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, _RateLimited):
        return True
    if isinstance(exc, UpstreamError):
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    # Never retry auth, permission, 404, or bad-request failures.
    return False


def _retry_after(exc: BaseException) -> float | None:
    if isinstance(exc, _RateLimited) and exc.retry_after:
        try:
            return max(0.0, float(exc.retry_after))
        except ValueError:
            return None
    return None


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _short(text: str, limit: int = 200) -> str:
    t = " ".join((text or "").split())
    if "<html" in t.lower():
        return ""
    return t[:limit]


def _filename_from(disposition: str | None) -> str | None:
    if not disposition:
        return None
    for part in disposition.split(";"):
        part = part.strip()
        for key in ("filename*=", "filename="):
            if part.lower().startswith(key):
                val = part[len(key):].strip().strip('"')
                if val.lower().startswith("utf-8''"):
                    from urllib.parse import unquote

                    val = unquote(val[7:])
                return val or None
    return None
