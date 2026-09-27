"""Files sent into a live voice conversation, stored only on this agent.

Clients send the bytes inline over the authenticated voice RPC (Relay only
relays them); nothing is uploaded to or kept on Flowly servers. Storage uses
the same ``_save_attachments`` publication path as a chat turn, scoped to the
voice conversation's owner, and the voice backend receives only a reference.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
from pathlib import Path

from flowly.live_voice.sessions import VoiceError, bounded_text, identity, session_key

MAX_FILES = 10
MAX_TOTAL_BYTES = 25 * 1024 * 1024
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$", re.I)


def read_attachments(value: object) -> list[dict]:
    """Inline bytes only: a CDN URL or a host file path is never accepted here."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_FILES:
        raise VoiceError("INVALID_PARAMS", f"Send between 1 and {MAX_FILES} files.")
    items, total = [], 0
    for row in value:
        if not isinstance(row, dict) or set(row) - {"fileName", "mimeType", "content"}:
            raise VoiceError("INVALID_PARAMS", "Voice files carry only a name, a type and inline content.")
        name = bounded_text(row.get("fileName"), "fileName", maximum=255)
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise VoiceError("INVALID_PARAMS", "Invalid file name.")
        mime = row.get("mimeType")
        if not isinstance(mime, str) or not _MIME.fullmatch(mime):
            raise VoiceError("INVALID_PARAMS", "Invalid file type.")
        content = row.get("content")
        if not isinstance(content, str) or not content:
            raise VoiceError("INVALID_PARAMS", "File content is required.")
        if content.startswith("data:") and "," in content:
            content = content.split(",", 1)[1]
        if len(content) > (MAX_TOTAL_BYTES * 4) // 3 + 4:
            raise VoiceError("LIMIT", "These files are too large for one message.")
        try:
            size = len(base64.b64decode(content, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise VoiceError("INVALID_PARAMS", "File content is not valid base64.") from exc
        total += size
        if total > MAX_TOTAL_BYTES:
            raise VoiceError("LIMIT", "These files are too large for one message.")
        items.append({"fileName": name, "mimeType": mime.lower(), "content": content, "size": size})
    return items


def _store(conversation_id: str, items: list[dict]) -> list[str]:
    from flowly.gateway.server import _save_attachments
    from flowly.live_voice.events import EventAccess, event_access_scope
    from flowly.profile import get_flowly_home
    from flowly.session.control_access import SessionControlScope

    # Private media owned by the voice conversation's owner, like a chat turn.
    scope = SessionControlScope.capture(session_key(conversation_id))
    with event_access_scope(EventAccess(scopes=(scope,))):
        return _save_attachments([{key: item[key] for key in ("fileName", "mimeType", "content")} for item in items],
                                 get_flowly_home() / "media")


async def store_attachments(sessions, params: dict) -> dict:
    conversation = sessions.require_active(params)
    conversation_id = conversation["conversationId"]
    command_id = identity(params.get("commandId"), "commandId")
    existing = sessions.attachment_record(conversation_id, command_id)
    if existing:
        return {"record": existing, "replayed": True}
    items = read_attachments(params.get("attachments"))
    paths = await asyncio.to_thread(_store, conversation_id, items)
    if len(paths) != len(items):
        raise VoiceError("INVALID_PARAMS", "A file could not be stored.")
    files = [{"fileName": item["fileName"], "mimeType": item["mimeType"], "size": item["size"], "path": str(Path(path))}
             for item, path in zip(items, paths)]
    record, created = sessions.record_attachments(conversation_id, command_id, files)
    return {"record": record, **({} if created else {"replayed": True})}
