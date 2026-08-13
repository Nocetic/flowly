"""Cross-process safety at the canonical session rewrite boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from flowly.session.manager import ConcurrentSessionWriteError, SessionManager


@pytest.fixture
def managers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly-home"))
    first = SessionManager(workspace=tmp_path / "workspace-a")
    second = SessionManager(workspace=tmp_path / "workspace-b")
    return first, second


def _seed(manager: SessionManager, key: str = "web:shared"):
    session = manager.get_or_create(key)
    session.add_message("user", "original question")
    session.add_message("assistant", "original answer")
    manager.save(session)
    return session


def test_stale_atomic_save_cannot_overwrite_a_newer_process(managers):
    first, second = managers
    live = _seed(first)
    stale = second.get_or_create(live.key)

    live.add_message("user", "newer process message")
    first.save(live)
    stale.add_message("user", "stale process message")

    with pytest.raises(ConcurrentSessionWriteError, match="stale session revision"):
        second.save(stale)

    third = SessionManager(workspace=first.workspace).get_or_create(live.key)
    contents = [message.get("content") for message in third.messages]
    assert "newer process message" in contents
    assert "stale process message" not in contents


def test_compaction_guard_carries_a_tail_appended_by_another_process(managers):
    compactor, writer = managers
    source = _seed(compactor)
    source_count = len(source.messages)
    fingerprint = compactor.source_fingerprint(source.messages, source_count)

    current = writer.get_or_create(source.key)
    current.add_message("user", "arrived while summary was running")
    writer.save(current)

    with compactor.compaction_commit_guard(
        source,
        source_message_count=source_count,
        source_fingerprint=fingerprint,
        fence=1,
    ):
        pass

    assert source.messages[-1]["content"] == "arrived while summary was running"
    assert source.metadata["_compaction_fence"] == 1


def test_compaction_guard_rejects_a_changed_source_prefix(managers):
    compactor, editor = managers
    source = _seed(compactor)
    source_count = len(source.messages)
    fingerprint = compactor.source_fingerprint(source.messages, source_count)

    changed = editor.get_or_create(source.key)
    changed.messages[0]["content"] = "edited original question"
    editor.save(changed)

    with pytest.raises(ConcurrentSessionWriteError, match="history changed"):
        with compactor.compaction_commit_guard(
            source,
            source_message_count=source_count,
            source_fingerprint=fingerprint,
            fence=1,
        ):
            pass


def test_fence_prevents_an_older_lease_from_committing(managers):
    manager, _ = managers
    session = _seed(manager)
    count = len(session.messages)
    fingerprint = manager.source_fingerprint(session.messages, count)

    with manager.compaction_commit_guard(
        session,
        source_message_count=count,
        source_fingerprint=fingerprint,
        fence=4,
    ) as guard:
        guard.save(session)

    with pytest.raises(ConcurrentSessionWriteError, match="fence is stale"):
        with manager.compaction_commit_guard(
            session,
            source_message_count=count,
            source_fingerprint=fingerprint,
            fence=3,
        ):
            pass


def test_revision_is_monotonic_across_atomic_saves(managers):
    manager, _ = managers
    session = _seed(manager)
    first_revision = session.metadata["_session_revision"]
    session.add_message("user", "next")
    manager.save(session)

    assert session.metadata["_session_revision"] == first_revision + 1
