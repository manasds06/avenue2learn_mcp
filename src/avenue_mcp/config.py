"""Environment-driven configuration.

MCP servers are launched by a client with an env block, so environment
variables are the only configuration channel that reliably exists.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from avenue_mcp.errors import ConfigError
from avenue_mcp.institutions import (
    DEFAULT_INSTITUTION,
    INSTITUTIONS,
    Institution,
    custom_institution,
    get_institution,
)

log = logging.getLogger(__name__)

_DEFAULT_PROFILE = INSTITUTIONS[DEFAULT_INSTITUTION]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AVENUE_MCP_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Instance ---------------------------------------------------------
    # Which school's Brightspace this process talks to. One process serves one
    # institution; two schools means two MCP server entries with two env blocks.
    # See avenue_mcp/institutions.py for the registry.
    institution: str = DEFAULT_INSTITUTION

    # These default to the profile's values, but an explicit env override always
    # wins -- that is what lets an unlisted school work with no code change.
    # Resolved in _resolve_institution below.
    base_url: str = _DEFAULT_PROFILE.base_url
    login_url: str = _DEFAULT_PROFILE.login_url
    timezone: str = _DEFAULT_PROFILE.timezone

    # --- State ------------------------------------------------------------
    state_dir: Path = Field(default_factory=lambda: Path.home() / ".avenue-mcp")

    # --- Safety -----------------------------------------------------------
    # Off by default. When off, write tools are not registered at all, so
    # the model never sees them.
    enable_writes: bool = False

    # --- RAG --------------------------------------------------------------
    embed_model: str = "BAAI/bge-small-en-v1.5"
    index_discussions: bool = True
    render_dpi: int = 120
    max_file_mb: int = 100

    # --- Politeness -------------------------------------------------------
    max_concurrency: int = 4
    min_request_interval_ms: int = 100
    cache_ttl_seconds: int = 300
    request_timeout_seconds: float = 60.0
    download_timeout_seconds: float = 120.0
    max_retries: int = 3
    max_pages: int = 50

    # --- Keepalive --------------------------------------------------------
    # 0 disables. Default off until Phase 0 confirms sessions extend on
    # activity. See docs/01-authentication.md.
    keepalive_minutes: int = 0
    keepalive_idle_stop: int = 120

    # --- Grades -----------------------------------------------------------
    # Path to a JSON letter-grade cutoff table. Unset means no letter
    # projections are ever claimed -- cutoffs vary by faculty, so a single
    # hardcoded scale would be wrong for some of a student's own courses.
    grade_scale: Path | None = None

    # --- Logging ----------------------------------------------------------
    log_level: str = "INFO"

    _profile: Institution = PrivateAttr()

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("state_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v

    @model_validator(mode="after")
    def _resolve_institution(self) -> Settings:
        """Fill base_url/login_url/timezone from the profile, unless overridden.

        Precedence: explicit env/kwarg > selected profile > mcmaster.
        """
        try:
            profile = get_institution(self.institution)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None

        provided = self.model_fields_set
        for name in ("base_url", "login_url", "timezone"):
            if name not in provided:
                # Bypass __setattr__ validation; field_validator does not re-run
                # on assignment here, so normalize explicitly where it matters.
                object.__setattr__(self, name, getattr(profile, name))

        # An explicit base_url pointing somewhere the profile does not describe
        # means we are talking to a school we know nothing about. Inheriting the
        # selected profile's name -- let alone its measured capabilities -- would
        # be exactly the lie the capability system exists to prevent.
        if "base_url" in provided and self.base_url.rstrip("/") != profile.base_url:
            profile = custom_institution(self.base_url, self.timezone)

        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        self._profile = profile
        return self

    # --- Institution ------------------------------------------------------
    @property
    def institution_profile(self) -> Institution:
        return self._profile

    @property
    def state_namespace(self) -> str:
        return self._profile.id

    # --- Derived paths ----------------------------------------------------
    @property
    def institution_dir(self) -> Path:
        """Per-institution state root.

        Namespaced because org_unit_ids are per-instance and WILL collide across
        schools -- a shared index would silently overwrite one school's course
        with another's -- and because a shared session.json means logging into
        one school destroys the other's session.
        """
        return self.state_dir / self.state_namespace

    @property
    def session_path(self) -> Path:
        return self.institution_dir / "session.json"

    @property
    def index_path(self) -> Path:
        return self.institution_dir / "index.db"

    @property
    def cache_dir(self) -> Path:
        return self.institution_dir / "cache"

    @property
    def render_dir(self) -> Path:
        return self.cache_dir / "renders"

    @property
    def log_dir(self) -> Path:
        return self.institution_dir / "logs"

    @property
    def writes_log_path(self) -> Path:
        return self.log_dir / "writes.log"

    def ensure_dirs(self) -> None:
        self._adopt_legacy_state()
        for p in (
            self.state_dir,
            self.institution_dir,
            self.cache_dir,
            self.render_dir,
            self.log_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)
        # These dirs hold a credential; keep them owner-only where the platform
        # supports it. institution_dir matters most -- it directly contains
        # session.json.
        for p in (self.state_dir, self.institution_dir):
            try:
                p.chmod(0o700)
            except (OSError, NotImplementedError):
                pass

    def _adopt_legacy_state(self) -> None:
        """Move pre-namespacing flat state into the institution subdirectory.

        State used to live directly under state_dir. That layout was
        unambiguously McMaster's, so it is only ever adopted into mcmaster/ --
        never into another school's namespace.

        Moves rather than copies: a copied session.json leaves a second live
        credential at a path nothing will subsequently chmod or ACL. Failure is
        non-fatal -- housekeeping must never take down a login.
        """
        if self.state_namespace != DEFAULT_INSTITUTION:
            return
        if self.institution_dir.exists():
            return
        legacy = [
            (self.state_dir / name, self.institution_dir / name)
            for name in ("session.json", "index.db", "cache", "logs")
        ]
        if not any(src.exists() for src, _ in legacy):
            return
        try:
            self.institution_dir.mkdir(parents=True, exist_ok=True)
            for src, dst in legacy:
                if src.exists() and not dst.exists():
                    src.replace(dst)
        except OSError:
            log.warning(
                "Could not migrate existing state into %s. The old files are "
                "still in %s; you may need to run `avenue-mcp login` again.",
                self.institution_dir,
                self.state_dir,
            )

    def load_grade_scale(self) -> dict[str, float] | None:
        """Letter-grade cutoffs, or None if not configured."""
        if self.grade_scale is None:
            return None
        try:
            data = json.loads(Path(self.grade_scale).expanduser().read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        out: dict[str, float] = {}
        for k, v in data.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
