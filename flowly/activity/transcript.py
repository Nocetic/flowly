"""A step's arguments and result, read back from its conversation's transcript.

Activity never stores what a tool was given or what it returned; the
transcript already does, and the chat shows it in its tool panels. A step
names its call (``callId``), and its detail reads that call from the
conversation's file, under the same lock and ownership check the chat's own
reads use. A transcript that no longer holds the call (deleted, compacted,
an older line without a call id) simply yields nothing.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from flowly.activity.recorder import DETAIL_ARGS_MAX_CHARS, DETAIL_RESULT_MAX_CHARS


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content parts
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def _arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def find_call(session_key: str, call_id: str) -> dict[str, str] | None:
    """``{"args", "result"}`` of one tool call in a conversation, or None."""
    if not session_key or not call_id or len(session_key) > 512 or any(c in session_key for c in "/\\\x00"):
        return None
    from flowly.profile import get_flowly_home
    from flowly.session.manager import session_file_lock
    from flowly.session.ownership import require_session_file
    from flowly.utils.helpers import safe_filename

    path = get_flowly_home() / "sessions" / (safe_filename(session_key.replace(":", "_")) + ".jsonl")
    args: str | None = None
    result: str | None = None
    try:
        with session_file_lock(path):
            require_session_file(path, session_key)
            if not path.exists():
                return None
            with path.open(encoding="utf-8") as handle:
                for raw in handle:
                    if call_id not in raw:
                        continue
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
                        result = _text(message.get("content"))
                    for call in message.get("tool_calls") or []:
                        if isinstance(call, dict) and call.get("id") == call_id:
                            args = _arguments((call.get("function") or {}).get("arguments"))
    except Exception as exc:  # noqa: BLE001 — a missing detail is never an error
        logger.debug(f"[activity] could not read a step from the transcript: {exc!r}")
        return None
    if args is None and result is None:
        return None
    return {"args": (args or "{}")[:DETAIL_ARGS_MAX_CHARS], "result": (result or "")[:DETAIL_RESULT_MAX_CHARS]}
