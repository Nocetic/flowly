"""Audit log retention — age + size based pruning.

Runs once at gateway start. Two-tier policy:

  1. Age cap: any ``YYYY-MM-DD.jsonl`` older than ``retention_days`` is
     deleted outright.
  2. Size cap: if the audit folder is still larger than ``max_size_mb``,
     the oldest remaining files are deleted until the total is under cap.

Both caps are best-effort and never raise — auditing is a non-critical
side path; an IO error here must not block startup.

A value of ``-1`` (age) or ``0`` (size) disables that cap.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable

from loguru import logger as _logger


# Files we manage: ``YYYY-MM-DD.jsonl``. Anything else in the folder is
# left alone (e.g. someone's manual export, a future schema, etc.).
_AUDIT_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")


def _audit_files(audit_dir: Path) -> list[Path]:
    """Return the audit jsonl files, oldest first."""
    if not audit_dir.exists() or not audit_dir.is_dir():
        return []
    files = [
        p for p in audit_dir.iterdir()
        if p.is_file() and _AUDIT_FILE_RE.match(p.name)
    ]
    # Sort by mtime ascending so iter pops oldest first.
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
        _logger.debug("[Audit] retention: unlink failed for {}: {}", path, exc)
        return 0
    return size


def prune_audit_logs(
    audit_dir: Path,
    retention_days: int = 90,
    max_size_mb: int = 100,
) -> dict:
    """Trim old / oversized audit files.

    Returns a small summary dict. Never raises — caller can rely on it
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
        files = _audit_files(audit_dir)
    except OSError as exc:
        _logger.debug("[Audit] retention: list failed for {}: {}", audit_dir, exc)
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

        # Pop oldest first until under cap. ``survivors`` is already
        # mtime-ascending from ``_audit_files``.
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
            "[Audit] retention: pruned {} file(s), {:.1f} MB freed; "
            "{} file(s), {:.1f} MB remaining",
            summary["deleted_files"],
            summary["deleted_bytes"] / 1_048_576,
            summary["remaining_files"],
            summary["remaining_bytes"] / 1_048_576,
        )
    return summary
