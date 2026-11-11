"""A chat's cards, kept by the agent itself: what the chat shows that is not a message.

The Flowly server keeps a stored chat's cards as entries of its log
(chat-one-log.md): a secret request (``secret_request``), a template the
agent drafted (``template_card``), the row of a turn's messages with another
agent (``agent_exchange``). A chat it keeps no copy of (server PROTOCOL A11),
and a direct gateway's, had none of them: a request for an API key showed
nowhere, and a reload lost every card (2026-11-11). Here the agent keeps them
beside the chat (``<key>.cards.json``; the session is never rewritten for
one), as the server's entries are (``{id, kind, at, ...fields}``), and
``chat.history`` gives each as one row in its place in time (``card_rows``).
A card that changes is told to the chat's apps as it is now (``chat.card``).
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

#: The kinds a chat's card is, as the server's log names its entries.
SECRET_REQUEST = "secret_request"
TEMPLATE_CARD = "template_card"
AGENT_EXCHANGE = "agent_exchange"
KINDS = frozenset({SECRET_REQUEST, TEMPLATE_CARD, AGENT_EXCHANGE})

#: A history row that is a card (``card_rows``).
ROW_KIND = "card"
#: Cards kept per chat: the newest.
MAX_CARDS = 500
#: A card's id: the server's entry ids (``secret-sr_…``, ``template-…``, ``exchange-…``).
_MAX_ID = 160
#: The place and identity fields: never a card's own.
_PLACE = ("id", "kind", "seq", "ver", "at")

_lock = threading.Lock()


class CardError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def whole_ms(ms: float) -> int:
    """A time in whole ms, as the server's: rounded up, so a card never shows
    before a row written earlier in the same millisecond (its place in time)."""
    return math.ceil(ms)


def _path(sessions: Any, key: str) -> Path | None:
    locate = getattr(sessions, "_get_session_path", None)
    return locate(key).with_suffix(".cards.json") if callable(locate) else None


def read_cards(sessions: Any, key: str) -> list[dict[str, Any]]:
    """The chat's cards, oldest first: each ``{id, kind, seq, ver, at, ...fields}`` (``at`` in ms)."""
    path = _path(sessions, key)
    try:
        data = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError):
        return []
    cards = data.get("cards") if isinstance(data, dict) else None
    out = [c for c in (cards if isinstance(cards, list) else [])
           if isinstance(c, dict) and isinstance(c.get("id"), str) and c.get("kind") in KINDS
           and isinstance(c.get("at"), (int, float)) and isinstance(c.get("ver"), int)]
    return sorted(out, key=lambda c: (c["at"], c.get("seq", 0)))


def _write(path: Path, cards: list[dict[str, Any]]) -> None:
    cards = sorted(cards, key=lambda c: (c["at"], c.get("seq", 0)))[-MAX_CARDS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps({"version": 1, "cards": cards}, ensure_ascii=False))
    os.replace(temporary, path)


def put_card(sessions: Any, key: str, card_id: str, kind: str, fields: dict[str, Any], *,
             at: float | None = None, if_present: bool = False) -> dict[str, Any] | None:
    """The card made, or changed (its fields merged over the ones it had): it as it is now.

    ``at`` (whole ms, as every time of the server's) is when it is shown as
    having happened, its first put's.
    ``if_present``: only a card the chat has is changed (a template's later
    state for a card that was never in this chat makes none). None when
    nothing was put.
    """
    if kind not in KINDS:
        raise CardError("INVALID_PARAMS", "Unknown card kind.")
    if not isinstance(card_id, str) or not 0 < len(card_id) <= _MAX_ID:
        raise CardError("INVALID_PARAMS", "A card needs an id.")
    path = _path(sessions, key)
    if path is None:
        return None
    own = {name: value for name, value in fields.items() if name not in _PLACE}
    from flowly.session.manager import session_file_lock

    # A named agent's chat takes cards from its own runtime and from the agent host (exchanges).
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, session_file_lock(path):
        cards = read_cards(sessions, key)
        held = next((c for c in cards if c["id"] == card_id), None)
        if held is None and if_present:
            return None
        highest = max((c["ver"] for c in cards), default=0)
        if held is None:
            card = {"id": card_id, "kind": kind, "seq": max((c.get("seq", 0) for c in cards), default=0) + 1,
                    "ver": highest + 1, "at": whole_ms(at if at is not None else time.time() * 1000), **own}
            cards.append(card)
        else:
            if held["kind"] != kind:
                raise CardError("INVALID_PARAMS", "This card is of another kind.")
            changed = {**held, **own}
            if changed == held:
                return held
            card = {**changed, "ver": highest + 1}
            cards = [card if c["id"] == card_id else c for c in cards]
        _write(path, cards)
    return card


def card(sessions: Any, key: str, card_id: str) -> dict[str, Any] | None:
    return next((c for c in read_cards(sessions, key) if c["id"] == card_id), None)


def forget_cards(sessions: Any, key: str) -> None:
    """The chat went: so do its cards."""
    path = _path(sessions, key)
    if path is not None:
        path.unlink(missing_ok=True)


def fallback_text(card: dict[str, Any]) -> str:
    """The line a client that does not know cards shows for one."""
    kind = card.get("kind")
    if kind == SECRET_REQUEST:
        return f"🔑 {card.get('title') or 'Secret request'}"
    if kind == TEMPLATE_CARD:
        return f"📦 {card.get('name') or 'Template'}"
    peer = card.get("peer") or "another agent"
    return f"💬 {'Messaged' if card.get('direction') == 'to' else 'Message from'} {peer}"


def card_rows(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The cards as history rows, each the card itself under ``card``, oldest first."""
    rows = []
    for card in cards:
        at = datetime.fromtimestamp(card["at"] / 1000, tz=timezone.utc).isoformat()
        rows.append({
            "id": card["id"], "role": "assistant", "kind": ROW_KIND,
            "content": [{"type": "text", "text": fallback_text(card)}],
            "timestamp": at,
            # Never a real run: no tool steps or run chips attach to it.
            "runId": f"card:{card['id']}",
            "card": dict(card),
        })
    return rows


# ── telling a chat's apps (``chat.card``) ─────────────────────────────────────

#: ``(session_key, card)``: carry a card as it is now to the chat's apps.
CardCallback = Callable[[str, dict], Awaitable[None]]
_callbacks: list[CardCallback] = []


def add_card_callback(callback: CardCallback) -> None:
    _callbacks.append(callback)


def _reset_callbacks_for_tests() -> None:
    _callbacks.clear()


async def announce(session_key: str, card: dict[str, Any]) -> None:
    """The card as it is now, to every surface that carries this agent's chats."""
    for callback in list(_callbacks):
        try:
            await callback(session_key, card)
        except Exception:  # noqa: BLE001 - one surface's failure never costs another its card
            from loguru import logger

            logger.exception("chat.card not carried")


# ── secret requests (flowly/secrets/requests.py; the server's secrets.ts) ────

#: States a request leaves only by being answered again: a late ``secret.requested`` never reopens it.
SECRET_FINAL = frozenset({"saved", "declined", "expired", "superseded"})


def secret_card_id(request_id: str) -> str:
    return f"secret-{request_id}"


def keep_secret_requested(sessions: Any, wire: dict[str, Any]) -> dict[str, Any] | None:
    """A request's card (``secret.requested``), as the server's entry is: pending until answered."""
    key, request_id = wire.get("sessionKey"), wire.get("id")
    if not isinstance(key, str) or not key or not isinstance(request_id, str) or not request_id:
        return None
    held = card(sessions, key, secret_card_id(request_id))
    fields = {
        "requestId": request_id, "title": wire.get("title") or "", "description": wire.get("description") or "",
        "fields": [{"name": f.get("name"), "label": f.get("label")} for f in wire.get("fields") or [] if isinstance(f, dict)],
        "replace": wire.get("replace") is True, "key": dict(wire.get("key") or {}),
        "expiresAt": int(float(wire.get("expiresAt") or 0) * 1000),
        "state": held["state"] if held and held.get("state") in SECRET_FINAL else "pending",
    }
    created = wire.get("createdAt")
    at = float(created) * 1000 if isinstance(created, (int, float)) else None
    return put_card(sessions, key, secret_card_id(request_id), SECRET_REQUEST, fields, at=at)


def keep_secret_closed(sessions: Any, wire: dict[str, Any]) -> dict[str, Any] | None:
    """How a request ended (``secret.closed``), on its card; a card already ended stays as it ended."""
    key, request_id, reason = wire.get("sessionKey"), wire.get("id"), wire.get("reason")
    if not isinstance(key, str) or not isinstance(request_id, str) or reason not in SECRET_FINAL:
        return None
    held = card(sessions, key, secret_card_id(request_id))
    if held is None or held.get("state") in SECRET_FINAL:
        return None
    return put_card(sessions, key, secret_card_id(request_id), SECRET_REQUEST, {"state": reason, "code": None})


# ── a card the Flowly server puts (``chat.card.put``): a template's ─────────

#: The server's word for a template card's fields; nothing an app sends.
MAX_PUT_BYTES = 16 * 1024


def validate_put(params: dict[str, Any]) -> tuple[str, str, dict[str, Any], bool]:
    """``(session_key, card_id, fields, if_present)`` of a ``chat.card.put``, or CardError."""
    key = params.get("sessionKey")
    put = params.get("card")
    if not isinstance(key, str) or not 0 < len(key) <= 256:
        raise CardError("INVALID_PARAMS", "sessionKey is required")
    if not isinstance(put, dict) or put.get("kind") != TEMPLATE_CARD:
        raise CardError("INVALID_PARAMS", "Only a template's card is put here.")
    card_id = put.get("id")
    if not isinstance(card_id, str) or not card_id.startswith("template-") or len(card_id) > _MAX_ID:
        raise CardError("INVALID_PARAMS", "A template card's id is required.")
    fields = put.get("fields")
    if not isinstance(fields, dict):
        raise CardError("INVALID_PARAMS", "A card's fields are required.")
    try:
        size = len(json.dumps(fields, ensure_ascii=False).encode())
    except (TypeError, ValueError):
        raise CardError("INVALID_PARAMS", "A card's fields are not readable.") from None
    if size > MAX_PUT_BYTES:
        raise CardError("INVALID_PARAMS", "A card's fields are too large.")
    if_present = params.get("ifPresent", False)
    if type(if_present) is not bool:
        raise CardError("INVALID_PARAMS", "ifPresent is a boolean")
    return key, card_id, fields, if_present
