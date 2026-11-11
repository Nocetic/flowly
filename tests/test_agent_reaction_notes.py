"""What the agent hears of a person's reaction, kept and told by the agent itself.

A home that keeps no chats on the Flowly server (server PROTOCOL A11) kept
the reaction on the agent, but what the agent was to hear of it (one that
waits for the next message) was held on the server, sealed, until told
(2026-11-11). The server now asks the agent to tell it (``chat.react``
``tell``): the note is held beside the chat, told with its next message from
whichever device, and a reaction that asks for an answer starts the agent's
own turn here (flowly/agent/reaction_notes.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.agent import turn_notes
from flowly.agent.reaction_notes import reaction_note, wakes
from flowly.gateway.server import GatewayServer
from flowly.session.manager import SessionManager
from flowly.session.reactions import HELD_TTL_SECONDS, drop_held, held_notes, read_reactions

CHAT = "web:c1"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    return home


def _sessions(home, key=CHAT):
    sessions = SessionManager(home / "workspace")
    chat = sessions.get_or_create(key)
    chat.add_message("user", "plan my week")
    chat.add_message("assistant", "Monday is free.")
    chat.add_message("user", "and tuesday?")
    chat.add_message("assistant", "Tuesday is busy.")
    sessions.save(chat)
    return sessions


def _replies(gateway, key=CHAT):
    return [m["reactId"] for m in gateway.chat_history_result({"sessionKey": key})["messages"] if m["role"] == "assistant"]


def _react(gateway, message_id, emoji, *, tell=True, key=CHAT):
    return gateway.chat_react_result({"sessionKey": key, "messageId": message_id, "emoji": emoji,
                                      **({"tell": True} if tell else {})})


def _notes(sessions, key=CHAT):
    return [h["note"] for h in held_notes(sessions, key)]


# ── words and the wake rule, as the server's (reactions.ts) ──────────────────


def test_the_note_and_the_wake_rule_are_the_servers():
    assert reaction_note("👍", "Monday  is\nfree.") == 'The user reacted 👍 to your message: "Monday is free."'
    assert reaction_note("👍", "") == "The user reacted 👍 to your message."
    long = reaction_note("👍", "x" * 300)
    assert long.endswith('…"') and len(long.split('"')[1]) == 200
    assert all(wakes(e) for e in ("👎", "👎🏽", "😕", "❌", "🤔", "❓", "❔", "❓️"))
    assert not any(wakes(e) for e in ("👍", "❤️", "😂", "🎉"))


# ── held and told with the next message ─────────────────────────────────────


def test_a_reaction_the_server_asks_to_tell_is_held_beside_the_chat(home):
    sessions = _sessions(home)
    gateway = GatewayServer(sessions=sessions)
    first, second = _replies(gateway)
    result = _react(gateway, first, "👍")
    assert "wake" not in result and result["reactions"] == [{"by": "user", "emoji": "👍"}]
    assert _notes(sessions) == ['The user reacted 👍 to your message: "Monday is free."']
    # A newer reaction on the message replaces what was still to be told of it; another message's stays.
    _react(gateway, first, "❤️")
    _react(gateway, second, "😂")
    assert _notes(sessions) == ['The user reacted ❤️ to your message: "Monday is free."',
                                'The user reacted 😂 to your message: "Tuesday is busy."']
    # The same emoji again tells nothing more (it is not told anew); taking it off withdraws it.
    told = held_notes(sessions, CHAT)
    _react(gateway, first, "❤️")
    assert held_notes(sessions, CHAT) == told
    _react(gateway, first, None)
    assert _notes(sessions) == ['The user reacted 😂 to your message: "Tuesday is busy."']
    assert read_reactions(sessions, CHAT) == {second: [{"by": "user", "emoji": "😂"}]}


def test_without_tell_nothing_is_held_the_server_tells_it(home):
    sessions = _sessions(home)
    gateway = GatewayServer(sessions=sessions)
    first, _second = _replies(gateway)
    assert "wake" not in _react(gateway, first, "👍", tell=False)
    assert "wake" not in _react(gateway, first, "👎", tell=False)
    assert _notes(sessions) == []
    from flowly.channels.feature_rpc import FeatureRpcError

    with pytest.raises(FeatureRpcError):
        gateway.chat_react_result({"sessionKey": CHAT, "messageId": first, "emoji": "👍", "tell": "yes"})


def test_a_held_note_expires_and_a_told_one_goes(home):
    sessions = _sessions(home)
    gateway = GatewayServer(sessions=sessions)
    first, second = _replies(gateway)
    _react(gateway, first, "👍")
    _react(gateway, second, "❤️")
    held = held_notes(sessions, CHAT)
    later = held[0]["at"] + HELD_TTL_SECONDS + 1
    assert held_notes(sessions, CHAT, now=later) == []
    drop_held(sessions, CHAT, [held[0]["id"]])
    assert _notes(sessions) == ['The user reacted ❤️ to your message: "Tuesday is busy."']


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _server_channel(gateway):
    from tests.test_flowly_server_main_history import _channel

    channel, socket = _channel(gateway)
    seen: list[dict] = []

    async def process(session_id, session_key, content, run_id, stream_callback=None, media=None,
                      voice_mode=False, render_capabilities=(), extra_metadata=None):
        seen.append({"session": session_id, "key": session_key, "content": content, "run": run_id,
                     "metadata": extra_metadata or {}})

    channel._process_message = process  # type: ignore[method-assign]
    channel.supports_turn_start = False
    return channel, socket, seen


async def _deliver(channel, rpc_id, params, session="srv-default-c1"):
    ws = _Ws()
    await channel._dispatch_rpc(ws, {"type": "rpc", "id": rpc_id, "sessionId": session, "method": "chat.send",
                                     "params": params})
    await asyncio.sleep(0)
    return ws


@pytest.mark.asyncio
async def test_the_chats_next_message_tells_what_was_held_from_any_device_once(home):
    from flowly.agent import inflight

    sessions = _sessions(home)
    gateway = GatewayServer(sessions=sessions)
    channel, _socket, seen = _server_channel(gateway)
    first, _second = _replies(gateway)
    _react(gateway, first, "👍")

    await _deliver(channel, "m1", {"sessionKey": CHAT, "idempotencyKey": "m1", "message": "what about friday?"})
    assert seen[-1]["content"] == "what about friday?"
    assert seen[-1]["metadata"][turn_notes.TURN_NOTES] == ['The user reacted 👍 to your message: "Monday is free."']
    assert turn_notes.NOTES_ONLY_TURN not in seen[-1]["metadata"]
    assert _notes(sessions) == [], "told once"
    inflight.finish(CHAT, "m1")

    await _deliver(channel, "m2", {"sessionKey": CHAT, "idempotencyKey": "m2", "message": "thanks"})
    assert turn_notes.TURN_NOTES not in seen[-1]["metadata"]
    inflight.finish(CHAT, "m2")
    # A held note goes before a message's own notes.
    _react(gateway, first, "🎉")
    await _deliver(channel, "m3", {"sessionKey": CHAT, "idempotencyKey": "m3", "message": "ok", "notes": ["own"]})
    assert seen[-1]["metadata"][turn_notes.TURN_NOTES] == ['The user reacted 🎉 to your message: "Monday is free."', "own"]
    inflight.finish(CHAT, "m3")


@pytest.mark.asyncio
async def test_a_message_that_is_refused_or_sent_again_tells_nothing_and_keeps_the_note(home):
    from flowly.agent import inflight

    sessions = _sessions(home)
    gateway = GatewayServer(sessions=sessions)
    channel, _socket, seen = _server_channel(gateway)
    first, _second = _replies(gateway)
    await _deliver(channel, "m1", {"sessionKey": CHAT, "idempotencyKey": "m1", "message": "hi"})
    inflight.finish(CHAT, "m1")
    _react(gateway, first, "👍")
    refused = await _deliver(channel, "m2", {"sessionKey": CHAT, "idempotencyKey": "m2", "message": "x", "notes": "bad"})
    assert refused.sent[-1]["error"]["code"] == "INVALID_REQUEST"
    again = await _deliver(channel, "m1b", {"sessionKey": CHAT, "idempotencyKey": "m1", "message": "hi"})
    assert again.sent[-1]["result"]["runId"] == "m1" and len(seen) == 1, "the first acceptance, no new turn"
    assert len(_notes(sessions)) == 1, "kept for the next message"


# ── a reaction that asks for an answer: the agent's own turn ─────────────────


@pytest.mark.asyncio
async def test_a_waking_reaction_starts_the_main_agents_turn_on_the_servers_session(home):
    from flowly.agent import inflight

    sessions = _sessions(home)
    gateway = GatewayServer(sessions=sessions)
    channel, socket, seen = _server_channel(gateway)
    first, second = _replies(gateway)
    _react(gateway, first, "👍")
    await channel._handle_relay_message(socket, {
        "type": "rpc", "id": "r1", "sessionId": "srv-default-c1", "method": "chat.react",
        "params": {"sessionKey": CHAT, "messageId": second, "emoji": "👎", "tell": True}})
    await asyncio.sleep(0)
    answer = socket.rpc("r1")["result"]
    assert answer["reactions"] == [{"by": "user", "emoji": "👎"}] and "wake" not in answer
    assert answer["runId"] == seen[-1]["run"] and answer["runId"].startswith("reaction-")
    turn = seen[-1]
    assert turn["session"] == "srv-default-c1" and turn["key"] == CHAT and turn["content"] == ""
    assert turn["metadata"][turn_notes.NOTES_ONLY_TURN] is True
    assert turn["metadata"][turn_notes.TURN_NOTES] == ['The user reacted 👍 to your message: "Monday is free."',
                                                      'The user reacted 👎 to your message: "Tuesday is busy."']
    assert _notes(sessions) == []
    # Its acknowledgement was the channel's own: the server got only the reaction's answer.
    assert [f["id"] for f in socket.sent if f.get("type") == "rpc"] == ["r1"]
    inflight.finish(CHAT, answer["runId"])


class _GatewayWs:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_a_named_agents_runtime_tells_its_reactions_and_starts_its_own_turn(home):
    """A named agent's chat calls reach its own runtime's gateway (through the profile host)."""
    sessions = _sessions(home)
    seen: list[dict] = []
    done = asyncio.Event()

    async def on_chat_message(session_key, message, run_id, stream_callback, media, voice_mode,
                              iteration_callback, render_capabilities, extra_metadata=None):
        seen.append({"key": session_key, "content": message, "run": run_id, "metadata": extra_metadata or {}})
        done.set()
        return "Sorry, let me fix that.", {}

    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=on_chat_message, sessions=sessions)
    first, second = _replies(server)
    ws = _GatewayWs()
    await server._dispatch_ws_rpc(ws, "host", {"type": "rpc", "id": "r1", "method": "chat.react", "params": {
        "sessionKey": CHAT, "messageId": first, "emoji": "❤️", "tell": True}})
    assert _notes(sessions) == ['The user reacted ❤️ to your message: "Monday is free."'] and not seen
    await server._dispatch_ws_rpc(ws, "host", {"type": "rpc", "id": "r2", "method": "chat.react", "params": {
        "sessionKey": CHAT, "messageId": second, "emoji": "🤔", "tell": True}})
    await asyncio.wait_for(done.wait(), timeout=2)
    await asyncio.gather(*server._active_tasks.values())
    replies = {f["id"]: f for f in ws.sent if f.get("type") == "rpc"}
    assert set(replies) == {"r1", "r2"}, "the turn's own acknowledgement is never sent"
    assert "wake" not in replies["r2"]["result"] and replies["r2"]["result"]["runId"] == seen[-1]["run"]
    assert seen[-1]["content"] == "" and seen[-1]["metadata"][turn_notes.NOTES_ONLY_TURN] is True
    assert seen[-1]["metadata"][turn_notes.TURN_NOTES] == ['The user reacted ❤️ to your message: "Monday is free."',
                                                          'The user reacted 🤔 to your message: "Tuesday is busy."']
    assert _notes(sessions) == []

    # A message on the runtime tells what was held since.
    await server._dispatch_ws_rpc(ws, "host", {"type": "rpc", "id": "r3", "method": "chat.react", "params": {
        "sessionKey": CHAT, "messageId": first, "emoji": "🎉", "tell": True}})
    done.clear()
    await server._ws_rpc_chat_send(ws, "host", "r4", {"sessionKey": CHAT, "idempotencyKey": "m9", "message": "and wednesday?"})
    await asyncio.wait_for(done.wait(), timeout=2)
    await asyncio.gather(*server._active_tasks.values())
    assert seen[-1]["content"] == "and wednesday?"
    assert seen[-1]["metadata"][turn_notes.TURN_NOTES] == ['The user reacted 🎉 to your message: "Monday is free."']
    assert _notes(sessions) == []


@pytest.mark.asyncio
async def test_the_server_channel_binds_a_named_agents_reaction_turn_to_its_chat():
    from unittest.mock import AsyncMock

    from flowly.channels.flowly_server import FlowlyServerChannel

    class _Host:
        async def dispatch(self, method, params):
            return {"ok": True, "messageId": "m", "reactions": [], "text": "", "runId": "reaction-1"}

    channel = object.__new__(FlowlyServerChannel)
    channel._profile_host = _Host()
    channel._profile_run_bindings = {}
    channel._prune_profile_bindings = lambda: None
    channel._remember_profile_session = remembered = AsyncMock()  # type: ignore[assignment]
    ws = _Ws()
    await channel._dispatch_profile_chat(ws, {"type": "rpc", "id": "r1", "sessionId": "srv-p:tsuki:c1",
                                              "method": "chat.react", "params": {
                                                  "profile": "tsuki", "sessionKey": "web:c1", "messageId": "m",
                                                  "emoji": "🤔", "tell": True}}, "tsuki")
    assert ws.sent[-1]["result"]["runId"] == "reaction-1"
    assert channel._profile_run_bindings[("tsuki", "reaction-1")][0] == "web:c1"
    remembered.assert_called_once_with("tsuki", "srv-p:tsuki:c1")


def test_core_says_it_tells_reactions_itself():
    from flowly.channels.flowly_server import CHAT_REACTION_NOTES_FEATURE

    assert CHAT_REACTION_NOTES_FEATURE == "chat-reaction-notes-v1"
