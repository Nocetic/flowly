"""Flowlet store: CRUD, versioning, state, event log, single-writer safety."""

from __future__ import annotations

import threading


def test_create_and_get(store, water_def):
    f = store.create("Su Takibi", water_def, icon="droplet", accent="#00A6C8")
    assert f["id"].startswith("flt_")
    assert f["name"] == "Su Takibi"
    assert f["version"] == 1
    assert f["pinned"] is False
    assert f["definition"]["catalog"] == 1

    got = store.get(f["id"])
    assert got["definition"] == water_def


def test_update_bumps_version_and_snapshots(store, water_def):
    f = store.create("x", water_def)
    changed = dict(water_def)
    changed = {**water_def, "name": "Su v2"}
    updated = store.update(f["id"], definition=changed)
    assert updated["version"] == 2
    versions = store.get_versions(f["id"])
    assert len(versions) == 1
    assert versions[0]["version"] == 1


def test_update_no_definition_change_keeps_version(store, water_def):
    f = store.create("x", water_def)
    updated = store.update(f["id"], name="renamed")
    assert updated["version"] == 1
    assert updated["name"] == "renamed"


def test_pin_and_list_order(store, water_def):
    a = store.create("a", water_def)
    b = store.create("b", water_def)
    store.pin(b["id"], True)
    listed = store.list()
    assert listed[0]["id"] == b["id"]      # pinned floats to top
    assert {x["id"] for x in listed} == {a["id"], b["id"]}


def test_delete_cascades(store, water_def):
    f = store.create("x", water_def)
    store.set_state(f["id"], "goal_ml", 3000)
    store.add_event(f["id"], "water", 250)
    assert store.delete(f["id"]) is True
    assert store.get(f["id"]) is None
    assert store.get_state(f["id"]) == {}
    assert store.get_events(f["id"]) == []


def test_state_set_and_reset(store, water_def):
    f = store.create("x", water_def)
    store.set_state(f["id"], "goal_ml", 2500)
    assert store.get_state(f["id"]) == {"goal_ml": 2500}
    store.reset_state(f["id"], "goal_ml")
    assert store.get_state(f["id"]) == {}


def test_events_ordered_and_remove_last(store, water_def):
    f = store.create("x", water_def)
    store.add_event(f["id"], "water", 250, ts=1000)
    store.add_event(f["id"], "water", 500, ts=2000)
    events = store.get_events(f["id"])
    assert [e["value"] for e in events] == [250, 500]
    assert store.remove_last_event(f["id"], "water") is True
    assert [e["value"] for e in store.get_events(f["id"])] == [250]
    # remove_last on an empty series is a no-op, not an error
    store.reset_events(f["id"], "water")
    assert store.remove_last_event(f["id"], "water") is False


def test_concurrent_writes_single_writer(store, water_def):
    f = store.create("x", water_def)

    def worker():
        for _ in range(50):
            store.add_event(f["id"], "water", 1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.get_events(f["id"])) == 200
