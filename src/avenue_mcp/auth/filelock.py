"""Owner-only file permissions on Windows.

`os.open(..., 0o600)` and `os.chmod(path, 0o600)` are the right answer on
POSIX, and both are **silent no-ops on Windows** — NTFS uses ACLs, and
CPython's chmod there only toggles the read-only attribute. The session file
would keep whatever the parent directory grants (typically SYSTEM,
Administrators, and the user) while the code reported it protected.

Since session.json is equivalent to a logged-in Brightspace session, "we called
chmod" is not an acceptable answer on this platform. On POSIX these functions
are inert — auth/login.py already sets the mode correctly there.
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


def _principal() -> str:
    """The account to grant. Domain-qualified when that's available."""
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def restrict_to_owner(path: Path) -> bool:
    """Break ACL inheritance and grant the current user alone.

    Returns True if applied, False if not — never raises. A failure here must
    not take down a login that otherwise succeeded, but it must not pass
    silently either, so the caller logs a warning.
    """
    if not IS_WINDOWS or not path.exists():
        return False

    try:
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",  # drop inherited ACEs first
                "/grant:r",  # replace rather than append
                f"{_principal()}:(R,W)",
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
    """Current permissions, so the protection can be verified not assumed.

    A protection that silently doesn't apply is worse than a known-absent one,
    because it stops anyone from looking.
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
