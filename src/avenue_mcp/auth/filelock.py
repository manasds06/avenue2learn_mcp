"""Owner-only file permissions, per platform (docs/01-authentication.md).

`os.chmod(path, 0o600)` is the obvious implementation and it is a **silent
no-op on Windows** — NTFS uses ACLs, and CPython's chmod there only toggles the
read-only attribute. Since session.json is equivalent to a logged-in Avenue
session, "we called chmod" is not an acceptable answer on this platform: the
file would inherit whatever the parent directory grants while the code claimed
otherwise.

So: chmod on POSIX, icacls on Windows, and a verifier so the Phase 1 exit
criterion can be checked rather than assumed.
"""

from __future__ import annotations

import getpass
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


def _current_windows_principal() -> str:
    """The account to grant. Prefer the domain-qualified form when available."""
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def restrict_to_owner(path: Path) -> bool:
    """Make `path` readable and writable by its owner alone.

    Returns True if the restriction was applied. Returns False (with a warning)
    rather than raising if it couldn't be — a failure here should not take down
    a login that otherwise succeeded, but it must not pass silently either.
    """
    if not path.exists():
        return False

    if not IS_WINDOWS:
        try:
            os.chmod(path, 0o600)
            return True
        except OSError as exc:
            log.warning("Could not chmod %s to 0600: %s", path, exc)
            return False

    principal = _current_windows_principal()
    try:
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",          # drop inherited ACEs first
                "/grant:r",                # replace, don't append
                f"{principal}:(R,W)",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not run icacls on %s: %s", path, exc)
        return False

    if result.returncode != 0:
        log.warning(
            "icacls failed on %s (exit %d): %s",
            path,
            result.returncode,
            (result.stderr or result.stdout).strip(),
        )
        return False

    return True


def describe_permissions(path: Path) -> str:
    """Human-readable current permissions, for verification and probe output.

    This exists so the Phase 1 exit criterion is checkable. A protection that
    silently doesn't apply is worse than a known-absent one, because it stops
    anyone from looking.
    """
    if not path.exists():
        return "file does not exist"

    if not IS_WINDOWS:
        return f"mode {oct(path.stat().st_mode & 0o777)}"

    try:
        result = subprocess.run(
            ["icacls", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return (result.stdout or result.stderr).strip() or "icacls returned nothing"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not read ACL: {exc}"


def write_private(path: Path, data: str) -> None:
    """Write text to `path`, then restrict it. Creates parent dirs as needed.

    Order matters on POSIX: the file exists briefly with default permissions.
    Narrow that window by creating it with a restrictive mode up front.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if IS_WINDOWS:
        path.write_text(data, encoding="utf-8")
    else:
        # O_CREAT with 0600 avoids the brief world-readable window that
        # write_text-then-chmod leaves open.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)

    if not restrict_to_owner(path):
        log.warning(
            "Session state at %s could not be restricted to your account. "
            "Treat it as a credential and check its permissions manually.",
            path,
        )
