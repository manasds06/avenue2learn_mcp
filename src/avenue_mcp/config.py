"""Environment-driven configuration.

MCP servers are launched by a client with an env block, so environment
variables are the only configuration channel that reliably exists.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AVENUE_MCP_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Instance ---------------------------------------------------------
    # IMPORTANT: avenue.mcmaster.ca is a static Apache landing page, NOT the
    # Brightspace application -- every /d2l/* path 404s there. The real
    # Brightspace host is avenue.cllmcmaster.ca, confirmed by
    # GET /d2l/api/versions/ returning live Valence JSON (lp 1.62, le 1.96).
    #
    # Login STARTS on the landing page (login.php -> Microsoft SAML) and lands
    # on the Brightspace host, so the two are configured separately.
    base_url: str = "https://avenue.cllmcmaster.ca"
    login_url: str = "https://avenue.mcmaster.ca/login.php"
    timezone: str = "America/Toronto"

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

    # --- Derived paths ----------------------------------------------------
    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.json"

    @property
    def index_path(self) -> Path:
        return self.state_dir / "index.db"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def render_dir(self) -> Path:
        return self.cache_dir / "renders"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def writes_log_path(self) -> Path:
        return self.log_dir / "writes.log"

    def ensure_dirs(self) -> None:
        for p in (self.state_dir, self.cache_dir, self.render_dir, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)
        # The state dir holds a credential; keep it owner-only where the
        # platform supports it.
        try:
            self.state_dir.chmod(0o700)
        except (OSError, NotImplementedError):
            pass

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
