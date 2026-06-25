"""Tests for the single-writer Board store."""

from __future__ import annotations

import threading

import pytest

from flowly.board.store import (
    BoardError,
    BoardStore,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_TODO,
    STATUS_WAITING,
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
    store.set_status(live.id, STATUS_IN_PROGRESS)
    store.set_run_id(live.id, "run-live")
    store.set_status(dead.id, STATUS_IN_PROGRESS)
    store.set_run_id(dead.id, "run-dead")
    store.set_status(never.id, STATUS_IN_PROGRESS)  # null run_id

    reset = store.reset_orphaned(live_run_ids={"run-live"})
    assert reset == 2  # dead + never
    assert store.get_card(live.id).status == STATUS_IN_PROGRESS
    assert store.get_card(dead.id).status == STATUS_TODO
    assert store.get_card(never.id).status == STATUS_TODO
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
        STATUS_TODO, STATUS_IN_PROGRESS, STATUS_WAITING, STATUS_DONE
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
