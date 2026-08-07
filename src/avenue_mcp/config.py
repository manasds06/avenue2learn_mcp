"""Environment-driven settings (docs/05-architecture.md).

No config file, no CLI flags for anything that matters — MCP servers are
launched by a client with an env block, so env is the only channel that
reliably exists.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AVENUE_MCP_",
        extra="ignore",
        case_sensitive=False,
    )

    # Nothing here is McMaster-specific except the default URL and the login
    # flow's success detection. The Valence API is the same everywhere, so
    # another D2L school should mostly work by overriding this.
    base_url: str = "https://avenue.mcmaster.ca"
    state_dir: Path = Field(default_factory=lambda: Path.home() / ".avenue-mcp")

    enable_writes: bool = False

    embed_model: str = "BAAI/bge-small-en-v1.5"
    max_file_mb: int = 100

    max_concurrency: int = 4
    min_request_interval_ms: int = 100
    cache_ttl_seconds: int = 300
    request_timeout_seconds: float = 30.0

    log_level: str = "INFO"
    timezone: str = "America/Toronto"

    # A default httpx UA is an easy thing for a WAF to flag. Probe question B6
    # confirms whether a browser-like one is actually required.
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("state_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        return Path(os.path.expanduser(v)) if isinstance(v, str) else v

    # --- Derived paths ---------------------------------------------------

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
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def writes_log_path(self) -> Path:
        return self.log_dir / "writes.log"

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        """Create the state directory tree. Cheap, idempotent, no network."""
        for d in (self.state_dir, self.cache_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Test hook — forces the next get_settings() to re-read the environment."""
    global _settings
    _settings = None
