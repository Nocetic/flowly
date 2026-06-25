"""Generated-media retention — age + size based pruning.

Generation tools (``image_generate`` and, later, video/speech) drop files under
``<flowly home>/media`` so the delivery path can serve full-resolution media via
``/api/media``. Nothing deleted them: the disk-cleanup plugin deliberately
*protects* ``~/.flowly/media``, so on an always-on bot that generates images the
folder grows without bound until the disk fills.

This runs once at gateway start, mirroring :mod:`flowly.audit.retention`:

  1. Age cap: any file older than ``retention_days`` is deleted. The default
     (30 days) is generous enough that a recently generated image stays
     re-fetchable from chat history, while old ones are reclaimed.
  2. Size cap: if the folder is still larger than ``max_size_mb``, the oldest
     remaining files are deleted until the total is under cap.

Both caps are best-effort and never raise — pruning must not block startup. A
value of ``-1`` (age) or ``0`` (size) disables that cap. Only regular files
directly inside the media dir are touched (the folder is owned by the
generation layer); subdirectories are left alone.
"""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger as _logger

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_SIZE_MB = 500


def _media_files(media_dir: Path) -> list[Path]:
    """Return the regular files directly in ``media_dir``, oldest first."""
    if not media_dir.exists() or not media_dir.is_dir():
        return []
    files = [p for p in media_dir.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def _safe_unlink(path: Path) -> int:
    """Delete a file, returning its size if successful (0 otherwise)."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    try:
        path.unlink()
    except OSError as exc:
        _logger.debug("[Media] retention: unlink failed for {}: {}", path, exc)
        return 0
    return size


def prune_media(
    media_dir: Path,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_size_mb: int = DEFAULT_MAX_SIZE_MB,
) -> dict:
    """Trim old / oversized generated-media files.

    Returns a small summary dict. Never raises — the caller can rely on it
    completing during gateway startup without try/except.
    """
    summary = {
        "deleted_files": 0,
        "deleted_bytes": 0,
        "remaining_files": 0,
        "remaining_bytes": 0,
        "skipped": False,
    }

    try:
        files = _media_files(media_dir)
    except OSError as exc:
        _logger.debug("[Media] retention: list failed for {}: {}", media_dir, exc)
        summary["skipped"] = True
        return summary

    if not files:
        return summary

    survivors: list[Path] = list(files)

    # ── 1. Age cap ─────────────────────────────────────────────────────
    if retention_days >= 0:
        cutoff = time.time() - (retention_days * 86_400)
        kept: list[Path] = []
        for f in survivors:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                kept.append(f)
                continue
            if mtime < cutoff:
                bytes_freed = _safe_unlink(f)
                if bytes_freed:
                    summary["deleted_files"] += 1
                    summary["deleted_bytes"] += bytes_freed
            else:
                kept.append(f)
        survivors = kept

    # ── 2. Size cap ────────────────────────────────────────────────────
    if max_size_mb > 0:
        max_bytes = max_size_mb * 1024 * 1024
        sizes: dict[Path, int] = {}
        total = 0
        for f in survivors:
            try:
                s = f.stat().st_size
            except OSError:
                s = 0
            sizes[f] = s
            total += s

        idx = 0
        while total > max_bytes and idx < len(survivors):
            f = survivors[idx]
            bytes_freed = _safe_unlink(f)
            if bytes_freed:
                summary["deleted_files"] += 1
                summary["deleted_bytes"] += bytes_freed
                total -= sizes.get(f, 0)
            idx += 1
        survivors = survivors[idx:]

    # ── 3. Tally what's left ───────────────────────────────────────────
    remaining_bytes = 0
    for f in survivors:
        try:
            remaining_bytes += f.stat().st_size
        except OSError:
            continue
    summary["remaining_files"] = len(survivors)
    summary["remaining_bytes"] = remaining_bytes

    if summary["deleted_files"]:
        _logger.info(
            "[Media] retention: pruned {} file(s), {:.1f} MB freed; "
            "{} file(s), {:.1f} MB remaining",
            summary["deleted_files"],
            summary["deleted_bytes"] / 1_048_576,
            summary["remaining_files"],
            summary["remaining_bytes"] / 1_048_576,
        )
    return summary
