"""The session file is a live credential. Prove it cannot leak at rest.

docs/06 Phase 4 exit criterion: "`git status` is clean after a full login + sync
cycle -- no stray state files", singled out in that doc as **the most plausible
way this project leaks a credential**. Anyone holding storage_state.json can act
as the user until it expires.

Three distinct properties, each previously unasserted:

1. The file is written owner-only, and *stays* owner-only across a re-login.
2. State lives outside any repository by default.
3. .gitignore genuinely matches the credential paths -- tested by asking git,
   not by reading the file and hoping.

For (3) the tests build a throwaway repo in tmp_path and copy the real
.gitignore into it. Asserting against the actual working tree would depend on
whatever the developer happens to have lying around, and creating credential
files inside the real repo to check they're ignored is a bad trade for a test.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Paths that carry a credential, course material, or real org unit ids. Each must
# be ignored by the shipped .gitignore.
MUST_BE_IGNORED = (
    ".avenue-mcp/session.json",
    ".avenue-mcp/carleton/session.json",
    "session.json",
    "storage_state.json",
    "index.db",
    "index.db-wal",
    "cache/lecture-notes.pdf",
    "probe.json",
    "probe-carleton.json",
    "tests/fixtures/raw-response.json",
)

# Proves the ignore check discriminates. Without these, a .gitignore of "*"
# would pass every assertion above.
MUST_NOT_BE_IGNORED = ("README.md", "src/avenue_mcp/server.py", "notes.md")


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _git_available() -> bool:
    if shutil.which("git") is None:
        return False
    return _git("--version", cwd=REPO_ROOT).returncode == 0


requires_git = pytest.mark.skipif(
    not _git_available(), reason="git is not available on PATH"
)


@pytest.fixture
def throwaway_repo(tmp_path: Path) -> Path:
    """A real git repo carrying this project's actual .gitignore."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    # Identity, so `git status` works on a machine with no global git config.
    _git("config", "user.email", "test@example.ca", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    shutil.copy(REPO_ROOT / ".gitignore", repo / ".gitignore")
    return repo


class TestSessionFilePermissions:
    """Owner-only, and owner-only again after a re-login overwrites it."""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX mode bits are meaningless on Windows; the ACL path is "
        "covered by test_merge_fixes.py::test_windows_acl_drops_inherited_entries",
    )
    def test_written_owner_only(self, tmp_path):
        from avenue_mcp.auth.login import _persist

        path = tmp_path / "storage_state.json"
        _persist({"cookies": [{"name": "d2lSessionVal", "value": "x"}]}, path)

        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_relogin_tightens_a_loose_existing_file(self, tmp_path):
        """The interesting case: the file already exists, world-readable.

        `_persist` writes to a temp file and renames over the target, so the
        replacement's mode is what survives -- not the old file's. If that ever
        regresses to writing in place at the umask, this catches it.
        """
        from avenue_mcp.auth.login import _persist

        path = tmp_path / "storage_state.json"
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o644)

        _persist({"cookies": [{"name": "d2lSessionVal", "value": "x"}]}, path)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_no_temp_file_is_left_behind(self, tmp_path):
        """A leftover .tmp would hold the same cookies at an unchecked mode."""
        from avenue_mcp.auth.login import _persist

        path = tmp_path / "storage_state.json"
        _persist({"cookies": [{"name": "d2lSessionVal", "value": "x"}]}, path)

        leftovers = [p.name for p in tmp_path.iterdir() if p.name != path.name]
        assert not leftovers, f"unexpected files alongside the session: {leftovers}"


class TestStateLivesOutsideTheRepo:
    def test_default_state_dir_is_in_the_home_directory(self, monkeypatch):
        """conftest points state_dir at tmp_path, so unset it to see the default."""
        monkeypatch.delenv("AVENUE_MCP_STATE_DIR", raising=False)
        from avenue_mcp.config import Settings

        assert Settings(_env_file=None).state_dir == Path.home() / ".avenue-mcp"

    def test_default_state_dir_is_not_inside_this_repo(self, monkeypatch):
        monkeypatch.delenv("AVENUE_MCP_STATE_DIR", raising=False)
        from avenue_mcp.config import Settings

        state_dir = Settings(_env_file=None).state_dir.resolve()
        assert REPO_ROOT not in state_dir.parents
        assert state_dir != REPO_ROOT

    def test_session_and_index_sit_under_the_institution_dir(self, monkeypatch, tmp_path):
        """Namespaced per school, so two institutions cannot share a session."""
        monkeypatch.setenv("AVENUE_MCP_STATE_DIR", str(tmp_path))
        from avenue_mcp.config import Settings

        settings = Settings(_env_file=None, institution="carleton")
        assert settings.institution_dir == tmp_path / "carleton"
        for path in (settings.session_path, settings.index_path):
            assert settings.institution_dir in path.parents


@requires_git
class TestGitignoreActuallyMatches:
    """Ask git, rather than trusting that a pattern in the file does what we think."""

    @pytest.mark.parametrize("relpath", MUST_BE_IGNORED)
    def test_credential_paths_are_ignored(self, throwaway_repo, relpath):
        target = throwaway_repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret", encoding="utf-8")

        result = _git("check-ignore", "-q", "--", relpath, cwd=throwaway_repo)
        assert result.returncode == 0, f"{relpath} is NOT ignored by .gitignore"

    @pytest.mark.parametrize("relpath", MUST_NOT_BE_IGNORED)
    def test_source_files_are_not_ignored(self, throwaway_repo, relpath):
        target = throwaway_repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("code", encoding="utf-8")

        result = _git("check-ignore", "-q", "--", relpath, cwd=throwaway_repo)
        assert result.returncode != 0, (
            f"{relpath} IS ignored -- .gitignore is too broad, which would also "
            "make the assertions above pass for the wrong reason"
        )

    def test_a_full_login_and_sync_leaves_the_tree_clean(self, throwaway_repo):
        """The docs/06 criterion, end to end.

        Everything a login and a sync write, dropped into a repo at once. If
        `git status` stays empty, no credential can be committed by accident.
        """
        for relpath in MUST_BE_IGNORED:
            target = throwaway_repo / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("secret", encoding="utf-8")

        status = _git("status", "--porcelain", cwd=throwaway_repo)
        assert status.returncode == 0
        # .gitignore itself is the only untracked file we deliberately added.
        listed = [
            line for line in status.stdout.splitlines() if ".gitignore" not in line
        ]
        assert listed == [], f"state files visible to git: {listed}"

    def test_the_real_repo_has_a_gitignore_to_copy(self):
        """Guards the fixture: a missing .gitignore would silently pass everything."""
        assert (REPO_ROOT / ".gitignore").is_file()
