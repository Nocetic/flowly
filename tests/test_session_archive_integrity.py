"""Enterprise invariants for append-only conversation lineage."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from flowly.agent.loop import AgentLoop
from flowly.compaction.types import CompactionResult, is_summary_message
from flowly.session.archive import (
    ARCHIVE_STATE_KEY,
    ARCHIVE_SUMMARY_KEY,
    ARCHIVE_TRANSITION_TYPE,
    EVENT_ID_KEY,
    EVENT_SEQ_KEY,
)
from flowly.session.indexer import SessionIndexer
from flowly.session.manager import SessionManager


@pytest.fixture
def archive_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "flowly-home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    from flowly import profile

    if hasattr(profile, "_cached_home"):
        profile._cached_home = None
    return home


def _manager() -> SessionManager:
    return SessionManager(workspace=Path("/tmp"))


def _seed(manager: SessionManager, key: str = "web:archive"):
    session = manager.get_or_create(key)
    for index in range(3):
        session.add_message("user", f"question {index}")
        session.add_message("assistant", f"answer {index}")
    manager.save(session)
    return session


class _Context:
    def invalidate_memory_snapshot(self, key: str) -> None:
        pass


class _CommitHarness:
    _commit_compaction = AgentLoop._commit_compaction
    _context_coverage_sidecar = AgentLoop._context_coverage_sidecar

    def __init__(self, manager: SessionManager):
        self.sessions = manager
        self.context = _Context()
        self._last_turn_total_tokens: dict[str, int] = {}


def _compact(manager: SessionManager, session):
    kept = session.get_history()[-2:]
    result = CompactionResult(
        summary="Questions zero and one were answered.",
        tokens_before=2_000,
        tokens_after=200,
        messages_removed=4,
        kept_messages=kept,
    )
    source_count = len(session.messages)
    _CommitHarness(manager)._commit_compaction(
        session,
        result,
        session.key,
        source_message_count=source_count,
        compaction_id="cmp_archive_test",
    )


def test_event_identity_is_stable_and_never_reaches_provider_shape(archive_home):
    manager = _manager()
    session = _seed(manager)
    before = [message[EVENT_ID_KEY] for message in session.messages]

    manager._cache.clear()
    resumed = manager.get_or_create(session.key)

    assert [message[EVENT_ID_KEY] for message in resumed.messages] == before
    assert [message[EVENT_SEQ_KEY] for message in resumed.messages] == list(range(1, 7))
    assert all(
        EVENT_ID_KEY not in message and EVENT_SEQ_KEY not in message
        for message in resumed.get_history()
    )


def test_compaction_is_an_append_only_state_transition_with_zero_gap(archive_home):
    manager = _manager()
    session = _seed(manager)
    archive_path = manager._get_full_path(session.key)
    before_lines = archive_path.read_text(encoding="utf-8").splitlines()

    _compact(manager, session)

    after_lines = archive_path.read_text(encoding="utf-8").splitlines()
    assert after_lines[: len(before_lines)] == before_lines
    rows = [json.loads(line) for line in after_lines]
    transitions = [
        row for row in rows if row.get("_type") == ARCHIVE_TRANSITION_TYPE
    ]
    assert len(transitions) == 1
    assert transitions[0]["state"] == "compacted"
    assert transitions[0]["compaction_id"] == "cmp_archive_test"

    manifest = manager.context_coverage(session)
    assert manifest.coverage_gap == 0
    assert manifest.compacted_events == 4
    assert manifest.active_events == 2
    assert manifest.internal_hidden_events == 1  # derived summary
    assert manifest.represented_events == manifest.total_events
    assert is_summary_message(session.messages[0])
    assert session.messages[0][ARCHIVE_SUMMARY_KEY] is True

    visible = manager.get_full_messages(session.key)
    assert [row.get("content") for row in visible if row.get("role") == "user"] == [
        "question 0", "question 1", "question 2",
    ]
    assert not any(row.get(ARCHIVE_SUMMARY_KEY) for row in visible)
    assert sum(row.get("kind") == "context_boundary" for row in visible) == 1

    sidecar = _CommitHarness(manager)._context_coverage_sidecar(session)
    assert 'status="complete"' in sidecar
    assert 'coverage_gap="0"' in sidecar
    assert session.metadata["context_coverage"]["coverage_gap"] == 0


def test_coverage_gap_is_detected_instead_of_silently_claiming_completeness(
    archive_home,
):
    manager = _manager()
    session = _seed(manager)
    manager._append_archive_row(
        session.key,
        {
            "role": "user",
            "content": "persisted but absent from working context",
            "timestamp": "2026-01-01T00:00:00",
            EVENT_ID_KEY: "evt_gap",
            EVENT_SEQ_KEY: 999,
            ARCHIVE_STATE_KEY: "active",
        },
    )

    manifest = manager.context_coverage(session)
    sidecar = _CommitHarness(manager)._context_coverage_sidecar(session)

    assert manifest.coverage_gap == 1
    assert 'status="INCOMPLETE"' in sidecar
    assert "Do not infer the first message" in sidecar


def test_prepared_transition_is_invisible_when_canonical_swap_fails(
    archive_home, monkeypatch,
):
    manager = _manager()
    session = _seed(manager)
    before_messages = [dict(message) for message in session.messages]
    before_metadata = dict(session.metadata)

    def fail_save(_session):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        _compact(manager, session)

    assert session.messages == before_messages
    assert session.metadata == before_metadata
    snapshot = manager.get_archive_snapshot(session.key)
    assert {event.state for event in snapshot.events} == {"active"}


def test_pending_transaction_is_committed_idempotently_on_reload(
    archive_home, monkeypatch,
):
    manager = _manager()
    session = _seed(manager)
    original_finalize = manager.finalize_compaction_archive

    def fail_finalize(_session, _transaction_id):
        raise OSError("commit marker interrupted")

    monkeypatch.setattr(manager, "finalize_compaction_archive", fail_finalize)
    _compact(manager, session)
    assert session.metadata.get("_pending_archive_transaction")
    assert manager.context_coverage(session).coverage_gap > 0

    monkeypatch.setattr(manager, "finalize_compaction_archive", original_finalize)
    manager._cache.clear()
    resumed = manager.get_or_create(session.key)

    assert "_pending_archive_transaction" not in resumed.metadata
    assert manager.context_coverage(resumed).coverage_gap == 0
    rows = manager._read_full_rows(session.key)
    commits = [row for row in rows if row.get("_type") == "archive_commit"]
    assert len(commits) == 1


def test_undo_appends_withdrawal_and_hides_rows_without_rewriting(archive_home):
    manager = _manager()
    session = _seed(manager)
    archive_path = manager._get_full_path(session.key)
    before = archive_path.read_text(encoding="utf-8")

    assert session.drop_last_turn() == "question 2"
    manager.save(session)

    after = archive_path.read_text(encoding="utf-8")
    assert after.startswith(before)
    snapshot = manager.get_archive_snapshot(session.key)
    assert [event.state for event in snapshot.events[-2:]] == [
        "withdrawn", "withdrawn",
    ]
    assert all(
        row.get("content") not in {"question 2", "answer 2"}
        for row in manager.get_full_messages(session.key)
    )


def test_legacy_rows_gain_deterministic_identity_without_archive_rewrite(archive_home):
    manager = _manager()
    key = "web:legacy"
    messages = [
        {"role": "user", "content": "old question", "timestamp": "2025-01-01"},
        {"role": "assistant", "content": "old answer", "timestamp": "2025-01-02"},
    ]
    canonical = manager._get_session_path(key)
    archive = manager._get_full_path(key)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "_type": "metadata",
        "created_at": "2025-01-01T00:00:00",
        "updated_at": "2025-01-02T00:00:00",
        "metadata": {},
    }
    canonical.write_text(
        "\n".join(json.dumps(row) for row in [metadata, *messages]) + "\n",
        encoding="utf-8",
    )
    archive.write_text(
        "\n".join(json.dumps(row) for row in messages) + "\n",
        encoding="utf-8",
    )
    before = archive.read_text(encoding="utf-8")

    session = manager.get_or_create(key)
    first_ids = [message[EVENT_ID_KEY] for message in session.messages]
    manager.save(session)
    manager._cache.clear()
    resumed = manager.get_or_create(key)

    assert all(event_id.startswith("evt_legacy_") for event_id in first_ids)
    assert [message[EVENT_ID_KEY] for message in resumed.messages] == first_ids
    assert archive.read_text(encoding="utf-8") == before


def test_archive_index_preserves_row_ids_when_compaction_changes_state(archive_home):
    manager = _manager()
    indexer = SessionIndexer(archive_home / "session_index.sqlite")
    manager._indexer = indexer
    session = _seed(manager)
    before = indexer._conn.execute(
        "SELECT id, event_id FROM messages WHERE session_key = ? ORDER BY id",
        (session.key,),
    ).fetchall()

    _compact(manager, session)
    after = indexer._conn.execute(
        "SELECT id, event_id, state FROM messages "
        "WHERE session_key = ? ORDER BY id",
        (session.key,),
    ).fetchall()

    assert [(row["id"], row["event_id"]) for row in after] == [
        (row["id"], row["event_id"]) for row in before
    ]
    assert [row["state"] for row in after] == [
        "compacted", "compacted", "compacted", "compacted", "active", "active",
    ]
    indexer.close()


def test_legacy_index_migrates_in_place_without_losing_fts_or_row_ids(
    archive_home,
):
    """An upgrade must enrich an old search DB, never rebuild its rows."""
    db_path = archive_home / "legacy_session_index.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            key TEXT PRIMARY KEY,
            created_at REAL,
            updated_at REAL,
            msg_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL
        );
        CREATE INDEX idx_messages_session
            ON messages(session_key, timestamp);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content,
            content=messages,
            content_rowid=id
        );
        CREATE TRIGGER fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER fts_delete AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES('delete', old.id, old.content);
        END;
        """
    )
    connection.execute(
        "INSERT INTO sessions (key, created_at, updated_at, msg_count) "
        "VALUES (?, ?, ?, ?)",
        ("web:migrated", 1.0, 1.0, 1),
    )
    connection.execute(
        "INSERT INTO messages (session_key, role, content, timestamp) "
        "VALUES (?, ?, ?, ?)",
        ("web:migrated", "user", "legacy migration needle", 1.0),
    )
    legacy_row_id = connection.execute(
        "SELECT id FROM messages WHERE session_key = ?",
        ("web:migrated",),
    ).fetchone()[0]
    connection.commit()
    connection.close()

    indexer = SessionIndexer(db_path)
    columns = {
        row[1]
        for row in indexer._conn.execute("PRAGMA table_info(messages)").fetchall()
    }
    assert {"event_id", "event_seq", "state"} <= columns

    archive_rows = [
        {
            "role": "user",
            "content": "legacy migration needle",
            "timestamp": "2026-01-01T00:00:00+00:00",
            EVENT_ID_KEY: "evt_migrated_1",
            EVENT_SEQ_KEY: 1,
            ARCHIVE_STATE_KEY: "compacted",
        },
        {
            "role": "assistant",
            "content": "new archive tail",
            "timestamp": "2026-01-01T00:00:01+00:00",
            EVENT_ID_KEY: "evt_migrated_2",
            EVENT_SEQ_KEY: 2,
            ARCHIVE_STATE_KEY: "active",
        },
    ]
    indexer.index_archive("web:migrated", archive_rows)

    stored = indexer._conn.execute(
        "SELECT id, event_id, event_seq, state, content FROM messages "
        "WHERE session_key = ? ORDER BY event_seq",
        ("web:migrated",),
    ).fetchall()
    assert [row["content"] for row in stored] == [
        "legacy migration needle",
        "new archive tail",
    ]
    assert stored[0]["id"] == legacy_row_id
    assert stored[0]["event_id"] == "evt_migrated_1"
    assert stored[0]["state"] == "compacted"
    assert stored[1]["event_id"] == "evt_migrated_2"
    assert stored[1]["state"] == "active"
    assert indexer.search("needle", include_bookends=False)[0]["anchor_id"] == legacy_row_id
    assert indexer.search("archive tail", include_bookends=False)
    indexer.close()
