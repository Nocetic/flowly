"""A conversation's last words, as its row in a list shows them.

Kept in the session's header (``metadata["last_message"]``) each time it is
saved, so a list (``sessions.list``) reads it with the header alone. What
counts is what the person sees: a user's or the agent's words, never a note
the app added (``_display_hidden``), a compaction summary, a context
boundary, or an agent's turn that only called tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

PREVIEW_CHARS = 160
# How much of a session file's end a list reads for one written before the
# header kept its last words.
TAIL_BYTES = 64 * 1024


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
    return ""


def last_message_preview(messages: Iterable[dict[str, Any]]) -> dict[str, str] | None:
    """``{"role", "text"}`` of the last message a person sees, or None."""
    from flowly.session.archive import ARCHIVE_SUMMARY_KEY

    for message in reversed(list(messages)):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        if message.get("_display_hidden") or message.get(ARCHIVE_SUMMARY_KEY) or message.get("_type"):
            continue
        text = " ".join(_text(message.get("content")).split())
        if not text:
            continue  # an agent's step that only called tools, an empty row
        return {"role": message["role"], "text": text[:PREVIEW_CHARS]}
    return None


def tail_preview(path: Path) -> dict[str, str] | None:
    """The last words of a session file whose header does not keep them, from its end only."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            chunk = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    rows = []
    for line in chunk.splitlines()[1 if size > TAIL_BYTES else 0:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return last_message_preview(rows)
