"""Shared test setup.

Every test runs against a temp state dir so a real session file, cache, or index
is never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path / "avenue-state"))
    monkeypatch.delenv("AVENUE_MCP_ENABLE_WRITES", raising=False)
    monkeypatch.setenv("AVENUE_MCP_KEEPALIVE_MINUTES", "0")

    # Pin the institution explicitly rather than deleting the var. Settings'
    # `_env_file=None` disables dotenv, NOT os.environ, so a developer with
    # AVENUE_MCP_INSTITUTION exported in their shell would otherwise run the
    # whole suite against the wrong profile -- silently, apart from the URL
    # assertions in test_live_findings.py.
    monkeypatch.setenv("AVENUE_MCP_INSTITUTION", "mcmaster")
    for var in ("AVENUE_MCP_BASE_URL", "AVENUE_MCP_LOGIN_URL", "AVENUE_MCP_TIMEZONE"):
        monkeypatch.delenv(var, raising=False)

    from avenue_mcp.config import get_settings

    get_settings.cache_clear()

    import avenue_mcp.context as context

    context.set_context(None)
    yield
    get_settings.cache_clear()
    context.set_context(None)
