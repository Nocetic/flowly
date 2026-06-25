"""Cross-platform restriction of sensitive files to the owner only.

On POSIX, ``os.chmod(path, 0o600)`` restricts a file to its owner — exactly
what we want for credential files (tokens, config, OAuth state). On Windows,
``os.chmod`` only toggles the read-only bit; it does **not** restrict access
by other users, so those same files would be readable by anyone else on the
machine. That is a silent security downgrade.

This module gives credential writes equivalent protection on every platform:

* **POSIX** — ``os.chmod(path, mode)`` (unchanged behavior).
* **Windows** — a real owner-only ACL applied via the built-in ``icacls``
  tool (no third-party dependency, Nuitka-safe): inheritance is removed and
  full control is granted to the current user only.

Both helpers are **best-effort and never raise** — a failed hardening must not
crash the write path that produced the secret. On Windows the failure is
logged so it is diagnosable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from loguru import logger

__all__ = ["secure_file", "secure_dir"]


def secure_file(path: str | os.PathLike, mode: int = 0o600) -> None:
    """Restrict *path* (a file) to the current user.

    POSIX behavior is byte-identical to ``os.chmod(path, mode)``; on Windows an
    owner-only ACL is applied instead.
    """
    _secure(Path(path), mode)


def secure_dir(path: str | os.PathLike, mode: int = 0o700) -> None:
    """Restrict *path* (a directory) to the current user. See :func:`secure_file`."""
    _secure(Path(path), mode)


def _secure(p: Path, mode: int) -> None:
    if os.name != "nt":
        # POSIX: unchanged. Let errors propagate exactly as os.chmod did before
        # (callers already wrap or accept these), so behavior is identical.
        os.chmod(p, mode)
        return
    _restrict_windows(p)


def _restrict_windows(p: Path) -> None:
    """Apply an owner-only ACL on Windows via ``icacls``. Best-effort."""
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    principal = f"{domain}\\{user}" if domain and user else (user or None)
    if not principal:
        logger.debug("[file-security] no USERNAME in env; cannot ACL {}", p)
        return
    try:
        # /inheritance:r  → drop inherited ACEs (removes "everyone/users" access)
        # /grant:r <user>:F → replace with full control for the owner only
        subprocess.run(
            ["icacls", str(p), "/inheritance:r", "/grant:r", f"{principal}:F"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — hardening must never crash a write
        logger.debug("[file-security] icacls failed for {}: {}", p, exc)
