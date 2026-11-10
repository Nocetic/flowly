"""Reactions on a conversation's messages, kept by the agent itself.

The person's one emoji on one of the agent's messages, the agent's on one of
the person's, as a chat shows them (`{"by": "user" | "agent", "emoji"}`, one
each per message). A home whose chats the Flowly server keeps has them on the
server's log; one that keeps none (server PROTOCOL A11), and a direct gateway,
have them here, and ``chat.history`` gives them with their messages.

Kept beside the session (``<key>.reactions.json``), never in it: a reaction
never rewrites a conversation, nor waits on a turn that is saving it. A
message is named by the id its history page gives it
(:func:`flowly.session.history_page.row_identities`).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

#: Reactions kept per conversation; the oldest go first past it.
MAX_REACTED = 2000
#: The agent's reactions kept per conversation (each on the person's message before it).
MAX_AGENT_NOTES = 500
_SIDES = ("user", "agent")
_lock = threading.Lock()


class ReactionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _path(sessions: Any, key: str) -> Path | None:
    """Where the conversation's reactions are kept; None for a store that keeps no files."""
    locate = getattr(sessions, "_get_session_path", None)
    return locate(key).with_suffix(".reactions.json") if callable(locate) else None


def _read(sessions: Any, key: str) -> dict[str, Any]:
    path = _path(sessions, key)
    try:
        data = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_reactions(sessions: Any, key: str) -> dict[str, list[dict[str, str]]]:
    """Every reacted message of the conversation: ``{messageId: [{by, emoji}]}``."""
    out: dict[str, list[dict[str, str]]] = {}
    reacted = _read(sessions, key).get("reactions")
    for message_id, reactions in reacted.items() if isinstance(reacted, dict) else []:
        clean = [{"by": r["by"], "emoji": r["emoji"]} for r in reactions if isinstance(r, dict)
                 and r.get("by") in _SIDES and isinstance(r.get("emoji"), str)] if isinstance(reactions, list) else []
        if isinstance(message_id, str) and clean:
            out[message_id] = clean
    return out


def read_agent_reactions(sessions: Any, key: str) -> list[dict[str, Any]]:
    """The agent's reactions, in the order it made them: ``[{emoji, at}]`` (``at`` epoch seconds)."""
    notes = _read(sessions, key).get("agent")
    return [{"emoji": n["emoji"], "at": float(n["at"])} for n in (notes if isinstance(notes, list) else [])
            if isinstance(n, dict) and isinstance(n.get("emoji"), str) and isinstance(n.get("at"), (int, float))]


def _write(path: Path, reactions: dict[str, list[dict[str, str]]], agent: list[dict[str, Any]]) -> None:
    if len(reactions) > MAX_REACTED:
        reactions = dict(list(reactions.items())[-MAX_REACTED:])
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps({"version": 1, "reactions": reactions, "agent": agent[-MAX_AGENT_NOTES:]},
                                    ensure_ascii=False))
    os.replace(temporary, path)


def _text(row: dict[str, Any]) -> str:
    content = row.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return " ".join(content.split()) if isinstance(content, str) else ""


def find_message(sessions: Any, key: str, message_id: str) -> dict[str, Any] | None:
    """The message a history page names ``message_id``, from the conversation as it is shown."""
    from flowly.session.history_page import row_identities

    try:
        rows = sessions.get_full_messages(key)
    except (FileNotFoundError, OSError):
        return None
    for row, identity in zip(rows, row_identities(rows)):
        if identity == message_id:
            return row
    return None


def latest_user_message_id(sessions: Any, key: str) -> str | None:
    """The person's newest message in the conversation: where the agent's reaction goes."""
    from flowly.session.history_page import row_identities

    try:
        rows = sessions.get_full_messages(key)
    except (FileNotFoundError, OSError):
        return None
    for row, identity in reversed(list(zip(rows, row_identities(rows)))):
        if row.get("role") == "user" and not row.get("_display_hidden") and _text(row):
            return identity
    return None


def set_reaction(sessions: Any, key: str, message_id: str, by: str, emoji: str | None) -> dict[str, Any]:
    """Put ``by``'s reaction on the message (or take it off, ``emoji`` None).

    The person reacts to the agent's messages, the agent to the person's.
    Returns the message's reactions now, and its words (what the agent is
    told the reaction was on).
    """
    if by not in _SIDES:
        raise ReactionError("INVALID_PARAMS", "by is user or agent")
    message = find_message(sessions, key, message_id)
    wanted = "assistant" if by == "user" else "user"
    if message is None or message.get("role") != wanted or message.get("_display_hidden"):
        raise ReactionError("ENTRY_NOT_FOUND", "There is no message with this id to react to in this chat.")
    path = _path(sessions, key)
    if path is None:
        raise ReactionError("UNAVAILABLE", "This agent keeps no reactions here.")
    with _lock:
        reactions = read_reactions(sessions, key)
        kept = [r for r in reactions.get(message_id, []) if r["by"] != by]
        if emoji:
            kept.append({"by": by, "emoji": emoji})
        reactions.pop(message_id, None)
        if kept:
            reactions[message_id] = kept
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, reactions, read_agent_reactions(sessions, key))
    return {"ok": True, "messageId": message_id, "reactions": kept, "text": _text(message)[:200]}


def note_agent_reaction(sessions: Any, key: str, emoji: str, at: float | None = None) -> None:
    """The agent's reaction (its ``react`` tool) on the person's newest message.

    Kept with when it was made, not with that message's id: in the middle of
    a turn the person's message is saved as pending and gets its id when the
    turn ends. A history read puts it on the person's last message from
    before it (:func:`with_reactions`).
    """
    path = _path(sessions, key)
    if path is None:
        return
    with _lock:
        agent = read_agent_reactions(sessions, key)
        agent.append({"emoji": emoji, "at": time.time() if at is None else at})
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, read_reactions(sessions, key), agent)


def forget_reactions(sessions: Any, key: str) -> None:
    """The conversation went: so do its reactions."""
    path = _path(sessions, key)
    if path is not None:
        path.unlink(missing_ok=True)


def _seconds(value: Any) -> float | None:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def with_reactions(rows: list[dict[str, Any]], reactions: dict[str, list[dict[str, str]]],
                   agent: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """History rows, each with its reactions (``reactions``) when it has some.

    The agent's go on the person's last message from before each was made;
    a later one on the same message replaces an earlier one.
    """
    placed: dict[int, str] = {}
    if agent:
        spoken = [(i, _seconds(row.get("timestamp"))) for i, row in enumerate(rows)
                  if row.get("role") == "user" and _text(row)]
        for note in agent:
            before = [i for i, at in spoken if at is not None and at <= note["at"] + 1]
            if before:
                placed[before[-1]] = note["emoji"]
    if not reactions and not placed:
        return rows
    out = []
    for i, row in enumerate(rows):
        name = row.get("reactId") or row.get("id")
        mine = reactions.get(name, []) if isinstance(name, str) else []
        theirs = [{"by": "agent", "emoji": placed[i]}] if i in placed else []
        kept = [r for r in mine if r["by"] != "agent" or not theirs] + theirs
        out.append({**row, "reactions": kept} if kept else row)
    return out
