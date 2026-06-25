"""Audit log reader — query JSONL records for the Activity tab.

The logger writes one JSON object per line into daily files at
``~/.flowly/audit/YYYY-MM-DD.jsonl``. This module reads them back with
filtering, search and pagination so the Desktop UI can browse history
without having to ingest the whole folder client-side.

Reads are best-effort: malformed lines are skipped, missing files yield
empty results. The reader never mutates the audit log.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from loguru import logger as _logger


_AUDIT_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


# ── Discovery ────────────────────────────────────────────────────────────


def _list_audit_files(audit_dir: Path) -> list[Path]:
    """Return audit files newest-first (matches "most recent first" reads)."""
    if not audit_dir.exists() or not audit_dir.is_dir():
        return []
    try:
        files = [
            p for p in audit_dir.iterdir()
            if p.is_file() and _AUDIT_FILE_RE.match(p.name)
        ]
    except OSError:
        return []
    files.sort(key=lambda p: p.name, reverse=True)
    return files


def _file_for_date(audit_dir: Path, date: str) -> Path | None:
    """Return the file matching ``YYYY-MM-DD`` if it exists."""
    if not _AUDIT_FILE_RE.match(f"{date}.jsonl"):
        return None
    candidate = audit_dir / f"{date}.jsonl"
    return candidate if candidate.is_file() else None


# ── Stats ────────────────────────────────────────────────────────────────


def get_stats(audit_dir: Path) -> dict[str, Any]:
    """Folder-level stats for the UI footer."""
    files = _list_audit_files(audit_dir)
    if not files:
        return {
            "files": 0,
            "total_bytes": 0,
            "oldest_date": None,
            "newest_date": None,
        }

    total_bytes = 0
    for f in files:
        try:
            total_bytes += f.stat().st_size
        except OSError:
            continue

    # Files are newest-first (sorted by name desc).
    newest = _AUDIT_FILE_RE.match(files[0].name).group(1)
    oldest = _AUDIT_FILE_RE.match(files[-1].name).group(1)

    return {
        "files": len(files),
        "total_bytes": total_bytes,
        "oldest_date": oldest,
        "newest_date": newest,
    }


# ── Iteration ────────────────────────────────────────────────────────────


def _iter_file_reverse(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from a single file, newest line first.

    JSONL is append-only so the bottom of the file is the most recent
    event. We read the whole file and reverse — daily files are small
    (< few MB) so this is simpler than a tail-seek implementation, and
    the reader stays robust against partial writes.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.debug("[Audit] reader: open failed for {}: {}", path, exc)
        return

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # Skip malformed lines — could be a half-written line during
            # a crash. Don't fail the whole listing because of one row.
            continue


def _iter_entries(audit_dir: Path, date: str | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(date, entry)`` tuples newest-first across all files (or one)."""
    if date:
        f = _file_for_date(audit_dir, date)
        if f is None:
            return
        for entry in _iter_file_reverse(f):
            yield date, entry
        return

    for f in _list_audit_files(audit_dir):
        m = _AUDIT_FILE_RE.match(f.name)
        if not m:
            continue
        d = m.group(1)
        for entry in _iter_file_reverse(f):
            yield d, entry


# ── Filtering ────────────────────────────────────────────────────────────


def _matches(entry: dict[str, Any], tool: str | None, status: str | None, search: str | None) -> bool:
    """Apply UI filters to a single entry."""
    if tool:
        # Match against either 'tool' (tool_call) or 'type' (other event types).
        e_tool = entry.get("tool")
        e_type = entry.get("type")
        if e_tool != tool and e_type != tool:
            return False

    if status:
        # Tool calls have ``success: bool``; map to "success"/"error".
        if "success" in entry:
            entry_status = "success" if entry["success"] else "error"
        else:
            entry_status = entry.get("status", "")
        if entry_status != status:
            return False

    if search:
        needle = search.lower()
        # Search across the JSON-encoded form so nested args / result
        # snippets are reachable without enumerating fields.
        try:
            haystack = json.dumps(entry, ensure_ascii=False).lower()
        except (TypeError, ValueError):
            haystack = str(entry).lower()
        if needle not in haystack:
            return False

    return True


# ── Public API ───────────────────────────────────────────────────────────


def read_entries(
    audit_dir: Path,
    *,
    date: str | None = None,
    tool: str | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Page through audit entries newest-first.

    Returns ``{"entries": [...], "total": N, "has_more": bool, "next_offset": int}``.

    ``date`` restricts to a single ``YYYY-MM-DD`` file (much faster than
    scanning the whole folder). When omitted, all files are scanned.

    ``total`` reflects post-filter matches across the scanned range, not
    raw line count. The UI uses it to render the footer ("142 events").
    """
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500
    if offset < 0:
        offset = 0

    entries: list[dict[str, Any]] = []
    total = 0

    for d, entry in _iter_entries(audit_dir, date=date):
        if not _matches(entry, tool=tool, status=status, search=search):
            continue
        total += 1
        if total <= offset:
            continue
        if len(entries) < limit:
            # Stamp the source file's date so the UI can group / link
            # without re-deriving it from the timestamp.
            enriched = {**entry, "_date": d}
            entries.append(enriched)
        # Keep counting past limit so ``total`` is accurate; bail out
        # once we know "has_more" status.
        if total > offset + limit + 1000:
            # Sanity cap to avoid scanning a huge folder when the user
            # is paginating: break after a generous lookahead.
            break

    has_more = total > offset + len(entries)
    return {
        "entries": entries,
        "total": total,
        "has_more": has_more,
        "next_offset": offset + len(entries) if has_more else None,
    }


def clear_audit_logs(audit_dir: Path) -> dict[str, Any]:
    """Delete every audit jsonl file. Returns counts.

    Wired to the "Clear all history" button in the UI; the desktop side
    is responsible for the confirmation dialog.
    """
    deleted_files = 0
    deleted_bytes = 0
    for f in _list_audit_files(audit_dir):
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        try:
            f.unlink()
        except OSError as exc:
            _logger.debug("[Audit] reader: clear unlink failed for {}: {}", f, exc)
            continue
        deleted_files += 1
        deleted_bytes += size

    return {"deleted_files": deleted_files, "deleted_bytes": deleted_bytes}
