"""Tests for the memory governance store (P0): CRUD, status machine, audit."""

from __future__ import annotations

import threading

import pytest

from flowly.memory.governance import (
    ACTOR_DREAMER,
    ACTOR_USER,
    GovernanceError,
    GovernanceStore,
    InvalidTransition,
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    STATUS_STALE,
    STATUS_SUPERSEDED,
)


@pytest.fixture
def store(tmp_path):
    s = GovernanceStore(tmp_path / "memory_governance.sqlite3")
    yield s
    s.close()


# -- CRUD -------------------------------------------------------------------


def test_add_and_get_item(store):
    item = store.add_item(
        kind="preference",
        text="prefers dark mode",
        normalized_key="ui:theme",
        confidence=0.9,
        source_session="telegram:42",
        source_message_ids=["m1", "m2"],
    )
    assert item.id.startswith("m_")
    assert item.status == STATUS_CANDIDATE  # default
    assert item.created_at and item.updated_at
    assert item.last_seen_at is not None

    fetched = store.get_item(item.id)
    assert fetched is not None
    assert fetched.text == "prefers dark mode"
    assert fetched.normalized_key == "ui:theme"
    assert fetched.confidence == 0.9
    assert fetched.source_message_ids == ["m1", "m2"]  # JSON round-trip


def test_add_item_validates_enums(store):
    with pytest.raises(GovernanceError):
        store.add_item(kind="bogus", text="x")
    with pytest.raises(GovernanceError):
        store.add_item(kind="fact", text="x", ref_kind="bogus")
    with pytest.raises(GovernanceError):
        store.add_item(kind="fact", text="x", privacy_level="bogus")
    with pytest.raises(GovernanceError):
        store.add_item(kind="fact", text="x", status="bogus")


def test_fact_item_references_kg_triple(store):
    item = store.add_item(
        kind="fact",
        text="Hakan works_at Nocetic Limited",
        ref_kind="kg_triple",
        ref_id="t_hakan_works_at_nocetic_ab12",
        normalized_key="hakan|works_at",
    )
    found = store.find_by_ref("kg_triple", "t_hakan_works_at_nocetic_ab12")
    assert [i.id for i in found] == [item.id]


def test_to_dict_has_camelcase_mirror(store):
    item = store.add_item(kind="fact", text="x", ref_kind="kg_triple", ref_id="t1")
    d = item.to_dict()
    assert d["refKind"] == "kg_triple"
    assert d["refId"] == "t1"
    assert "sourceMessageIds" in d and "privacyLevel" in d


# -- status machine ---------------------------------------------------------


def test_legal_transition_candidate_to_active(store):
    item = store.add_item(kind="preference", text="x")
    updated = store.transition(
        item.id, STATUS_ACTIVE, actor=ACTOR_DREAMER, reason="high confidence"
    )
    assert updated.status == STATUS_ACTIVE


def test_illegal_transition_rejected(store):
    item = store.add_item(kind="preference", text="x")
    store.transition(item.id, STATUS_REJECTED, actor=ACTOR_USER)
    # rejected is terminal — no outgoing transitions
    with pytest.raises(InvalidTransition):
        store.transition(item.id, STATUS_ACTIVE, actor=ACTOR_USER)


def test_illegal_transition_does_not_mutate(store):
    item = store.add_item(kind="preference", text="x")
    store.transition(item.id, STATUS_ACTIVE)
    # active → candidate is not allowed
    with pytest.raises(InvalidTransition):
        store.transition(item.id, STATUS_CANDIDATE)
    assert store.get_item(item.id).status == STATUS_ACTIVE


def test_same_status_is_idempotent_noop(store):
    item = store.add_item(kind="preference", text="x")
    store.transition(item.id, STATUS_ACTIVE)
    store.transition(item.id, STATUS_ACTIVE)  # no raise
    # only the create + one real transition should be audited
    log = store.audit_log(item.id)
    assert [e.to_status for e in log] == [STATUS_CANDIDATE, STATUS_ACTIVE]


def test_undo_superseded_back_to_active(store):
    item = store.add_item(kind="profile", text="email a@b.com")
    store.transition(item.id, STATUS_ACTIVE)
    store.transition(item.id, STATUS_SUPERSEDED, reason="new email")
    restored = store.transition(item.id, STATUS_ACTIVE, actor=ACTOR_USER, reason="undo")
    assert restored.status == STATUS_ACTIVE


def test_supersede_link_recorded(store):
    winner = store.add_item(kind="profile", text="email new@b.com")
    loser = store.add_item(kind="profile", text="email old@b.com")
    store.transition(loser.id, STATUS_ACTIVE)
    store.transition(winner.id, STATUS_ACTIVE, supersedes=loser.id)
    store.transition(loser.id, STATUS_SUPERSEDED, reason="replaced")
    assert store.get_item(winner.id).supersedes == loser.id


def test_transition_missing_item_raises(store):
    with pytest.raises(GovernanceError):
        store.transition("m_doesnotexist", STATUS_ACTIVE)


# -- audit ------------------------------------------------------------------


def test_audit_row_per_transition(store):
    item = store.add_item(kind="preference", text="x", actor=ACTOR_DREAMER, reason="extracted")
    store.transition(item.id, STATUS_NEEDS_REVIEW, actor=ACTOR_DREAMER, reason="low conf")
    store.transition(item.id, STATUS_ACTIVE, actor=ACTOR_USER, reason="accepted")

    log = store.audit_log(item.id)
    assert [(e.from_status, e.to_status) for e in log] == [
        (None, STATUS_CANDIDATE),
        (STATUS_CANDIDATE, STATUS_NEEDS_REVIEW),
        (STATUS_NEEDS_REVIEW, STATUS_ACTIVE),
    ]
    assert log[0].actor == ACTOR_DREAMER
    assert log[-1].actor == ACTOR_USER
    assert log[-1].reason == "accepted"


# -- update / touch ---------------------------------------------------------


def test_update_fields(store):
    item = store.add_item(kind="preference", text="x", confidence=0.3)
    updated = store.update_fields(item.id, text="y", confidence=0.7)
    assert updated.text == "y"
    assert updated.confidence == 0.7
    assert updated.updated_at >= item.updated_at


def test_update_fields_rejects_status_change(store):
    item = store.add_item(kind="preference", text="x")
    with pytest.raises(GovernanceError):
        store.update_fields(item.id, status=STATUS_ACTIVE)


def test_update_message_ids_roundtrip(store):
    item = store.add_item(kind="preference", text="x")
    updated = store.update_fields(item.id, source_message_ids=["a", "b", "c"])
    assert updated.source_message_ids == ["a", "b", "c"]


def test_touch_used_sets_last_used(store):
    item = store.add_item(kind="preference", text="x")
    assert item.last_used_at is None
    store.touch_used(item.id)
    assert store.get_item(item.id).last_used_at is not None


# -- queries ----------------------------------------------------------------


def test_find_by_key_filters_status(store):
    a = store.add_item(kind="profile", text="email old", normalized_key="hakan|email")
    b = store.add_item(kind="profile", text="email new", normalized_key="hakan|email")
    store.transition(a.id, STATUS_ACTIVE)
    # b stays candidate
    actives = store.find_by_key("hakan|email", statuses={STATUS_ACTIVE})
    assert [i.id for i in actives] == [a.id]
    allk = store.find_by_key("hakan|email")
    assert {i.id for i in allk} == {a.id, b.id}


def test_list_items_filters(store):
    store.add_item(kind="preference", text="p1")
    f = store.add_item(kind="fact", text="f1")
    store.transition(f.id, STATUS_ACTIVE)
    assert len(store.list_items(kind="fact")) == 1
    assert len(store.list_items(status=STATUS_ACTIVE)) == 1
    assert len(store.list_items(status=STATUS_CANDIDATE)) == 1


def test_stats(store):
    store.add_item(kind="preference", text="p1")
    a = store.add_item(kind="fact", text="f1")
    r = store.add_item(kind="fact", text="f2")
    store.transition(a.id, STATUS_ACTIVE)
    store.transition(r.id, STATUS_NEEDS_REVIEW)
    s = store.stats()
    assert s["total"] == 3
    assert s["active"] == 1
    assert s["review_queue"] == 1
    assert s["by_kind"]["fact"] == 2


# -- meta kv ----------------------------------------------------------------


def test_meta_kv_roundtrip(store):
    assert store.get_meta("watermark") is None
    assert store.get_meta("watermark", "0") == "0"
    store.set_meta("watermark", "msg_123")
    assert store.get_meta("watermark") == "msg_123"
    store.set_meta("watermark", "msg_456")  # upsert
    assert store.get_meta("watermark") == "msg_456"


# -- concurrency (single-writer serialization) ------------------------------


def test_concurrent_writes_serialized(store):
    """The RLock must keep concurrent writers from corrupting state."""
    ids = []

    def worker(n):
        for i in range(20):
            it = store.add_item(kind="preference", text=f"t{n}-{i}")
            ids.append(it.id)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == 80
    assert len(set(ids)) == 80  # all unique, no lost writes
    assert store.stats()["total"] == 80
