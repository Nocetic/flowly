"""A conversation's row says its last words (sessions.list `lastMessage`).

The apps that read a home's chats from its agent (a direct gateway, a home
that keeps no chats on the server) show them in the list and on the agent's
row on Home; without them iOS said "No messages yet" for an agent that had
talked all day.
"""

from __future__ import annotations

import json

from flowly.channels.feature_rpc import sessions_list
from flowly.session.manager import SessionManager
from flowly.session.preview import last_message_preview


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    return home, SessionManager(home / "workspace")


def _row(key):
    return next(row for row in sessions_list({})["sessions"] if row["key"] == key)


def test_a_row_says_the_last_words_a_person_sees(tmp_path, monkeypatch):
    _home_dir, manager = _home(tmp_path, monkeypatch)
    chat = manager.get_or_create("web:c1")
    chat.add_message("user", "plan my   week")
    chat.add_message("assistant", "Monday is free.\nTuesday is busy.")
    manager.save(chat)
    row = _row("web:c1")
    assert (row["lastMessage"], row["lastMessageRole"]) == ("Monday is free. Tuesday is busy.", "assistant")
    # Kept in the header: a list reads it without reading the conversation.
    header = json.loads(next(_home_dir.rglob("web_c1.jsonl")).read_text().splitlines()[0])
    assert header["metadata"]["last_message"] == {"role": "assistant", "text": "Monday is free. Tuesday is busy."}

    # A note the app added and a step that only called tools are not words anyone said.
    chat.add_message("assistant", "", tool_calls=[{"id": "t1", "type": "function", "function": {"name": "x", "arguments": "{}"}}])
    chat.add_message("user", "The user reacted ❤️ to your message.", _display_hidden=True)
    manager.save(chat)
    assert _row("web:c1")["lastMessage"] == "Monday is free. Tuesday is busy."


def test_a_chat_saved_before_rows_kept_their_last_words_is_read_from_its_end(tmp_path, monkeypatch):
    home, manager = _home(tmp_path, monkeypatch)
    chat = manager.get_or_create("web:old")
    chat.add_message("user", "an old question")
    chat.add_message("assistant", "an old answer")
    manager.save(chat)
    for path in home.rglob("web_old*.jsonl"):
        lines = path.read_text().splitlines()
        header = json.loads(lines[0])
        header.get("metadata", {}).pop("last_message", None)
        path.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n")
    assert _row("web:old")["lastMessage"] == "an old answer"


def test_a_chat_with_nothing_said_has_no_last_words():
    assert last_message_preview([]) is None
    assert last_message_preview([{"role": "assistant", "content": [{"type": "text", "text": "   "}]}]) is None
    long = last_message_preview([{"role": "user", "content": "x" * 500}])
    assert long and len(long["text"]) == 160
