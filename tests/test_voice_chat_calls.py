"""A chat's voice calls in its history and its list row, from the calls' own ledgers.

The Flowly server keeps a stored chat's calls as rows; a home that keeps no
chats there (server PROTOCOL A11) had none: a call showed while it ran and was
gone from the chat after, a chat begun by a call was listed under its id with
no words, and only once the call ended (2026-11-10).
"""
from __future__ import annotations

import time

import pytest

from flowly.channels.feature_rpc import sessions_list
from flowly.gateway.server import GatewayServer
from flowly.live_voice.chat_calls import chat_calls, forget_chat_calls
from flowly.live_voice.sessions import VoiceSessions
from flowly.session.manager import SessionManager

CHAT = "web:chat-1"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    return home


@pytest.fixture
def manager(home):
    return SessionManager(home / "workspace")


def call(manager, call_id="call-1", chat=CHAT, words=(("user", "hello"), ("assistant", "hi there")), end=True):
    voice = VoiceSessions(manager)
    voice.open({"conversationId": call_id, "connectionId": f"{call_id}-c", "language": "tr"},
               profile="default", bot_id="agent-1", chat=chat)
    for ordinal, (role, text) in enumerate(words, 1):
        voice.append({"conversationId": call_id, "connectionId": f"{call_id}-c", "messageId": f"{call_id}-m{ordinal}",
                      "ordinal": ordinal, "role": role, "text": text})
    if end:
        voice.end({"conversationId": call_id, "connectionId": f"{call_id}-c"})


def chat(manager, key=CHAT, texts=("plan my week", "Monday is free.")):
    session = manager.get_or_create(key)
    for index, text in enumerate(texts):
        session.add_message("user" if index % 2 == 0 else "assistant", text)
    manager.save(session)


def kinds(rows):
    return [row.get("kind") or row["role"] for row in rows]


def test_a_chat_begun_by_a_call_shows_the_call_as_its_card_in_history(manager):
    call(manager)
    rows = GatewayServer(sessions=manager).chat_history_result({"sessionKey": CHAT})["messages"]
    assert kinds(rows) == ["voice_call"]
    card = rows[0]
    assert card["id"] == "voice-call-call-1" and card["role"] == "assistant" and card["runId"] == "voice-call:call-1"
    assert card["voiceCall"]["callId"] == "call-1" and card["voiceCall"]["phase"] == "ended"
    assert card["voiceCall"]["turnCount"] == 2
    assert [turn["text"] for turn in card["turns"]] == ["hello", "hi there"]
    assert card["content"][0]["text"].startswith("🎙 Voice chat · ")
    # A page holds it too.
    paged = GatewayServer(sessions=manager).chat_history_result({"sessionKey": CHAT, "limit": 50})["messages"]
    assert kinds(paged) == ["voice_call"]


def test_a_call_in_a_chat_with_messages_takes_its_place_in_time_on_the_one_page_that_covers_it(manager):
    chat(manager, texts=("first", "first answer"))
    time.sleep(0.05)
    call(manager)
    time.sleep(0.05)
    session = manager.get_or_create(CHAT)
    session.add_message("user", "after the call")
    session.add_message("assistant", "noted")
    manager.save(session)
    gateway = GatewayServer(sessions=manager)
    whole = gateway.chat_history_result({"sessionKey": CHAT})["messages"]
    assert kinds(whole) == ["user", "assistant", "voice_call", "user", "assistant"]
    newest = gateway.chat_history_result({"sessionKey": CHAT, "limit": 2})
    assert kinds(newest["messages"]) == ["user", "assistant"], "the call is older than this page"
    older = gateway.chat_history_result({"sessionKey": CHAT, "limit": 2, "before": newest["before"]})
    assert kinds(older["messages"]) == ["user", "assistant", "voice_call"], "it shows once, on its own page"


def test_a_call_nobody_spoke_in_another_chats_call_and_a_running_call(manager):
    call(manager, words=())
    call(manager, "call-2", chat="web:other")
    assert chat_calls(manager, CHAT) == []
    call(manager, "call-3", end=False)
    live = chat_calls(manager, CHAT)
    assert [row["voiceCall"]["phase"] for row in live] == ["live"] and "turns" not in live[0]
    assert live[0]["content"][0]["text"] == "🎙 Voice chat"


def test_the_list_names_a_chat_by_its_call_and_lists_one_whose_call_runs_before_it_is_written(home, manager):
    call(manager)
    rows = {row["key"]: row for row in sessions_list({"limit": 50})["sessions"]}
    assert rows.get(CHAT) is None or rows[CHAT]["lastMessage"].startswith("🎙 Voice chat · ")
    # Its context row makes a session once the call ends (chat_binding.finish): then it says its call.
    session = manager.get_or_create(CHAT)
    session.add_message("user", "[Voice call …]", kind="voice_call_context", voiceCallId="call-1", _display_hidden=True)
    manager.save(session)
    rows = {row["key"]: row for row in sessions_list({"limit": 50})["sessions"]}
    assert rows[CHAT]["lastMessage"].startswith("🎙 Voice chat · ")
    # A call running in a new chat: the chat is listed at once, before anything is written into it.
    call(manager, "call-2", chat="web:new-chat", end=False)
    rows = {row["key"]: row for row in sessions_list({"limit": 50})["sessions"]}
    assert rows["web:new-chat"]["lastMessage"] == "🎙 Voice chat"
    assert rows["web:new-chat"]["title"] is None
    # The call itself is not listed apart from its chat (the call library leaves it out too).
    assert not any(row.get("kind") == "voice" for row in sessions_list({"limit": 50})["sessions"])
    # An ended call of a chat that has no session (deleted) never brings it back.
    call(manager, "call-3", chat="web:gone")
    assert "web:gone" not in {row["key"] for row in sessions_list({"limit": 50})["sessions"]}


def test_a_deleted_chats_calls_go_with_it(home, manager):
    chat(manager)
    call(manager)
    call(manager, "call-2", chat="web:other")
    assert forget_chat_calls(manager, CHAT) == 1
    assert chat_calls(manager, CHAT) == []
    assert manager.read("desktop:voice:call-1") is None
    assert [row["id"] for row in chat_calls(manager, "web:other")] == ["voice-call-call-2"]


@pytest.mark.asyncio
async def test_deleting_a_chat_through_the_agent_deletes_its_calls(home, manager):
    from flowly.agent.loop import AgentLoop
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from tests.test_user_row_run_id import _Reply

    # Its own chat: another test's turn on web:chat-1 may still be in the process-wide registry.
    chat(manager, key="web:delete-me")
    call(manager, chat="web:delete-me")
    loop = AgentLoop(bus=MessageBus(), provider=_Reply(), workspace=home / "workspace", main_config=Config(),
                     max_iterations=2, soft_warn_at_iteration=0)
    try:
        assert await loop.delete_session("web:delete-me") is True
    finally:
        loop.stop()
    assert SessionManager(home / "workspace").read("desktop:voice:call-1") is None
