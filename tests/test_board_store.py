"""Tests for the single-writer Board store."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from flowly.board.store import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_READY,
    STATUS_REVIEW,
    STATUS_TODO,
    STATUS_WAITING,
    BoardError,
    BoardStore,
)


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / "board.db")
    yield s
    s.close()


def test_add_and_get_card(store):
    card = store.add_card(
        "buy milk", origin_channel="telegram", origin_chat_id="42", created_by="user"
    )
    assert card.id.startswith("c_")
    assert card.title == "buy milk"
    assert card.status == STATUS_TODO
    assert card.origin_channel == "telegram"
    assert card.origin_chat_id == "42"
    assert card.created_at > 0

    fetched = store.get_card(card.id)
    assert fetched is not None
    assert fetched.title == "buy milk"


def test_add_card_rejects_empty_title(store):
    with pytest.raises(BoardError):
        store.add_card("   ")


def test_add_card_rejects_bad_status(store):
    with pytest.raises(BoardError):
        store.add_card("x", status="nonsense")


def test_origin_captured_per_card(store):
    a = store.add_card("a", origin_channel="telegram", origin_chat_id="1")
    b = store.add_card("b", origin_channel="discord", origin_chat_id="2")
    assert store.get_card(a.id).origin_channel == "telegram"
    assert store.get_card(b.id).origin_chat_id == "2"


def test_status_transitions(store):
    card = store.add_card("task")
    moved = store.set_status(card.id, STATUS_IN_PROGRESS)
    assert moved.status == STATUS_IN_PROGRESS
    assert moved.updated_at >= card.updated_at

    done = store.set_status(card.id, STATUS_DONE, result="all good")
    assert done.status == STATUS_DONE
    assert done.result == "all good"


def test_set_status_missing_card(store):
    with pytest.raises(BoardError):
        store.set_status("c_nope", STATUS_DONE)


def test_terminal_status_clears_run_id(store):
    card = store.add_card("task")
    store.set_run_id(card.id, "run-123")
    assert store.get_card(card.id).run_id == "run-123"
    done = store.set_status(card.id, STATUS_DONE)
    assert done.run_id is None


def test_waiting_keeps_run_id(store):
    card = store.add_card("task")
    store.set_run_id(card.id, "run-xyz")
    waiting = store.set_status(card.id, STATUS_WAITING)
    assert waiting.run_id == "run-xyz"


def test_list_filters_by_status(store):
    store.add_card("a")
    store.add_card("b")
    c = store.add_card("c")
    store.set_status(c.id, STATUS_DONE)

    todos = store.list_cards(status=STATUS_TODO)
    assert {x.title for x in todos} == {"a", "b"}
    dones = store.list_cards(status=STATUS_DONE)
    assert [x.title for x in dones] == ["c"]


def test_parent_child(store):
    parent = store.add_card("goal")
    child = store.add_card("subtask", parent_id=parent.id)
    assert child.parent_id == parent.id
    children = store.list_cards(parent_id=parent.id)
    assert [c.title for c in children] == ["subtask"]


def test_parent_must_exist(store):
    with pytest.raises(BoardError):
        store.add_card("orphan", parent_id="c_missing")


def test_notes_and_cascade(store):
    card = store.add_card("task")
    store.add_note(card.id, "user", "first note")
    store.add_note(card.id, "agent", "second note")
    fetched = store.get_card(card.id)
    assert [n.text for n in fetched.notes] == ["first note", "second note"]

    assert store.delete_card(card.id) is True
    assert store.get_card(card.id) is None
    # cascade removed notes
    assert store.list_cards() == []


def test_note_empty_rejected(store):
    card = store.add_card("task")
    with pytest.raises(BoardError):
        store.add_note(card.id, "user", "  ")


def test_reset_orphaned(store):
    live = store.add_card("live")
    dead = store.add_card("dead")
    never = store.add_card("never-claimed")
    leased = store.add_card(
        "leased profile worker",
        status=STATUS_READY,
        assignee_profile="research",
        assignee_bot_id="bot-1",
    )
    store.set_status(live.id, STATUS_IN_PROGRESS)
    store.set_run_id(live.id, "run-live")
    store.set_status(dead.id, STATUS_IN_PROGRESS)
    store.set_run_id(dead.id, "run-dead")
    store.set_status(never.id, STATUS_IN_PROGRESS)  # null run_id
    claimed = store.claim_card(leased.id, worker="research", lease_seconds=30)
    assert claimed is not None and claimed.claim_token

    reset = store.reset_orphaned(live_run_ids={"run-live"})
    assert reset == 2  # dead + never
    assert store.get_card(live.id).status == STATUS_IN_PROGRESS
    assert store.get_card(dead.id).status == STATUS_TODO
    assert store.get_card(never.id).status == STATUS_TODO
    assert store.get_card(leased.id).status == STATUS_IN_PROGRESS
    # explanatory note added
    assert any("restart" in n.text for n in store.get_card(dead.id).notes)


def test_delete_by_status(store):
    a = store.add_card("a")
    b = store.add_card("b")
    c = store.add_card("c")
    store.set_status(a.id, STATUS_DONE)
    store.set_status(b.id, STATUS_DONE)
    # c stays todo
    removed = store.delete_by_status(STATUS_DONE)
    assert removed == 2
    assert store.get_card(a.id) is None
    assert store.get_card(b.id) is None
    assert store.get_card(c.id) is not None


def test_delete_by_status_bad(store):
    with pytest.raises(BoardError):
        store.delete_by_status("nonsense")


def test_snapshot_shape(store):
    store.add_card("t1")
    ip = store.add_card("t2")
    store.set_status(ip.id, STATUS_IN_PROGRESS)
    d = store.add_card("t3")
    store.set_status(d.id, STATUS_DONE)

    snap = store.snapshot()
    assert [col["status"] for col in snap["columns"]] == [
        STATUS_TODO,
        STATUS_READY,
        STATUS_IN_PROGRESS,
        STATUS_WAITING,
        STATUS_REVIEW,
        STATUS_BLOCKED,
        STATUS_DONE,
    ]
    assert snap["counts"][STATUS_TODO] == 1
    assert snap["counts"][STATUS_IN_PROGRESS] == 1
    assert snap["counts"][STATUS_DONE] == 1
    assert snap["total"] == 3
    assert snap["timestampMs"] > 0
    # camelCase mirror present for JS clients
    todo_cards = snap["columns"][0]["cards"]
    assert todo_cards[0]["originChannel"] == ""
    assert "createdAt" in todo_cards[0]


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "board.db"
    s1 = BoardStore(path)
    card = s1.add_card("persist me", origin_channel="cli")
    s1.close()

    s2 = BoardStore(path)
    fetched = s2.get_card(card.id)
    assert fetched is not None
    assert fetched.title == "persist me"
    s2.close()


def test_concurrent_add_is_consistent(store):
    """Many threads adding cards under the lock → no lost writes / corruption."""
    n = 50

    def worker(i):
        store.add_card(f"card-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    cards = store.list_cards(limit=1000)
    assert len(cards) == n
    assert len({c.id for c in cards}) == n  # all ids unique


def test_idempotent_add_returns_original_card(store):
    first = store.add_card("task", idempotency_key="request-1")
    second = store.add_card("different title", idempotency_key="request-1")

    assert second.id == first.id
    assert second.title == "task"
    assert store.snapshot()["total"] == 1


def test_assignment_uses_revision_cas(store):
    card = store.add_card("task")
    assigned = store.assign_card(
        card.id,
        profile="research",
        bot_id="bot-1",
        expected_revision=card.revision,
    )

    # Assignment chooses the worker; it does not start or queue the card.
    assert assigned.status == STATUS_TODO
    assert assigned.assignee_profile == "research"
    assert assigned.assignee_bot_id == "bot-1"
    with pytest.raises(BoardError, match="changed"):
        store.assign_card(
            card.id,
            profile="writer",
            bot_id="bot-2",
            expected_revision=card.revision,
        )


def test_claim_heartbeat_retry_and_stale_completion(store):
    card = store.add_card(
        "task",
        assignee_profile="research",
        assignee_bot_id="bot-1",
        status=STATUS_READY,
        max_attempts=2,
    )
    claimed = store.claim_card(card.id, worker="research", lease_seconds=30)
    assert claimed is not None and claimed.claim_token
    assert claimed.status == STATUS_IN_PROGRESS
    assert store.heartbeat_claim(card.id, claimed.claim_token, lease_seconds=30)

    retry = store.finish_claim(
        card.id,
        claimed.claim_token,
        outcome="failed",
        error="temporary",
    )
    assert retry.status == STATUS_READY
    assert retry.attempt_count == 1
    with pytest.raises(BoardError, match="stale"):
        store.finish_claim(card.id, claimed.claim_token, outcome="done", result="late")

    claimed_again = store.claim_card(card.id, worker="research", lease_seconds=30)
    assert claimed_again is not None and claimed_again.claim_token
    blocked = store.finish_claim(
        card.id,
        claimed_again.claim_token,
        outcome="failed",
        error="permanent",
    )
    assert blocked.status == STATUS_BLOCKED
    assert len(store.get_runs(card.id)) == 2
    assert any(item["kind"] == "run_finished" for item in store.get_events(card.id))


def test_live_claim_repairs_legacy_status_rewrite(store):
    card = store.add_card(
        "task",
        assignee_profile="research",
        assignee_bot_id="bot-1",
        status=STATUS_READY,
    )
    claimed = store.claim_card(card.id, worker="research", lease_seconds=30)
    assert claimed is not None and claimed.claim_token

    # Simulate a pre-lease runtime's startup recovery. It knows only the old
    # status/run_id columns, so it leaves the authoritative claim intact.
    store._conn.execute(  # noqa: SLF001 - cross-version recovery regression
        "UPDATE cards SET status = ?, run_id = NULL WHERE id = ?",
        (STATUS_TODO, card.id),
    )
    store._conn.commit()  # noqa: SLF001

    assert store.claim_card(card.id, worker="research", lease_seconds=30) is None
    with pytest.raises(BoardError, match="running card"):
        store.assign_card(card.id, profile="writer", bot_id="bot-2")
    with pytest.raises(BoardError, match="running card"):
        store.delete_card(card.id)
    with pytest.raises(BoardError, match="running cards"):
        store.delete_by_status(STATUS_TODO)
    assert store.heartbeat_claim(card.id, claimed.claim_token, lease_seconds=30)
    repaired = store.get_card(card.id)
    assert repaired is not None and repaired.status == STATUS_IN_PROGRESS
    assert any(
        item["kind"] == "claim_state_repaired"
        for item in store.get_events(card.id)
    )

    # A matching claim token remains authoritative even if the legacy writer
    # races once more just before the worker completes.
    store._conn.execute(  # noqa: SLF001
        "UPDATE cards SET status = ?, run_id = NULL WHERE id = ?",
        (STATUS_TODO, card.id),
    )
    store._conn.commit()  # noqa: SLF001
    done = store.finish_claim(
        card.id,
        claimed.claim_token,
        outcome="done",
        result="finished",
    )
    assert done.status == STATUS_DONE
    assert done.result == "finished"


def test_recovery_restores_unexpired_claim_corrupted_by_legacy_writer(store):
    card = store.add_card(
        "task",
        assignee_profile="research",
        assignee_bot_id="bot-1",
        status=STATUS_READY,
    )
    claimed = store.claim_card(card.id, worker="research", lease_seconds=30)
    assert claimed is not None and claimed.claim_token
    store._conn.execute(  # noqa: SLF001
        "UPDATE cards SET status = ?, run_id = NULL WHERE id = ?",
        (STATUS_TODO, card.id),
    )
    store._conn.commit()  # noqa: SLF001

    assert store.recover_expired_claims(now=claimed.heartbeat_at + 1) == 1
    repaired = store.get_card(card.id)
    assert repaired is not None
    assert repaired.status == STATUS_IN_PROGRESS
    assert repaired.run_id is not None


def test_dependency_blocks_dispatch_until_parent_done(store):
    parent = store.add_card("parent")
    child = store.add_card(
        "child",
        status=STATUS_READY,
        assignee_profile="research",
        assignee_bot_id="bot-1",
    )
    store.link_cards(parent.id, child.id)

    assert store.list_dispatchable() == []
    store.set_status(parent.id, STATUS_DONE)
    assert [card.id for card in store.list_dispatchable()] == [child.id]


def test_dependencies_reject_cycles_and_duplicate_audit(store):
    first = store.add_card("first")
    second = store.add_card("second")
    third = store.add_card("third")

    store.link_cards(first.id, second.id)
    store.link_cards(second.id, third.id)
    with pytest.raises(BoardError, match="cycle"):
        store.link_cards(third.id, first.id)

    before = len(store.get_events(second.id))
    store.link_cards(first.id, second.id)
    assert len(store.get_events(second.id)) == before


def test_dispatchable_returns_at_most_one_card_per_profile(store):
    for title in ("first", "second"):
        store.add_card(
            title,
            status=STATUS_READY,
            assignee_profile="research",
            assignee_bot_id="bot-research",
        )
    writer = store.add_card(
        "writer",
        status=STATUS_READY,
        assignee_profile="writer",
        assignee_bot_id="bot-writer",
    )

    dispatchable = store.list_dispatchable()

    assert len(dispatchable) == 2
    assert {card.assignee_profile for card in dispatchable} == {"research", "writer"}
    assert store.list_dispatchable(exclude_profiles=("research",)) == [writer]


@pytest.mark.parametrize("scheduled_at", [float("nan"), float("inf"), float("-inf")])
def test_add_card_rejects_non_finite_schedule(store, scheduled_at):
    with pytest.raises(BoardError, match="scheduled time"):
        store.add_card("scheduled", scheduled_at=scheduled_at)


def test_existing_board_schema_migrates_without_data_loss(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE cards (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, origin_channel TEXT NOT NULL DEFAULT '',
            origin_chat_id TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT 'user',
            run_id TEXT, parent_id TEXT, result TEXT, error TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE card_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            author TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL
        );
        INSERT INTO cards VALUES (
            'c_legacy', 'legacy task', '', 'todo', '', '', 'user',
            NULL, NULL, NULL, NULL, 1.0, 1.0
        );
    """)
    conn.commit()
    conn.close()

    migrated = BoardStore(path)
    try:
        card = migrated.get_card("c_legacy")
        assert card is not None
        assert card.title == "legacy task"
        assert card.revision == 0
        assert card.max_attempts == 2
        assigned = migrated.assign_card(
            card.id,
            profile="research",
            bot_id="bot-1",
        )
        assert assigned.status == STATUS_TODO
    finally:
        migrated.close()
