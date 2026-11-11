"""Messages between two agents, kept by the agent host itself.

When one agent messages another (``profile_host``), the host tells the
Flowly server the exchange (``agent.exchange``), and the server keeps it the
way apps read chats (flowly-server agent-exchanges.md): one pair conversation
per two agents, and on each agent's chat one row per turn that messaged the
other. A home that keeps no chats on the server (server PROTOCOL A11) kept
none of it: the rows showed nowhere and the two agents' conversation could
not be opened (2026-11-11). Here the host keeps both, named and shaped as the
server's (``pairOf``, ``exchangeRowId``, ``agent_exchange``, ``exchange_message``):

* the pair conversation beside the main agent's chats (``web:agents-<id>``),
  read as a chat's history (``pair_history``) and never a session;
* the row as a card of each agent's chat (flowly/session/cards.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from flowly.session.cards import AGENT_EXCHANGE, put_card

#: A pair conversation's key, as an app reads it (the server's ``web:<pairId>``).
PAIR_KEY = re.compile(r"^web:(agents-[0-9a-f]{24})$")
#: One exchange's three messages at most; a pair keeps its newest.
MAX_PAIR_MESSAGES = 2000
#: Exchanges one row holds (the server's ``EXCHANGES_PER_ROW``).
EXCHANGES_PER_ROW = 50
TEXT_LIMIT = 32_000

_lock = threading.Lock()


def _digest(*parts: str) -> str:
    return hashlib.sha256("\u0000".join(parts).encode()).hexdigest()[:24]


def pair_of(a: str, b: str) -> tuple[str, list[str]]:
    """The two agents' one conversation, the same either way round (the server's ``pairOf``)."""
    participants = sorted([a, b])
    return f"agents-{_digest(*participants)}", participants


def row_id(correlation_id: str, peer: str) -> str:
    """The row of one turn's messages with ``peer`` (the server's ``exchangeRowId``)."""
    return f"exchange-{_digest(correlation_id, peer)}"


def pair_id_of(session_key: str) -> str | None:
    match = PAIR_KEY.match(session_key) if isinstance(session_key, str) else None
    return match.group(1) if match else None


def _pair_path(sessions: Any, pair_id: str) -> Path:
    return Path(sessions.sessions_dir) / f"{pair_id}.pair.json"


def read_pair(sessions: Any, pair_id: str) -> dict[str, Any]:
    try:
        data = json.loads(_pair_path(sessions, pair_id).read_text())
    except (OSError, ValueError):
        return {"participants": [], "messages": []}
    messages = data.get("messages") if isinstance(data, dict) else None
    return {"participants": data.get("participants") if isinstance(data.get("participants"), list) else [],
            "messages": [m for m in (messages if isinstance(messages, list) else []) if isinstance(m, dict)]}


def _keep_message(sessions: Any, pair_id: str, participants: list[str], message: dict[str, Any]) -> dict | None:
    """The message kept, as an entry (``exchange_message``); None when it was there already as it is."""
    from flowly.session.manager import session_file_lock

    path = _pair_path(sessions, pair_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, session_file_lock(path):
        pair = read_pair(sessions, pair_id)
        held = next((m for m in pair["messages"] if m.get("id") == message["id"]), None)
        highest = max((max(int(m.get("seq") or 0), int(m.get("ver") or 0)) for m in pair["messages"]), default=0)
        if held is not None:
            # Told again (Core may tell an exchange on several sessions): it keeps its place.
            if {k: v for k, v in held.items() if k not in ("seq", "ver")} == message:
                return None
            kept = {**message, "seq": held["seq"], "ver": highest + 1}
            messages = [kept if m is held else m for m in pair["messages"]]
        else:
            kept = {**message, "seq": highest + 1, "ver": highest + 1}
            messages = [*pair["messages"], kept]
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"version": 1, "participants": participants,
                                         "messages": messages[-MAX_PAIR_MESSAGES:]}, ensure_ascii=False))
        os.replace(temporary, path)
    return {**kept, "kind": "exchange_message"}


def forget_pairs_of(sessions: Any, profile: str) -> int:
    """An agent went: its pair conversations go with it."""
    gone = 0
    for path in Path(sessions.sessions_dir).glob("agents-*.pair.json"):
        try:
            participants = json.loads(path.read_text()).get("participants") or []
        except (OSError, ValueError):
            continue
        if profile in participants:
            path.unlink(missing_ok=True)
            gone += 1
    return gone


def pair_history(sessions: Any, pair_id: str) -> list[dict[str, Any]]:
    """The two agents' conversation as history rows: each message the server's ``exchange_message`` entry under ``card``."""
    from datetime import datetime, timezone

    rows = []
    for message in sorted(read_pair(sessions, pair_id)["messages"], key=lambda m: int(m.get("seq") or 0)):
        at = float(message.get("at") or 0)
        text = message.get("text") if isinstance(message.get("text"), str) else ""
        rows.append({
            "id": message["id"], "role": "assistant", "kind": "card",
            "content": [{"type": "text", "text": text}],
            "timestamp": datetime.fromtimestamp(at / 1000, tz=timezone.utc).isoformat(),
            "runId": f"card:{message['id']}",
            "card": {**message, "kind": "exchange_message"},
        })
    return rows


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= TEXT_LIMIT else None


def latest_web_chat(sessions: Any) -> str | None:
    """The main agent's chat written in last (where an exchange it is asked in is shown, as the server's)."""
    try:
        listed = sessions.list_sessions()
    except Exception:  # noqa: BLE001
        return None
    chats = [info for info in listed if isinstance(info.get("key"), str) and info["key"].startswith("web:")
             and pair_id_of(info["key"]) is None and info.get("kind") != "voice"]
    chats.sort(key=lambda info: str(info.get("updated_at") or ""), reverse=True)
    return chats[0]["key"] if chats else None


def record_exchange(exchange: dict[str, Any], *, main: Any, chat_of: Any) -> list[tuple[str, str, dict]]:
    """The exchange in the pair conversation and on each agent's row of it.

    ``main``: the main agent's session store (the pair conversations live
    there). ``chat_of(profile, source_session_key | None)`` -> ``(sessions,
    key)`` of the chat an agent's row goes in, or None. Returns what changed,
    ``(profile, key, card)``, for the host to tell the chats' apps: the pair
    conversation's new message first (the main agent's ``web:agents-<id>``),
    then each agent's row.
    """
    phase = exchange.get("phase")
    exchange_id, correlation = exchange.get("exchangeId"), exchange.get("correlationId")
    source, target = exchange.get("source"), exchange.get("target")
    at = int(exchange.get("at") or 0)
    if phase not in ("started", "ended") or not all(isinstance(v, str) and v for v in
                                                    (exchange_id, correlation, source, target)) or source == target:
        return []
    pair_id, participants = pair_of(source, target)
    if phase == "started":
        message = {"id": f"{exchange_id}:request", "at": at, "from": source, "to": target, "role": "request",
                   "text": _text(exchange.get("message")) or "", "exchangeId": exchange_id}
        state = "running"
    elif exchange.get("outcome") == "replied":
        message = {"id": f"{exchange_id}:reply", "at": at, "from": target, "to": source, "role": "reply",
                   "text": _text(exchange.get("reply")) or "", "exchangeId": exchange_id}
        state = "replied"
    else:
        outcome = exchange.get("outcome") if exchange.get("outcome") in ("error", "aborted") else "error"
        message = {"id": f"{exchange_id}:end", "at": at, "from": target, "to": source, "role": outcome,
                   "exchangeId": exchange_id}
        if isinstance(exchange.get("errorCode"), str):
            message["errorCode"] = exchange["errorCode"][:64]
        state = "failed"
    kept = _keep_message(main, pair_id, participants, message)

    put: list[tuple[str, str, dict]] = [("default", f"web:{pair_id}", kept)] if kept else []
    for profile, peer, direction, session_key in ((source, target, "to", exchange.get("sourceSessionKey")),
                                                   (target, source, "from", None)):
        chat = chat_of(profile, session_key if isinstance(session_key, str) else None)
        if chat is None:
            continue
        sessions, key = chat
        card = _row(sessions, key, row_id(correlation, peer), peer, pair_id, direction, exchange_id, state, at)
        if card is not None:
            put.append((profile, key, card))
    return put


def _row(sessions: Any, key: str, card_id: str, peer: str, pair_id: str, direction: str, exchange_id: str,
         state: str, at: int) -> dict | None:
    """The row's state with this exchange's (the server's ``ChatLog.exchange``)."""
    from flowly.session.cards import card as held_card

    held = held_card(sessions, key, card_id) or {}
    exchanges = dict(held.get("exchanges") or {})
    before = exchanges.get(exchange_id)
    if before and before != "running" and state == "running":
        return None
    if before is None and len(exchanges) >= EXCHANGES_PER_ROW:
        return None
    exchanges[exchange_id] = state
    states = list(exchanges.values())
    return put_card(sessions, key, card_id, AGENT_EXCHANGE, {
        "peer": peer, "pairId": pair_id, "direction": direction, "exchanges": exchanges,
        "count": sum(2 if s == "replied" else 1 for s in states),
        "state": "running" if "running" in states else "failed" if "failed" in states else "replied",
        "lastAt": max(at, int(held.get("lastAt") or 0)),
    }, at=at)
