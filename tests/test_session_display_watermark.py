"""The display transcript must not repeat rows after a fresh load.

The defect: ``_save_unlocked`` wrote the canonical file, then advanced the
display-log watermark only in memory. Any later load from disk — a gateway
restart, ``SessionManager.mutate``, another manager in the same process — saw
the stale count and appended the previous save's rows to the append-only
display log again, so clients showed the last turn twice.
"""

from __future__ import annotations

import json

from flowly.session.manager import SessionManager


def _contents(manager: SessionManager, key: str) -> list[str]:
    return [message["content"] for message in manager.get_full_messages(key)]


def test_restart_does_not_repeat_the_last_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    first = SessionManager(tmp_path / "ws")
    session = first.get_or_create("web:chat")
    session.add_message("user", "one")
    session.add_message("assistant", "two")
    first.save(session)
    session.add_message("user", "three")
    first.save(session)

    restarted = SessionManager(tmp_path / "ws")
    again = restarted.get_or_create("web:chat")
    again.add_message("assistant", "four")
    restarted.save(again)
    assert _contents(restarted, "web:chat") == ["one", "two", "three", "four"]


def test_mutate_and_other_managers_append_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    owner = SessionManager(tmp_path / "ws")
    session = owner.get_or_create("web:chat")
    session.add_message("user", "one")
    owner.save(session)
    SessionManager(tmp_path / "ws").mutate("web:chat", lambda s: s.add_message("assistant", "two"))
    owner.refresh(session)
    session.add_message("user", "three")
    owner.save(session)
    assert _contents(SessionManager(tmp_path / "ws"), "web:chat") == ["one", "two", "three"]


def test_persisted_watermark_matches_the_display_log(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    manager = SessionManager(tmp_path / "ws")
    session = manager.get_or_create("web:chat")
    for text in ("a", "b", "c"):
        session.add_message("user", text)
        manager.save(session)
    header = json.loads(manager._get_session_path("web:chat").read_text().splitlines()[0])
    assert header["metadata"]["_full_log_count"] == 3
    rows = [row for row in manager._read_full_rows("web:chat") if row.get("role")]
    assert [row["content"] for row in rows] == ["a", "b", "c"]
