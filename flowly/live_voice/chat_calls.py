"""A chat's voice calls as its history and its list row show them.

A call bound to a chat (``chat_binding.py``) keeps everything about itself in
its own ledger (``desktop:voice:<id>``): which chat, when, how long, what was
said. The Flowly server keeps a home's chats as one ``voice_call`` row per
call, written from the call's records; a home that keeps no chats there
(server PROTOCOL A11), and a direct gateway, have no such row: the call
showed while it ran and was gone from the chat after (2026-11-10). Here the
chat's history gives the same row, built from the ledger each time it is
read, and the chat's list row says so when the call is all the chat has.
Nothing is written into the chat for it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flowly.live_voice.chat_binding import call_record
from flowly.live_voice.sessions import session_key as voice_session_key

ROW_KIND = "voice_call"


def fallback_text(duration_ms: int, *, live: bool = False) -> str:
    """The line a client that does not know the card shows (the server's ``voiceCallFallback``)."""
    if live:
        return "🎙 Voice chat"
    seconds = max(0, duration_ms) // 1000
    return f"🎙 Voice chat · {seconds // 60}:{seconds % 60:02d}"


def row_id(call_id: str) -> str:
    return f"voice-call-{call_id}"


def _row(record: dict) -> dict | None:
    """The call's history row, as the server stores it (``saveVoiceCall``); None when it leaves none."""
    call_id = record.get("callId")
    phase = record.get("phase")
    turns = record.get("turns")
    # A call nobody spoke in is ``discarded`` (``call_record``): it leaves nothing in the chat.
    if not isinstance(call_id, str) or not call_id or phase not in ("live", "ended"):
        return None
    voice_call: dict[str, Any] = {
        "callId": call_id, "phase": phase, "startedAt": record.get("startedAt"),
        "endedAt": record.get("endedAt"), "durationMs": record.get("durationMs", 0),
    }
    if phase == "ended":
        voice_call["turnCount"] = len(turns)
        voice_call["truncated"] = record.get("truncated") is True
    row = {
        "id": row_id(call_id),
        "role": "assistant",
        "kind": ROW_KIND,
        "content": [{"type": "text", "text": fallback_text(int(record.get("durationMs") or 0), live=phase == "live")}],
        "timestamp": record.get("startedAt"),
        # Never a real run: no tool steps or run chips attach to it.
        "runId": f"voice-call:{call_id}",
        "voiceCall": voice_call,
    }
    if phase == "ended":
        row["turns"] = turns
    return row


def chat_calls(sessions: Any, chat: str, *, now: datetime | None = None, listed: list[dict] | None = None) -> list[dict]:
    """The chat's calls as history rows, oldest first (a deleted call, or one nobody spoke in, is none).

    ``listed``: the sessions' list already read (``list_sessions``), for many chats at once.
    """
    rows: list[dict] = []
    if listed is None:
        try:
            listed = sessions.list_sessions()
        except Exception:  # noqa: BLE001 - a chat reads without its calls rather than not at all
            return rows
    for info in listed:
        if info.get("kind") != "voice" or info.get("voiceChatSessionKey") != chat:
            continue
        call_id = info.get("voiceConversationId")
        if not isinstance(call_id, str) or not call_id:
            continue
        try:
            session = sessions.read(voice_session_key(call_id))
        except Exception:  # noqa: BLE001
            continue
        if session is None:
            continue
        voice = session.metadata.get("voice") if isinstance(session.metadata.get("voice"), dict) else {}
        if voice.get("deletedAt") or voice.get("chatSessionKey") != chat:
            continue
        record = call_record(session, now=now or datetime.now(timezone.utc))
        row = _row(record) if record else None
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: _seconds(row.get("timestamp")) or 0.0)
    return rows


def _seconds(value: Any) -> float | None:
    if isinstance(value, str) and value:
        try:
            # A row's time is naive local (``datetime.now()``), a call's UTC with its zone.
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000 if value > 1e12 else float(value)
    return None


def with_calls(rows: list[dict], calls: list[dict], *, lower: float | None = None,
               upper: float | None = None) -> list[dict]:
    """``rows`` with the calls that started in [``lower``, ``upper``) among them, each after the last row before it."""
    shown = [call for call in calls
             if (lower is None or (_seconds(call.get("timestamp")) or 0.0) >= lower)
             and (upper is None or (_seconds(call.get("timestamp")) or 0.0) < upper)]
    if not shown:
        return rows
    out = list(rows)
    for call in shown:
        at = _seconds(call.get("timestamp")) or 0.0
        index = len(out)
        while index > 0 and (_seconds(out[index - 1].get("timestamp")) or 0.0) > at:
            index -= 1
        out.insert(index, call)
    return out


def page_bounds(full: list[dict], page: list[dict], identities: list[str]) -> tuple[float | None, float | None]:
    """The times a page of ``full`` covers: from its first row (none for the oldest page) to the next page's first."""
    if not page:
        return None, None
    try:
        start = identities.index(page[0].get("id"))
    except ValueError:
        return None, None
    end = start + len(page)
    lower = _seconds(full[start].get("timestamp")) if start > 0 else None
    upper = _seconds(full[end].get("timestamp")) if end < len(full) else None
    return lower, upper


def preview(sessions: Any, chat: str, *, now: datetime | None = None, listed: list[dict] | None = None) -> dict | None:
    """The newest call's line for a chat whose list row has no words of its own: ``{text, role}``."""
    calls = chat_calls(sessions, chat, now=now, listed=listed)
    if not calls:
        return None
    newest = calls[-1]
    text = newest["content"][0]["text"]
    return {"text": text, "role": "assistant", "at": newest.get("timestamp")}


def forget_chat_calls(sessions: Any, chat: str) -> int:
    """A deleted chat's calls go with it (as the server deletes a stored chat's call rows).

    Each ended call is deleted the way the voice library deletes one (its
    tombstone, ``VoiceSessions.delete``); a call still running stays until it
    ends. Returns how many went.
    """
    from flowly.live_voice.sessions import VoiceError, VoiceSessions

    gone = 0
    try:
        listed = sessions.list_sessions()
    except Exception:  # noqa: BLE001
        return gone
    for info in listed:
        if info.get("kind") != "voice" or info.get("voiceChatSessionKey") != chat:
            continue
        call_id = info.get("voiceConversationId")
        if not isinstance(call_id, str) or not call_id:
            continue
        store = VoiceSessions(sessions)
        # Deleted as its own owner (the host's, or the account's that started it).
        owner = info.get("voiceOwner")
        store._owner = owner if isinstance(owner, dict) else {"kind": "host"}
        try:
            if store.delete({"conversationId": call_id}).get("deleted") is not False:
                gone += 1
        except VoiceError:
            continue
    return gone



def with_chat_calls(rows: list[dict], sessions: Any, chat_file_exists: Any) -> list[dict]:
    """A list page (``sessions.list`` rows) with what its chats' calls say.

    - A chat with no words of its own says its newest call ("🎙 Voice chat · 0:57").
    - A chat whose call is running now and that has no session yet (a call
      started in a new chat, before anything is written into it) is listed
      at once, where its call is; it comes back no other way. A chat deleted
      while its call runs is the only one it could bring back.
    - A call that belongs to a chat is not listed on its own: it is shown in
      that chat (as the call library, ``voice.list``, leaves it out).
    """
    chats_without_words = [row for row in rows if str(row.get("key", "")).startswith("web:") and not row.get("lastMessage")]
    voice_rows = [row for row in rows if row.get("kind") == "voice"]
    if not chats_without_words and not voice_rows:
        return rows
    try:
        listed = sessions.list_sessions()
    except Exception:  # noqa: BLE001
        return rows
    bound = {info.get("voiceConversationId"): info.get("voiceChatSessionKey") for info in listed
             if info.get("kind") == "voice" and isinstance(info.get("voiceChatSessionKey"), str)}
    chats = {chat for chat in bound.values()}
    for row in chats_without_words:
        if row["key"] in chats:
            line = preview(sessions, row["key"], listed=listed)
            if line:
                row["lastMessage"], row["lastMessageRole"] = line["text"], line["role"]
    keys = {row.get("key") for row in rows}
    out: list[dict] = []
    for row in rows:
        chat = bound.get(row.get("voiceConversationId")) if row.get("kind") == "voice" else None
        if isinstance(chat, str) and chat not in keys and not chat_file_exists(chat):
            calls = chat_calls(sessions, chat, listed=listed)
            if any(call["voiceCall"]["phase"] == "live" for call in calls):
                keys.add(chat)
                out.append({
                    "key": chat, "fileName": None, "sizeBytes": 0, "modifiedAt": row.get("modifiedAt"),
                    "channel": "web", "chatId": chat.split(":", 1)[1], "title": None,
                    "displayName": chat.split(":", 1)[1], "updatedAt": row.get("updatedAt"),
                    "lastAssistantRunId": None, "lastAssistantAt": None,
                    "lastMessage": fallback_text(0, live=True), "lastMessageRole": "assistant",
                    "running": False, "modelOverride": None,
                })
        if isinstance(chat, str):
            continue
        out.append(row)
    return out
