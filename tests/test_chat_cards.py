"""A chat's cards kept by the agent itself (flowly/session/cards.py, exchanges.py).

A home that keeps no chats on the Flowly server (server PROTOCOL A11) had no
card in its chats: an API-key request showed nowhere, a template the agent
drafted vanished on reload, and a talk between two agents left no row and no
conversation to open (2026-11-11). The agent keeps them now, shaped as the
server's log entries, and gives them with its chats' history.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.gateway.server import GatewayServer
from flowly.session.cards import (
    AGENT_EXCHANGE,
    CardError,
    _reset_callbacks_for_tests,
    add_card_callback,
    keep_secret_closed,
    keep_secret_requested,
    put_card,
    read_cards,
)
from flowly.session.exchanges import pair_of, record_exchange, row_id
from flowly.session.manager import SessionManager

CHAT = "web:c1"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    _reset_callbacks_for_tests()
    yield home
    _reset_callbacks_for_tests()


def _sessions(home, key=CHAT, texts=("plan my week", "Monday is free.")):
    sessions = SessionManager(home / "workspace")
    chat = sessions.get_or_create(key)
    for index, text in enumerate(texts):
        chat.add_message("user" if index % 2 == 0 else "assistant", text)
    sessions.save(chat)
    return sessions


def _request(request_id="sr_0123456789abcdef", key=CHAT, created=None):
    import time

    created = time.time() if created is None else created
    return {"id": request_id, "sessionKey": key, "title": "OpenWeather key", "description": "For the forecast.",
            "fields": [{"name": "OPENWEATHER_API_KEY", "label": "API key"}], "replace": False,
            "key": {"kid": "0123456789abcdef", "publicKey": "A" * 43}, "createdAt": created, "expiresAt": created + 3600}


def _kinds(rows):
    return [row.get("kind") or row["role"] for row in rows]


# ── secret requests ──────────────────────────────────────────────────────────


def test_a_secret_request_is_a_card_in_its_chat_in_its_place_and_ends_as_it_ended(home):
    sessions = _sessions(home)
    card = keep_secret_requested(sessions, _request())
    assert card["id"] == "secret-sr_0123456789abcdef" and card["kind"] == "secret_request"
    assert card["state"] == "pending" and card["requestId"] == "sr_0123456789abcdef"
    assert card["fields"] == [{"name": "OPENWEATHER_API_KEY", "label": "API key"}]
    assert card["expiresAt"] > 1e12, "in ms, as every time of the server's"
    # Whole ms: an app reads a time that is not a whole number as none (iOS ``chatLogWhole``).
    assert all(type(card[name]) is int for name in ("at", "expiresAt", "seq", "ver"))
    rows = GatewayServer(sessions=sessions).chat_history_result({"sessionKey": CHAT})["messages"]
    assert _kinds(rows) == ["user", "assistant", "card"]
    assert rows[-1]["card"]["kind"] == "secret_request" and rows[-1]["runId"] == "card:secret-sr_0123456789abcdef"

    closed = keep_secret_closed(sessions, {"id": "sr_0123456789abcdef", "sessionKey": CHAT, "reason": "saved"})
    assert closed["state"] == "saved" and closed["ver"] > card["ver"]
    # A late request never reopens it; a second close never changes it.
    assert keep_secret_requested(sessions, _request())["state"] == "saved"
    assert keep_secret_closed(sessions, {"id": "sr_0123456789abcdef", "sessionKey": CHAT, "reason": "expired"}) is None
    assert [c["state"] for c in read_cards(sessions, CHAT)] == ["saved"]


def test_a_card_takes_its_place_in_time_on_the_page_that_covers_it(home):
    import time

    sessions = _sessions(home, texts=("first", "first answer"))
    time.sleep(0.05)
    keep_secret_requested(sessions, _request())
    time.sleep(0.05)
    chat = sessions.get_or_create(CHAT)
    chat.add_message("user", "after")
    chat.add_message("assistant", "noted")
    sessions.save(chat)
    gateway = GatewayServer(sessions=sessions)
    assert _kinds(gateway.chat_history_result({"sessionKey": CHAT})["messages"]) == \
        ["user", "assistant", "card", "user", "assistant"]
    newest = gateway.chat_history_result({"sessionKey": CHAT, "limit": 2})
    assert _kinds(newest["messages"]) == ["user", "assistant"]
    older = gateway.chat_history_result({"sessionKey": CHAT, "limit": 2, "before": newest["before"]})
    assert _kinds(older["messages"]) == ["user", "assistant", "card"]


def test_a_deleted_chat_takes_its_cards_with_it(home):
    sessions = _sessions(home)
    keep_secret_requested(sessions, _request())
    sessions.delete(CHAT)
    assert read_cards(sessions, CHAT) == []


def test_cards_are_kinds_the_server_knows_and_a_change_of_nothing_is_no_change(home):
    sessions = _sessions(home)
    with pytest.raises(CardError):
        put_card(sessions, CHAT, "x-1", "note", {})
    first = put_card(sessions, CHAT, "template-a", "template_card", {"state": "draft"})
    assert put_card(sessions, CHAT, "template-a", "template_card", {"state": "draft"}) == first
    with pytest.raises(CardError):
        put_card(sessions, CHAT, "template-a", "secret_request", {})
    assert put_card(sessions, CHAT, "template-b", "template_card", {"state": "draft"}, if_present=True) is None


# ── a template's card, put by the server ─────────────────────────────────────


class _GatewayWs:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_the_server_puts_a_templates_card_and_the_chats_apps_hear_it(home):
    sessions = _sessions(home)
    told: list[tuple[str, dict]] = []

    async def carried(key, card):
        told.append((key, card))

    add_card_callback(carried)
    server = GatewayServer(host="127.0.0.1", port=0, sessions=sessions)
    ws = _GatewayWs()
    fields = {"state": "draft", "shareId": "s1", "version": 1, "name": "Rocket Scientist", "counts": {}}
    await server._dispatch_ws_rpc(ws, "host", {"type": "rpc", "id": "p1", "method": "chat.card.put", "params": {
        "sessionKey": CHAT, "card": {"id": "template-d1", "kind": "template_card", "fields": fields}}})
    assert ws.sent[-1]["result"]["card"]["name"] == "Rocket Scientist"
    assert told[-1][0] == CHAT and told[-1][1]["state"] == "draft"
    # Its later state: only for a card the chat has.
    await server._dispatch_ws_rpc(ws, "host", {"type": "rpc", "id": "p2", "method": "chat.card.put", "params": {
        "sessionKey": CHAT, "card": {"id": "template-d1", "kind": "template_card", "fields": {"state": "published"}},
        "ifPresent": True}})
    assert read_cards(sessions, CHAT)[0]["state"] == "published" and read_cards(sessions, CHAT)[0]["name"] == "Rocket Scientist"
    await server._dispatch_ws_rpc(ws, "host", {"type": "rpc", "id": "p3", "method": "chat.card.put", "params": {
        "sessionKey": CHAT, "card": {"id": "template-zz", "kind": "template_card", "fields": {"state": "published"}},
        "ifPresent": True}})
    assert ws.sent[-1]["result"]["card"] is None and len(told) == 2
    # Only a template's card is put this way, and never a large one.
    for bad in ({"id": "secret-x", "kind": "secret_request", "fields": {}},
                {"id": "template-x", "kind": "template_card", "fields": {"x": "y" * 20_000}},
                {"id": "nope", "kind": "template_card", "fields": {}}):
        await server._dispatch_ws_rpc(ws, "host", {"type": "rpc", "id": "bad", "method": "chat.card.put",
                                                   "params": {"sessionKey": CHAT, "card": bad}})
        assert ws.sent[-1]["error"]["code"] == "INVALID_PARAMS"


@pytest.mark.asyncio
async def test_only_the_server_puts_a_card_through_the_server_channel(home):
    from tests.test_flowly_server_main_history import _channel

    sessions = _sessions(home)
    channel, socket = _channel(GatewayServer(sessions=sessions))
    card = {"id": "template-d1", "kind": "template_card", "fields": {"state": "draft"}}
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "a1", "sessionId": "c-1", "method": "chat.card.put",
                                                 "params": {"sessionKey": CHAT, "card": card}})
    assert socket.rpc("a1")["error"]["code"] == "FORBIDDEN", "an app's session never puts one"
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "s1", "sessionId": "", "method": "chat.card.put",
                                                 "params": {"sessionKey": CHAT, "card": card}})
    assert socket.rpc("s1")["result"]["card"]["state"] == "draft"


# ── two agents' messages ─────────────────────────────────────────────────────


def _exchange(phase, **extra):
    return {"phase": phase, "exchangeId": "profile-run-1", "correlationId": "turn-1", "source": "default",
            "target": "tsuki", "sourceSessionKey": CHAT, "targetSessionKey": "agent:inbox", "at": 1_700_000_000_000,
            **extra}


def test_an_exchange_is_kept_as_the_two_agents_conversation_and_a_row_in_each_chat(home, tmp_path):
    main = _sessions(home)
    tsuki = SessionManager(tmp_path / "tsuki" / "workspace", sessions_dir=tmp_path / "tsuki" / "sessions")
    chats = {"default": main, "tsuki": tsuki}

    def chat_of(profile, source_key):
        return (chats[profile], source_key or "desktop:profile-home") if profile == "default" else (tsuki, "desktop:profile-home")

    put = record_exchange(_exchange("started", message="How are the rockets?"), main=main, chat_of=chat_of)
    pair_id, participants = pair_of("default", "tsuki")
    assert participants == ["default", "tsuki"]
    # The pair conversation's new message, on its own key, for a view of it that is open.
    message = put.pop(0)
    assert message[:2] == ("default", f"web:{pair_id}")
    assert message[2]["kind"] == "exchange_message" and message[2]["text"] == "How are the rockets?"
    assert [(profile, key, card["state"], card["direction"]) for profile, key, card in put] == [
        ("default", CHAT, "running", "to"), ("tsuki", "desktop:profile-home", "running", "from")]
    assert put[0][2]["id"] == row_id("turn-1", "tsuki") and put[0][2]["pairId"] == pair_id
    assert put[0][2]["kind"] == AGENT_EXCHANGE

    put = record_exchange(_exchange("ended", outcome="replied", reply="Launching Friday."), main=main, chat_of=chat_of)
    assert [card["state"] for _p, _k, card in put[1:]] == ["replied", "replied"]
    assert put[1][2]["count"] == 2
    # A late "started" never takes an answered exchange back to running.
    assert record_exchange(_exchange("started", message="How are the rockets?"), main=main, chat_of=chat_of) == []

    gateway = GatewayServer(sessions=main)
    pair = gateway.chat_history_result({"sessionKey": f"web:{pair_id}"})
    assert pair["readOnly"] is True
    entries = [row["card"] for row in pair["messages"]]
    assert [(e["kind"], e["role"], e["from"], e.get("text")) for e in entries] == [
        ("exchange_message", "request", "default", "How are the rockets?"),
        ("exchange_message", "reply", "tsuki", "Launching Friday.")]
    assert all(type(e[name]) is int for e in entries for name in ("seq", "ver", "at"))
    assert type(put[1][2]["at"]) is int and type(put[1][2]["lastAt"]) is int
    assert not main._get_session_path(f"web:{pair_id}").exists(), "never a session of its own"
    # The main agent's chat shows its row in its place (the exchange is older than its messages here).
    assert _kinds(gateway.chat_history_result({"sessionKey": CHAT})["messages"]) == ["card", "user", "assistant"]


def test_a_failed_exchange_says_so_and_the_main_agent_asked_shows_it_in_its_latest_chat(home, tmp_path):
    from flowly.profile_host import _exchange_chat

    main = _sessions(home, key="web:older")
    _sessions(home, key="web:newer")
    sessions, key = _exchange_chat("default", None)
    assert key == "web:newer"
    assert _exchange_chat("default", "web:older")[1] == "web:older"
    assert _exchange_chat("default", "web:agents-0123456789abcdef01234567")[1] == "web:newer", "never a pair"

    def chat_of(profile, source_key):
        return (main, "web:newer") if profile == "default" else None

    exchange = _exchange("ended", outcome="error", errorCode="PROFILE_RESPONSE_TIMEOUT",
                         source="tsuki", target="default", sourceSessionKey="desktop:profile-home")
    put = record_exchange(exchange, main=main, chat_of=chat_of)[1:]
    assert [(p, k, c["state"], c["direction"]) for p, k, c in put] == [("default", "web:newer", "failed", "from")]
    pair_id, _ = pair_of("tsuki", "default")
    end = GatewayServer(sessions=main).chat_history_result({"sessionKey": f"web:{pair_id}"})["messages"][-1]["card"]
    assert end["role"] == "error" and end["errorCode"] == "PROFILE_RESPONSE_TIMEOUT"


@pytest.mark.asyncio
async def test_the_host_keeps_an_exchange_and_tells_each_chat(home, monkeypatch, tmp_path):
    from flowly import profile_host

    main = _sessions(home)
    tsuki = SessionManager(tmp_path / "tsuki" / "workspace", sessions_dir=tmp_path / "tsuki" / "sessions")
    monkeypatch.setattr(profile_host, "_exchange_sessions", lambda profile: main if profile == "default" else tsuki)
    told: list[tuple[str, dict]] = []

    async def carried(key, card):
        told.append((key, card))

    add_card_callback(carried)
    host = object.__new__(profile_host.ProfileHost)
    emitted: list[tuple[str, str, dict]] = []

    async def emit(profile, event, data):
        emitted.append((profile, event, data))

    host._emit = emit  # type: ignore[method-assign]
    await host._emit_exchange("tsuki", {k: v for k, v in _exchange("started", message="hi").items() if k != "at"})
    await host._exchange_records
    assert [e[1] for e in emitted] == ["agent.exchange", "chat.card"]
    assert emitted[1][0] == "tsuki" and emitted[1][2]["sessionKey"] == "desktop:profile-home"
    pair_id, _ = pair_of("default", "tsuki")
    assert [key for key, _card in told] == [f"web:{pair_id}", CHAT], "the main agent's chats are told by its own surfaces"
    assert told[0][1]["kind"] == "exchange_message"
    assert read_cards(tsuki, "desktop:profile-home")[0]["peer"] == "default"


@pytest.mark.asyncio
async def test_the_host_keeps_an_exchanges_phases_in_order_off_its_way(home, monkeypatch, tmp_path):
    from flowly import profile_host

    main = _sessions(home)
    tsuki = SessionManager(tmp_path / "tsuki" / "workspace", sessions_dir=tmp_path / "tsuki" / "sessions")
    monkeypatch.setattr(profile_host, "_exchange_sessions", lambda profile: main if profile == "default" else tsuki)
    host = object.__new__(profile_host.ProfileHost)

    async def emit(profile, event, data):
        return None

    host._emit = emit  # type: ignore[method-assign]
    started = {k: v for k, v in _exchange("started", message="hi").items() if k != "at"}
    ended = {k: v for k, v in _exchange("ended", outcome="replied", reply="hello").items() if k != "at"}
    await host._emit_exchange("tsuki", started)
    await host._emit_exchange("tsuki", ended)
    await host._exchange_records
    assert read_cards(tsuki, "desktop:profile-home")[0]["state"] == "replied"
    pair_id, _ = pair_of("default", "tsuki")
    roles = [row["card"]["role"] for row in GatewayServer(sessions=main).chat_history_result({"sessionKey": f"web:{pair_id}"})["messages"]]
    assert roles == ["request", "reply"]


@pytest.mark.asyncio
async def test_a_named_agents_card_reaches_the_server_on_its_chats_session():
    from unittest.mock import AsyncMock

    from flowly.agent_home import HOME_SESSION
    from flowly.channels.flowly_server import FlowlyServerChannel

    channel = object.__new__(FlowlyServerChannel)
    channel._profile_sessions = lambda: {"tsuki": "srv-p:tsuki:c1"}
    channel._profile_run_bindings = {}
    channel._send_or_queue = AsyncMock()
    card = {"id": "secret-sr_1", "kind": "secret_request", "state": "pending"}
    await channel._forward_profile_event_to_server("tsuki", "chat.card", {"sessionKey": HOME_SESSION, "card": card})
    sent = json.loads(channel._send_or_queue.await_args.args[0])
    assert sent == {"type": "event", "sessionId": "srv-p:tsuki:c1", "event": "chat.card", "profile": "tsuki",
                    "data": {"sessionKey": HOME_SESSION, "card": card}}


@pytest.mark.asyncio
async def test_a_card_is_carried_to_every_surface_even_when_one_fails(home):
    from flowly.session.cards import announce

    told = []

    async def broken(key, card):
        raise RuntimeError("socket gone")

    async def carried(key, card):
        told.append(key)

    add_card_callback(broken)
    add_card_callback(carried)
    await announce(CHAT, {"id": "x"})
    assert told == [CHAT]
    await asyncio.sleep(0)


def test_a_card_made_in_the_same_millisecond_as_a_row_shows_after_it():
    """Times are whole ms (as the server's); a row's has microseconds. Rounded
    down, a card made just after a row in the same millisecond showed before it."""
    from datetime import datetime

    from flowly.live_voice.chat_calls import with_calls
    from flowly.session.cards import card_rows, whole_ms

    row_at = 1_700_000_000.1234
    rows = [{"id": "u", "role": "user", "timestamp": datetime.fromtimestamp(row_at).isoformat()}]
    card = {"id": "c", "kind": "secret_request", "seq": 1, "ver": 1, "at": whole_ms(row_at * 1000 + 0.3)}
    assert [row["id"] for row in with_calls(rows, card_rows([card]))] == ["u", "c"]
