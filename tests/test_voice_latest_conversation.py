"""A call continues where the owner and the agent left off.

The call knew the owner's memory but not what the two had just been talking
about: a call is its own conversation, and a chat turn's history never
reached it. Its memory snapshot now carries the end of the owner's latest
conversation, bounded, as the requester could read it anyway.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from flowly.live_voice import recent_chat
from flowly.live_voice.authority import RequestOwner, request_owner_scope
from flowly.live_voice.memory_snapshot import SNAPSHOT_BUDGET, VoiceMemorySnapshot
from flowly.live_voice.recent_chat import latest_chat
from flowly.memory.governance import GovernanceStore


def write_session(directory, key, rows, *, mtime, metadata=None):
    path = directory / (key.replace(":", "_") + ".jsonl")
    lines = [{"_type": "metadata", "session_key": key, "metadata": metadata or {}}, *rows]
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def say(role, content, **extra):
    return {"role": role, "content": content, **extra}


def open_call(ended=False):
    return {"kind": "voice", "title": "Voice chat", "voice": {"connections": [
        {"id": "c-1", "endedAt": "2026-10-04T15:00:00"},
        {"id": "c-2", "endedAt": "2026-10-04T15:20:00" if ended else None}]}}


@pytest.fixture
def sessions(tmp_path):
    directory = tmp_path / "sessions"
    directory.mkdir()
    return directory


def test_the_latest_owner_conversation_is_carried_in_its_own_words(sessions):
    write_session(sessions, "telegram:1", [say("user", "Older chat")], mtime=1_000,
                  metadata={"title": "Old"})
    write_session(sessions, "web:abc", [
        say("user", "Look at my last email"),
        say("assistant", "", tool_calls=[{"id": "t", "function": {"name": "email"}}]),
        say("tool", "raw mailbox json", name="email"),
        say("assistant", [{"type": "text", "text": "It is a Google security alert."}]),
        say("user", "Delete it then"),
        say("assistant", "Done, it is in the trash."),
    ], mtime=2_000, metadata={"title": "Google security alert"})
    # Newer, but none of these is a conversation to continue: the call that
    # is open (this one), a call's work, an agent room, scheduled runs.
    write_session(sessions, "desktop:voice:call-1", [say("user", "this call")], mtime=3_000,
                  metadata=open_call())
    for key in ("desktop:voice-work:c_1", "desktop:profile-inbox:x",
                "desktop:group:room", "cron:nightly", "heartbeat:main"):
        write_session(sessions, key, [say("user", "not this")], mtime=3_000)

    chat = latest_chat(sessions)
    assert chat.key == "web:abc" and chat.title == "Google security alert"
    assert chat.text == ("User: Look at my last email\n\nAgent: It is a Google security alert.\n\n"
                         "User: Delete it then\n\nAgent: Done, it is in the trash.")
    assert "mailbox" not in chat.text


def test_only_the_newest_messages_fit(sessions, monkeypatch):
    monkeypatch.setattr(recent_chat, "RECENT_CHAT_BYTES", 120)
    rows = [say("user" if n % 2 == 0 else "assistant", f"message number {n} " + "x" * 40) for n in range(30)]
    write_session(sessions, "ios:chat", rows, mtime=1_000)
    text = latest_chat(sessions).text
    assert "message number 29" in text and "message number 0 " not in text
    assert len(text.encode()) <= 120 + 80  # the newest message always stays whole


def test_a_long_message_is_cut_and_a_huge_file_is_read_from_its_end(sessions, monkeypatch):
    monkeypatch.setattr(recent_chat, "TAIL_BYTES", 4_096)
    rows = [say("user", "filler " * 200) for _ in range(40)] + [say("assistant", "word " * 400)]
    write_session(sessions, "android:chat", rows, mtime=1_000)
    text = latest_chat(sessions).text
    assert text.endswith(" …") and len(text.split("Agent: ", 1)[1]) <= recent_chat.MESSAGE_CHARS + 2


def test_another_accounts_conversation_is_never_carried(sessions):
    write_session(sessions, "web:mine", [say("user", "my chat")], mtime=1_000)
    write_session(sessions, "web:theirs", [say("user", "their chat")], mtime=2_000,
                  metadata={"voiceOwner": {"kind": "account", "uid": "account-b"}})
    with request_owner_scope(RequestOwner("account-a")):
        assert latest_chat(sessions).key == "web:mine"


def test_no_conversation_or_no_folder_carries_nothing(sessions, tmp_path):
    assert latest_chat(sessions) is None
    assert latest_chat(tmp_path / "absent") is None
    (sessions / "web_broken.jsonl").write_text("not json\n")
    assert latest_chat(sessions) is None


def test_the_snapshot_carries_it_after_memory_and_the_speaking_model_hears_the_topic(tmp_path, sessions):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    store = GovernanceStore(tmp_path / "memory_governance.sqlite3")
    store.add_item(kind="profile", text="Name is Hakan", status="active")
    store.add_item(kind="preference", text="Hakan is allergic to penicillin.", status="rejected")
    (workspace / "memory" / "MEMORY.md").write_text("The studio is on the 3rd floor.\n")
    write_session(sessions, "web:abc", [
        say("user", "Hakan is allergic to penicillin. Fix that, it is wrong."),
        say("assistant", "Fixed: you are not allergic. My key is sk-ant-api03-" + "a" * 40),
        say("user", "Thanks"),
    ], mtime=datetime(2026, 10, 4, 15, 14).timestamp(), metadata={"title": "Penicillin correction"})
    reader = VoiceMemorySnapshot(workspace, state_db=lambda name: tmp_path / name,
                                 profile=lambda: ("default", "bot-1"), search_enabled=lambda: True,
                                 sessions_dir=lambda: sessions)
    result = reader.snapshot({})
    kinds = [section["kind"] for section in result["sections"]]
    assert kinds == ["memory", "recent", "notes"]
    recent = result["sections"][1]
    assert recent["title"] == "Latest conversation: Penicillin correction (2026-10-04 15:14)"
    # Governance and secret redaction apply as to every other section: the
    # paragraph carrying a rejected item's words is left out.
    assert "allergic to penicillin. Fix" not in recent["text"] and "sk-ant" not in recent["text"]
    assert "Thanks" in recent["text"]
    assert result["profile"].startswith("Last talked about: Penicillin correction (2026-10-04 15:14)")
    assert sum(len(section["text"].encode()) for section in result["sections"]) <= SNAPSHOT_BUDGET
    store.close()


def test_without_a_sessions_folder_the_snapshot_is_unchanged(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "memory" / "MEMORY.md").write_text("A note.\n")
    reader = VoiceMemorySnapshot(workspace, state_db=lambda name: tmp_path / name,
                                 profile=lambda: ("default", "bot-1"), search_enabled=lambda: True)
    assert [section["kind"] for section in reader.snapshot({})["sections"]] == ["notes"]


def test_an_earlier_call_is_where_they_left_off_when_it_came_last(tmp_path, sessions):
    write_session(sessions, "telegram:1", [say("user", "Earlier chat")], mtime=1_000)
    write_session(sessions, "desktop:voice:call-1", [
        say("user", "Let's plan the trip to İzmir."),
        say("assistant", "Friday morning works; I will hold the 9:40 train."),
    ], mtime=datetime(2026, 10, 4, 15, 20).timestamp(), metadata=open_call(ended=True) | {"title": "İzmir trip"})
    chat = latest_chat(sessions)
    assert chat.is_call and chat.title == "İzmir trip" and "9:40 train" in chat.text

    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    reader = VoiceMemorySnapshot(workspace, state_db=lambda name: tmp_path / name,
                                 profile=lambda: ("default", "bot-1"), search_enabled=lambda: True,
                                 sessions_dir=lambda: sessions)
    result = reader.snapshot({})
    [recent] = [section for section in result["sections"] if section["kind"] == "recent"]
    assert recent["title"] == "Latest call: İzmir trip (2026-10-04 15:20)"
    assert result["profile"].startswith("Last talked about in a call: İzmir trip (2026-10-04 15:20)")


def test_another_accounts_call_is_never_carried(sessions):
    write_session(sessions, "web:mine", [say("user", "my chat")], mtime=1_000)
    theirs = open_call(ended=True) | {"voiceOwner": {"kind": "account", "uid": "account-b"}}
    write_session(sessions, "desktop:voice:theirs", [say("user", "their call")], mtime=2_000, metadata=theirs)
    with request_owner_scope(RequestOwner("account-a")):
        assert latest_chat(sessions).key == "web:mine"
