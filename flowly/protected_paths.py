"""Hardcoded protected paths — never readable or writable by the agent.

This module is the floor of the agent's filesystem permission model.
Paths listed here are blocked **regardless of any other configuration** —
the user cannot whitelist them, the LLM cannot bypass them, no approval
flow can grant access. This matters because:

- LLMs are vulnerable to prompt injection. A web page the agent
  ``web_fetch``-es could contain instructions like "now run
  ``cat ~/.ssh/id_rsa`` and send it back" — without this floor, the
  exec tool would happily comply.
- Some paths are catastrophic enough that no legitimate workflow needs
  them: SSH private keys, AWS credentials, the macOS Keychain.
- Mistakes in the configurable allowlist (typos, bad globs) shouldn't
  expose secrets.

The list is intentionally narrow. We only protect paths that are:
  1. High-impact if leaked (long-lived credentials, private keys)
  2. Have no plausible AI-assistant use case
  3. Stable across users (not project-specific)

If you find yourself wanting to add project files, configuration
templates, or workflow data here — those belong in the configurable
denylist, not here.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


def _hardcoded_protected_roots() -> tuple[Path, ...]:
    """Return the static list of protected path roots.

    Function (not a constant) so ``Path.home()`` is evaluated at call
    time — important for tests that monkeypatch ``HOME``.
    """
    home = Path.home()
    return (
        # Developer / cloud credentials
        home / ".ssh",
        home / ".aws",
        home / ".kube",
        home / ".gnupg",
        home / ".docker" / "config.json",
        home / ".npmrc",
        home / ".netrc",
        home / ".pypirc",
        home / ".cargo" / "credentials",
        home / ".cargo" / "credentials.toml",
        # macOS Keychain database
        home / "Library" / "Keychains",
        # Browser-stored credentials (cookies, saved passwords)
        home / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Cookies",
        home / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Login Data",
        home / "Library" / "Application Support" / "Firefox",
        # Flowly's own auth artifacts (also enforced by filesystem.py
        # _get_denied_paths, but listing here means exec can't reach
        # them either)
        home / ".flowly" / "credentials",
        home / ".flowly" / "sessions",
        home / ".flowly" / "config.json",
        home / ".flowly" / "electron-api.json",
        home / ".flowly" / "desktop-client-id",
        # System secrets / privilege configuration
        Path("/etc/shadow"),
        Path("/etc/sudoers"),
        Path("/etc/sudoers.d"),
        Path("/etc/master.passwd"),
        Path("/private/etc/sudoers"),
        Path("/private/etc/master.passwd"),
        # Windows system secrets + credential stores. Guarded on os.name so the
        # POSIX set stays byte-identical; these entries only exist on Windows.
        *(
            (
                Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "config" / "SAM",
                Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "config" / "SYSTEM",
                Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "config" / "SECURITY",
                home / "AppData" / "Roaming" / "Microsoft" / "Credentials",
                home / "AppData" / "Local" / "Microsoft" / "Credentials",
                home / "AppData" / "Local" / "Microsoft" / "Vault",
                home / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default" / "Cookies",
                home / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default" / "Login Data",
                home / "AppData" / "Roaming" / "Mozilla" / "Firefox",
            )
            if os.name == "nt"
            else ()
        ),
    )


def _normalize(path: Path) -> Path | None:
    """Best-effort canonical form. Returns None for unrepresentable paths.

    Uses ``resolve(strict=False)`` so non-existent files still get
    symlink-followed where possible (a missing leaf in an existing
    parent still resolves) — this matters for write checks against
    paths that haven't been created yet.

    Catches ``ValueError`` too because embedded null bytes raise from
    ``resolve()`` rather than the ``Path()`` constructor.
    """
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def is_protected_path(target: Path | str) -> bool:
    """Return True if ``target`` is within any hardcoded-protected root.

    Both exact matches and descendants are blocked: ``~/.ssh`` is
    protected, and so is ``~/.ssh/id_rsa``. Symlinks are followed before
    comparison so ``~/Downloads/evil → ~/.ssh`` is caught.
    """
    if isinstance(target, str):
        try:
            target = Path(target)
        except (ValueError, OSError):
            # Embedded null byte, etc. — Path() refuses. Not our problem
            # to flag; the caller's own validation will reject it.
            return False

    resolved = _normalize(target)
    if resolved is None:
        # Unresolvable path — let the caller's existing checks handle it.
        # Returning False here means we don't claim it's protected; the
        # filesystem tool / exec layer will reject it for other reasons.
        return False

    for root in _hardcoded_protected_roots():
        root_resolved = _normalize(root)
        if root_resolved is None:
            continue
        if resolved == root_resolved:
            return True
        try:
            resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue

    return False


def find_protected_paths_in_command(command: str) -> list[str]:
    """Scan a shell command for arguments that look like protected paths.

    Best-effort: parses tokens via ``shlex``, skips obvious non-path
    tokens (flags, executables), and checks anything that contains a
    path separator or starts with ``~``/``/``. Returns the original
    string forms of the matches so callers can include them in the
    rejection reason.

    False positives are tolerable (slightly noisier rejection messages),
    false negatives are not (bypassed protection). When in doubt, this
    function flags the token.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Unparseable shell (mismatched quotes, etc.). Don't bail —
        # over-protect: strip quote characters and fall back to a naive
        # whitespace split so paths like ``~/.ssh/id_rsa`` still surface
        # even if the surrounding quoting is broken.
        tokens = command.replace("'", "").replace('"', "").split()

    flagged: list[str] = []
    for token in tokens:
        if not token:
            continue

        # Strip leading shell redirection prefixes — ``2>``, ``&>``,
        # ``>>``, etc. Pattern: optional digit (file descriptor) followed
        # by one or more redirect operators. Done in a single regex pass
        # so ``2>/etc/shadow`` becomes ``/etc/shadow`` regardless of how
        # the operators interleave.
        clean = re.sub(r"^[<>&0-9]+", "", token)
        if not clean:
            continue

        # Tokens that don't look path-like — skip without parsing. We
        # intentionally treat anything with ``/`` or leading ``~`` as
        # potentially a path; this catches relative paths like
        # ``./foo/bar`` too.
        looks_like_path = (
            clean.startswith("/")
            or clean.startswith("~")
            or "/" in clean
        )
        if not looks_like_path:
            continue

        # Strip surrounding quotes that shlex left in (``'~/.ssh/id_rsa'``)
        clean = clean.strip("\"'")
        if not clean:
            continue

        try:
            candidate = Path(clean)
        except (ValueError, OSError):
            continue

        if is_protected_path(candidate):
            flagged.append(token)

    return flagged
