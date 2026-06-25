"""Spill oversized tool results to a temp file instead of losing them.

When a tool result exceeds its per-tool char cap (``_TOOL_MAX_CHARS`` in
``flowly.agent.loop``), the in-context text is truncated — historically the
overflow was simply gone. Long ``codex_session`` turns lost most of their
detail this way: the turn survived (compaction), but the model could never
answer "what exactly did codex change?" afterwards.

This module implements the fix: before truncating, write the
FULL output to a temp file and append a pointer to the truncated text so the
model can read the rest back with ``read_file`` (offset/limit). The spill
directory lives under the OS temp dir — the OS reclaims it eventually, and
:func:`_cleanup_old_spills` opportunistically removes files older than
``RETENTION_DAYS`` so long-running gateways don't accumulate junk.

The spill dir is allow-listed in ``flowly.agent.tools.filesystem`` so
``read_file`` can access it; everything else under the temp dir stays
off-limits to the agent.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

# Marker shared with CompactionService.microcompact — a truncated tool result
# containing this marker keeps its pointer line when microcompact shortens it
# further (the pointer sits at the END of the result, exactly where naive
# truncation would cut).
SPILL_POINTER_MARKER = "FULL output saved to: "

RETENTION_DAYS = 7

# Rate-limit the retention sweep to once per hour per process.
_CLEANUP_INTERVAL_S = 3600.0
_last_cleanup: float = 0.0


def get_spill_dir() -> Path:
    """Spill directory under the OS temp dir (created lazily on write)."""
    return Path(tempfile.gettempdir()) / "flowly-tool-results"


def spill_tool_result(content: str, tool_name: str) -> Path | None:
    """Write the full tool result to the spill dir. Returns None on failure.

    Failure is non-fatal by design — the caller falls back to plain
    truncation, which is the pre-spill behaviour.
    """
    try:
        spill_dir = get_spill_dir()
        spill_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_old_spills(spill_dir)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        digest = hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()[:8]
        path = spill_dir / f"{tool_name}-{stamp}-{digest}.txt"
        path.write_text(content, encoding="utf-8")
        logger.info(
            f"[spill] persisted oversized {tool_name} result: "
            f"{len(content)} chars -> {path}"
        )
        return path
    except Exception as exc:
        logger.warning(f"[spill] could not persist {tool_name} result: {exc}")
        return None


def build_spill_pointer(path: Path, total_chars: int, total_lines: int) -> str:
    """Footer appended to the truncated in-context text.

    Single bracketed line so :func:`extract_spill_pointer` can recover it
    verbatim after further truncation passes.
    """
    return (
        f"\n[... truncated — output was {total_chars} chars / {total_lines} lines. "
        f"{SPILL_POINTER_MARKER}{path} — "
        f"read the rest with read_file(path, offset=<start line>, limit=<line count>).]"
    )


def extract_spill_pointer(content: str) -> str | None:
    """Recover the spill pointer line from a tool result, if present.

    Used by ``CompactionService.microcompact``: old tool results get cut to
    a few hundred chars, which would drop a pointer sitting at the end of
    the text. The pointer is re-appended after truncation so the file
    reference survives every compaction pass.
    """
    pos = content.rfind(SPILL_POINTER_MARKER)
    if pos == -1:
        return None
    start = content.rfind("\n[", 0, pos)
    if start == -1:
        return None
    end = content.find("]", pos)
    if end == -1:
        return None
    return content[start : end + 1]


def _cleanup_old_spills(spill_dir: Path) -> None:
    """Delete spill files older than ``RETENTION_DAYS``. Best-effort."""
    global _last_cleanup
    now = time.monotonic()
    if now - _last_cleanup < _CLEANUP_INTERVAL_S:
        return
    _last_cleanup = now
    cutoff = time.time() - RETENTION_DAYS * 86400
    try:
        for entry in spill_dir.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass


__all__ = [
    "SPILL_POINTER_MARKER",
    "build_spill_pointer",
    "extract_spill_pointer",
    "get_spill_dir",
    "spill_tool_result",
]
