"""Vault path resolution and safe, sandboxed filesystem access.

Security model
--------------
Vault content is untrusted. Every path that reaches the filesystem is forced
through :func:`safe_resolve`, which:

1. rejects absolute paths and any path containing ``..`` segments,
2. resolves symlinks via ``Path.resolve()`` (realpath) on *both* the vault
   root and the candidate, then
3. requires the resolved candidate to live inside the resolved vault root.

Step 2 is deliberate: a purely lexical containment check (``startswith``) is
insufficient against a symlink that predates the access and points outside the
vault — the lesson from the agentmemory ``obsidian-export`` path-traversal
advisory. Resolving first closes that hole.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator

# Fallback vault location, matching the long-standing Obsidian skill convention.
_DEFAULT_VAULT = "~/Documents/Obsidian Vault"


class VaultError(Exception):
    """Raised when a vault path is unsafe or a note cannot be read."""


class VaultNotConfigured(VaultError):
    """Raised when no usable vault directory can be resolved."""


class VaultPermissionDenied(VaultError):
    """Raised when the vault exists but the OS denies reading it.

    On macOS this is almost always TCC: an iCloud vault under
    ``~/Library/Mobile Documents`` (or ``~/Documents``) that the host
    process lacks Full Disk Access for. Surfacing this distinctly stops it
    masquerading as an empty vault.
    """


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path))).resolve()


def resolve_vault_path(vault_path: str = "") -> Path:
    """Resolve the vault root from explicit config → env → default.

    Returns the resolved (realpath) directory. Raises
    :class:`VaultNotConfigured` if the resolved location is missing or is not
    a directory, so callers get one clear failure mode.
    """
    candidate = (vault_path or "").strip()
    if not candidate:
        candidate = (os.environ.get("OBSIDIAN_VAULT_PATH") or "").strip()
    if not candidate:
        candidate = _DEFAULT_VAULT

    root = _expand(candidate)
    if not root.exists():
        raise VaultNotConfigured(f"vault path does not exist: {root}")
    if not root.is_dir():
        raise VaultNotConfigured(f"vault path is not a directory: {root}")
    # Probe readability: directory metadata can be visible while readdir is
    # denied (macOS TCC on iCloud / Documents). Catch that here so it doesn't
    # look like an empty vault downstream.
    try:
        with os.scandir(root) as it:
            next(it, None)
    except PermissionError as exc:
        raise VaultPermissionDenied(
            f"cannot read vault (permission denied): {root}. On macOS, grant "
            f"Full Disk Access to the app running Flowly (Settings → Privacy & "
            f"Security → Full Disk Access), then restart it."
        ) from exc
    return root


def safe_resolve(vault_root: Path, rel_path: str) -> Path:
    """Resolve *rel_path* (vault-relative) to an absolute path inside the vault.

    Raises :class:`VaultError` on absolute paths, ``..`` traversal, or any path
    that — after symlink resolution — escapes the vault root.
    """
    rel = (rel_path or "").strip()
    if not rel:
        raise VaultError("empty path")
    p = Path(rel)
    if p.is_absolute():
        raise VaultError(f"absolute paths are not allowed: {rel}")
    if any(part == ".." for part in p.parts):
        raise VaultError(f"path traversal ('..') is not allowed: {rel}")

    root = vault_root.resolve()
    resolved = (root / p).resolve()
    # Containment check against the *resolved* root (symlinks already followed).
    if resolved != root and root not in resolved.parents:
        raise VaultError(f"path escapes the vault: {rel}")
    return resolved


@lru_cache(maxsize=256)
def _glob_to_re(pattern: str) -> re.Pattern[str]:
    """Translate a POSIX glob to a regex with recursive ``**`` support.

    ``*`` matches within a path segment, ``**`` (or ``**/``) matches across
    segments, ``?`` matches a single non-separator char. Unlike ``fnmatch``,
    ``**/*.md`` matches root-level notes too.
    """
    i, n = 0, len(pattern)
    out: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")  # **/ → zero or more leading dirs
                    i += 3
                    continue
                out.append(".*")            # ** → anything incl. separators
                i += 2
                continue
            out.append("[^/]*")             # * → within a segment
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches_any(rel_posix: str, globs: list[str]) -> bool:
    return any(_glob_to_re(pat).match(rel_posix) for pat in globs)


def _is_excluded(rel_posix: str, exclude_globs: list[str]) -> bool:
    return _matches_any(rel_posix, exclude_globs)


def _is_included(rel_posix: str, include_globs: list[str]) -> bool:
    if not include_globs:
        return True
    return _matches_any(rel_posix, include_globs)


@dataclass
class Note:
    """A vault note discovered during a walk."""
    rel_path: str          # vault-relative POSIX path, e.g. "People/Ada.md"
    abs_path: Path
    size: int              # bytes
    mtime: float


def iter_notes(
    vault_root: Path,
    *,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    max_note_bytes: int = 1_000_000,
) -> Iterator[Note]:
    """Yield Markdown notes under *vault_root* honouring include/exclude globs.

    Skips excluded paths, non-matching files, and notes larger than
    *max_note_bytes*. Symlinked directories are not followed (os.walk default)
    to avoid escaping the vault or looping.
    """
    include = include_globs or ["**/*.md"]
    exclude = exclude_globs or []
    root = vault_root.resolve()

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place so we don't descend into them.
        rel_dir = Path(dirpath).relative_to(root)
        kept: list[str] = []
        for d in dirnames:
            rel_posix = (rel_dir / d).as_posix()
            if not _is_excluded(rel_posix + "/", exclude) and not _is_excluded(rel_posix, exclude):
                kept.append(d)
        dirnames[:] = kept

        for name in filenames:
            if not name.lower().endswith(".md"):
                continue
            rel_posix = (rel_dir / name).as_posix()
            if _is_excluded(rel_posix, exclude):
                continue
            if not _is_included(rel_posix, include):
                continue
            abs_path = Path(dirpath) / name
            try:
                st = abs_path.stat()
            except OSError:
                continue
            if st.st_size > max_note_bytes:
                continue
            yield Note(rel_path=rel_posix, abs_path=abs_path, size=st.st_size, mtime=st.st_mtime)


def read_note(vault_root: Path, rel_path: str, *, max_note_bytes: int = 1_000_000) -> str:
    """Read a vault note's text after safe path resolution."""
    abs_path = safe_resolve(vault_root, rel_path)
    if not abs_path.exists():
        raise VaultError(f"note not found: {rel_path}")
    if not abs_path.is_file():
        raise VaultError(f"not a file: {rel_path}")
    if abs_path.stat().st_size > max_note_bytes:
        raise VaultError(f"note too large (> {max_note_bytes} bytes): {rel_path}")
    return abs_path.read_text(encoding="utf-8", errors="replace")
