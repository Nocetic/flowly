"""Locate bash on Windows for Mac-parity shell semantics.

Background
──────────
On macOS and Linux the agent's shell commands run under `/bin/sh`, where
`ls ~/Desktop`, `$HOME`, pipes, and globbing all behave consistently.
PowerShell on Windows has subtle differences — alias semantics, path
resolution with `~`, OneDrive redirection, encoding — that produce
spurious regressions the agent cannot anticipate. The most visible one:
`ls ~/Desktop` sometimes returns empty output where the Mac equivalent
correctly lists files.

The standard solution is to require bash on every platform (Windows
users install Git for Windows, which ships Git Bash at a standard
path). Flowly goes further: flowly-desktop's Windows installer bundles
a portable MinGit (≈35 MB) and injects its bash path via the
`FLOWLY_BASH_PATH` environment variable, so end-users get Mac-parity
shell semantics out of the box without any manual install.

Resolution order (Windows only)
───────────────────────────────
  1. `FLOWLY_BASH_PATH` — set by flowly-desktop for bundled MinGit. Highest
     priority so the version we ship always wins.
  2. Standard Git for Windows install paths (Program Files, LOCALAPPDATA).
     Catches dev machines and users who already installed Git manually.
  3. PATH lookup via `shutil.which("bash")`. Last resort.

Returns None if nothing is found. The caller decides whether to fall back
to PowerShell or hard-error.

Non-Windows platforms
─────────────────────
Returns None unconditionally and touches neither the filesystem nor the
environment. This is the Mac regression anchor — tests enforce it.
"""

from __future__ import annotations

import ntpath
import os
import shutil
import sys

from loguru import logger

_BASH_CACHE: str | None = None
_BASH_SEARCHED: bool = False


def find_bash() -> str | None:
    """Locate bash on Windows. Cached after first call.

    Returns the absolute path to bash(.exe), or None when nothing is found
    or we are not on Windows.
    """
    global _BASH_CACHE, _BASH_SEARCHED

    if _BASH_SEARCHED:
        return _BASH_CACHE

    _BASH_SEARCHED = True

    if sys.platform != "win32":
        _BASH_CACHE = None
        return None

    # 1. Env var injected by flowly-desktop for the bundled MinGit.
    env_path = os.environ.get("FLOWLY_BASH_PATH")
    if env_path and os.path.isfile(env_path):
        logger.info(f"[bash_finder] Using bundled bash: {env_path}")
        _BASH_CACHE = env_path
        return env_path

    # 2. Standard Git for Windows install paths.
    # Note: we read LOCALAPPDATA directly rather than using
    # `os.path.expandvars("%LOCALAPPDATA%")` because POSIX expandvars only
    # understands `$VAR` syntax. This keeps the Windows branch testable
    # from Mac via `monkeypatch.setattr(sys.platform, "win32")`.
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        # Use ntpath.join explicitly to force backslash separators even when
        # the test suite runs this branch on Mac (posixpath.join would emit
        # forward slashes and miss the bash binary at runtime).
        candidates.append(ntpath.join(local_appdata, "Programs", "Git", "bin", "bash.exe"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            logger.info(f"[bash_finder] Using system Git Bash: {candidate}")
            _BASH_CACHE = candidate
            return candidate

    # 3. PATH lookup — catches custom installs that are on PATH.
    which_result = shutil.which("bash")
    if which_result:
        logger.info(f"[bash_finder] Using bash from PATH: {which_result}")
        _BASH_CACHE = which_result
        return which_result

    logger.warning(
        "[bash_finder] No bash found on Windows. The executor will fall back "
        "to PowerShell, which has subtle divergences from Mac semantics. "
        "Install Git for Windows (https://git-scm.com/download/win) or update "
        "Flowly Desktop to a release that bundles MinGit."
    )
    _BASH_CACHE = None
    return None


def reset_bash_cache() -> None:
    """Clear the resolution cache. For unit testing only."""
    global _BASH_CACHE, _BASH_SEARCHED
    _BASH_CACHE = None
    _BASH_SEARCHED = False
