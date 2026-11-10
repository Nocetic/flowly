"""Reactions kept by the agent itself (flowly/session/reactions.py).

A home that keeps no chats on the Flowly server, and a direct gateway, have
their reactions here: the person's on the agent's messages, the agent's on
the person's, given back with the messages by ``chat.history``.
"""

from __future__ import annotations

import pytest

from flowly.channels.feature_rpc import FeatureRpcError
from flowly.gateway.server import GatewayServer
from flowly.session.manager import SessionManager
from flowly.session.reactions import ReactionError, note_agent_reaction, read_reactions
from tests.test_flowly_server_main_history import _channel, _strip_identities
from tests.test_flowly_server_channel import FakeSocket  # noqa: F401


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    return home


def _chat(home, key="web:c1"):
    sessions = SessionManager(home / "workspace")
    chat = sessions.get_or_create(key)
    chat.add_message("user", "plan my week")
    chat.add_message("assistant", "Monday is free.")
    sessions.save(chat)
    return sessions


def _ids(gateway, key="web:c1"):
    return {m["role"]: m["id"] for m in gateway.chat_history_result({"sessionKey": key})["messages"]}


def test_the_persons_reaction_is_kept_and_comes_back_with_its_message(home):
    gateway = GatewayServer(sessions=_chat(home))
    reply = _ids(gateway)["assistant"]
    result = gateway.chat_react_result({"sessionKey": "web:c1", "messageId": reply, "emoji": "❤️"})
    assert result["reactions"] == [{"by": "user", "emoji": "❤️"}] and result["text"] == "Monday is free."
    rows = gateway.chat_history_result({"sessionKey": "web:c1"})["messages"]
    assert [r.get("reactions") for r in rows] == [None, [{"by": "user", "emoji": "❤️"}]]
    # Another emoji replaces it; null takes it off.
    gateway.chat_react_result({"sessionKey": "web:c1", "messageId": reply, "emoji": "👍"})
    assert read_reactions(gateway.sessions, "web:c1") == {reply: [{"by": "user", "emoji": "👍"}]}
    gateway.chat_react_result({"sessionKey": "web:c1", "messageId": reply, "emoji": None})
    assert read_reactions(gateway.sessions, "web:c1") == {}


def test_a_person_reacts_to_the_agents_messages_only_and_with_one_emoji(home):
    gateway = GatewayServer(sessions=_chat(home))
    ids = _ids(gateway)
    with pytest.raises(ReactionError) as own:
        gateway.chat_react_result({"sessionKey": "web:c1", "messageId": ids["user"], "emoji": "❤️"})
    assert own.value.code == "ENTRY_NOT_FOUND"
    with pytest.raises(ReactionError):
        gateway.chat_react_result({"sessionKey": "web:c1", "messageId": "nope", "emoji": "❤️"})
    for bad in ("hi", "❤️❤️", " ", 3):
        with pytest.raises(FeatureRpcError):
            gateway.chat_react_result({"sessionKey": "web:c1", "messageId": ids["assistant"], "emoji": bad})


def test_the_agents_reaction_goes_on_the_persons_newest_message_even_mid_turn(home):
    sessions = _chat(home)
    chat = sessions.get_or_create("web:c1")
    # A turn has begun: the person's message is saved as pending, not yet part of the session.
    sessions.save(chat, extra_messages=[{"role": "user", "content": "and tuesday?"}])
    note_agent_reaction(sessions, "web:c1", "👀")
    # The turn ends: the person's message is in the conversation with its own id now.
    chat.add_message("user", "and tuesday?")
    chat.add_message("assistant", "Tuesday is busy.")
    sessions.save(chat)
    gateway = GatewayServer(sessions=sessions)
    rows = gateway.chat_history_result({"sessionKey": "web:c1"})["messages"]
    reacted = [r for r in rows if r.get("reactions")]
    assert [(r["role"], r["content"][0]["text"] if isinstance(r["content"], list) else r["content"]) for r in reacted] \
        == [("user", "and tuesday?")]
    assert reacted[0]["reactions"] == [{"by": "agent", "emoji": "👀"}]


def test_an_old_conversations_messages_take_reactions_by_the_ids_their_pages_give(home):
    sessions = _chat(home, "web:old")
    _strip_identities(home)
    gateway = GatewayServer(sessions=SessionManager(home / "workspace"))
    reply = _ids(gateway, "web:old")["assistant"]
    gateway.chat_react_result({"sessionKey": "web:old", "messageId": reply, "emoji": "🎉"})
    again = GatewayServer(sessions=SessionManager(home / "workspace"))
    rows = again.chat_history_result({"sessionKey": "web:old"})["messages"]
    assert rows[-1]["reactions"] == [{"by": "user", "emoji": "🎉"}], "found again after a restart"


def test_a_deleted_conversation_takes_its_reactions_with_it(home):
    sessions = _chat(home)
    gateway = GatewayServer(sessions=sessions)
    gateway.chat_react_result({"sessionKey": "web:c1", "messageId": _ids(gateway)["assistant"], "emoji": "❤️"})
    sessions.delete("web:c1")
    assert read_reactions(sessions, "web:c1") == {}


async def test_the_server_reaches_the_main_agents_reactions_and_a_named_agents(home):
    from flowly.profile_host_contract import validate_profile_rpc

    sessions = _chat(home)
    gateway = GatewayServer(sessions=sessions)
    channel, socket = _channel(gateway)
    reply = _ids(gateway)["assistant"]
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "r1", "sessionId": "c-1", "method": "chat.react",
                                                 "params": {"sessionKey": "web:c1", "messageId": reply, "emoji": "❤️"}})
    assert socket.rpc("r1")["result"]["reactions"] == [{"by": "user", "emoji": "❤️"}]
    await channel._handle_relay_message(socket, {"type": "rpc", "id": "r2", "sessionId": "c-1", "method": "chat.react",
                                                 "params": {"sessionKey": "web:c1", "messageId": reply, "emoji": "nope"}})
    assert socket.rpc("r2")["error"]["code"] == "INVALID_PARAMS"
    method, safe = validate_profile_rpc("chat.react", {"sessionKey": "web:c1", "messageId": "m", "emoji": "❤️"})
    assert method == "chat.react" and safe["emoji"] == "❤️"
