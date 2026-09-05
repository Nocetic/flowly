"""Bounded local media reads without following replaced path components."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_media_file(source: str | Path, roots: tuple[Path, ...], limit: int) -> bytes:
    """Resolve allowed symlinks once, then walk that exact path without links.

    Every directory is opened relative to its already-open parent. A client
    replacing a directory with a symlink between validation and read therefore
    cannot redirect the open outside the authorized roots. Platforms without
    no-follow directory descriptors fail closed; image data URLs still work.
    """
    path = Path(source).expanduser().resolve(strict=True)
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("Media path is outside the allowed directories")
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("Secure local media reads are unavailable on this platform")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor,
        )
        with os.fdopen(file_descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("Media must be a regular file within the size limit")
            data = handle.read(limit + 1)
            if len(data) > limit:
                raise ValueError("Media exceeds the size limit")
            return data
    finally:
        os.close(descriptor)
