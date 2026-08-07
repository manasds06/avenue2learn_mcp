"""Shared application context.

One place that owns the session, client, store, and embedder, so tools stay thin
and testable against a fake.

Two properties this file exists to protect:

* Startup does no network I/O and requires no session. An MCP client launches
  the server and expects it up immediately; blocking on a network call -- or
  worse, a login prompt -- makes the client look broken.
* Local-only tools never call `require_session()`. That is what makes offline
  search real rather than aspirational, and it is an easy line to add
  reflexively for consistency and thereby destroy.
"""

from __future__ import annotations

import logging
from typing import Any

from avenue_mcp.auth.session import CookieSessionAuth
from avenue_mcp.client.d2l import D2LClient
from avenue_mcp.config import Settings, get_settings
from avenue_mcp.errors import NotIndexedError
from avenue_mcp.rag.embed import Embedder
from avenue_mcp.rag.store import Store
from avenue_mcp.rag.sync import Syncer
from avenue_mcp.util.throttle import Keepalive

log = logging.getLogger(__name__)


class AppContext:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()

        self.auth = CookieSessionAuth(self.settings.base_url, self.settings.session_path)
        self.client = D2LClient(self.auth, self.settings)
        self.store = Store(self.settings.index_path)
        self.embedder = Embedder(self.settings.embed_model)
        self.syncer = Syncer(self.client, self.store, self.embedder, self.settings)

        self.keepalive = Keepalive(
            ping=self.auth.is_alive,
            interval_minutes=self.settings.keepalive_minutes,
            idle_stop_minutes=self.settings.keepalive_idle_stop,
        )
        self._course_names: dict[int, str] = {}
        # Declared rather than set dynamically by tools. `whoami_resolved`
        # distinguishes "not looked up" from "looked up and unavailable", so a
        # failure is cached instead of retried per assignment folder.
        self.whoami_id: int | None = None
        self.whoami_resolved: bool = False

    # --- session gating ---------------------------------------------------

    async def require_session(self) -> None:
        """Call this at the top of every NETWORK tool -- and nowhere else.

        Raises NoSessionError or SessionExpiredError with an actionable message.
        """
        await self.auth.require_alive()
        self.note_activity()
        if self.keepalive.enabled and not self.keepalive.running:
            self.keepalive.start()

    def note_activity(self) -> None:
        self.keepalive.note_activity()

    # --- course names -----------------------------------------------------

    async def course_name(self, org_unit_id: int) -> str:
        """Resolve a display name, cheaply and with fallbacks.

        Course names appear in every citation and chunk context header, so this
        is called constantly -- hence the layered cache.
        """
        if org_unit_id in self._course_names:
            return self._course_names[org_unit_id]

        stored = self.store.course_name(org_unit_id)
        if stored:
            self._course_names[org_unit_id] = stored
            return stored

        try:
            from avenue_mcp.client import models as m

            data = await self.client.get("lp", f"courses/{org_unit_id}")
            name = m.pick(data, "Name", "OrgUnitName", "Code")
            if name:
                self._course_names[org_unit_id] = str(name)
                self.store.remember_course(
                    org_unit_id, str(name), m.pick(data, "Code", "CourseCode")
                )
                return str(name)
        except Exception as exc:  # noqa: BLE001 -- a name is not worth failing over
            log.debug("could not resolve course name for %s: %s", org_unit_id, exc)

        return f"Course {org_unit_id}"

    def remember_course(self, org_unit_id: int, name: str | None, code: str | None) -> None:
        if name:
            self._course_names[org_unit_id] = name
        self.store.remember_course(org_unit_id, name, code)

    # --- index guards -----------------------------------------------------

    def require_indexed(self, org_unit_id: int | None) -> None:
        if org_unit_id is None:
            if self.store.is_empty():
                raise NotIndexedError("Nothing has been indexed yet.")
            return
        if not self.store.has_course(org_unit_id):
            name = self.store.course_name(org_unit_id) or f"course {org_unit_id}"
            raise NotIndexedError(f"{name} has not been indexed yet.")

    def indexed_course_names(self) -> list[str]:
        """Returned on EVERY search call so the model can distinguish
        'not in the materials' from 'that course was never synced'."""
        return [
            c["course_name"] or f"Course {c['org_unit_id']}"
            for c in self.store.indexed_courses()
        ]

    # --- teardown ---------------------------------------------------------

    async def aclose(self) -> None:
        await self.keepalive.stop()
        await self.auth.aclose()

    def status(self) -> dict[str, Any]:
        return {
            "session": {
                **self.auth.status(),
                "keepalive_enabled": self.keepalive.enabled,
                "keepalive_running": self.keepalive.running,
            },
            "index": self.store.stats(),
            "config": {
                "base_url": self.settings.base_url,
                "writes_enabled": self.settings.enable_writes,
                "embed_model": self.settings.embed_model,
                "index_discussions": self.settings.index_discussions,
                "timezone": self.settings.timezone,
                "grade_scale_configured": self.settings.grade_scale is not None,
            },
        }


_CONTEXT: AppContext | None = None


def get_context() -> AppContext:
    global _CONTEXT
    if _CONTEXT is None:
        _CONTEXT = AppContext()
    return _CONTEXT


def set_context(ctx: AppContext | None) -> None:
    """Test seam."""
    global _CONTEXT
    _CONTEXT = ctx
