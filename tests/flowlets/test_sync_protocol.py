"""Every values map a client receives is ordered, retried taps apply once,
multi-step actions are all-or-nothing, and "today" is the user's day.

Clients keep the map with the highest ``rev`` they have seen, so a slow poll
or a late push can never roll the screen back.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from flowly.flowlets.actions import FlowletActionError, apply_action
from flowly.flowlets.store import FlowletStore

from .conftest import load_fixture
from .test_feature_rpc import _seed_flowlet, rpc_home  # noqa: F401  (fixture)


async def test_every_values_payload_carries_a_rising_rev(rpc_home):  # noqa: F811
    from flowly.channels import feature_rpc
    events = []

    async def capture(name, data):
        events.append((name, data))

    feature_rpc.set_flowlet_broadcast(capture)
    try:
        f = _seed_flowlet()
        got, _ = await feature_rpc.dispatch("flowlets.get", {"id": f["id"]})
        listed, _ = await feature_rpc.dispatch("flowlets.list", {})
        r0 = got["rev"]
        assert listed["flowlets"][0]["rev"] == r0
        acted, _ = await feature_rpc.dispatch(
            "flowlets.action", {"id": f["id"], "componentId": "drink250"})
        assert acted["rev"] > r0
        pushed = [d for n, d in events if n == "flowlet.state"][-1]
        assert pushed["rev"] == acted["rev"]
        state, _ = await feature_rpc.dispatch("flowlets.state", {"id": f["id"]})
        assert state["rev"] == acted["rev"] and state["values"]["today_ml"] == 250
    finally:
        feature_rpc.set_flowlet_broadcast(None)


async def test_a_retried_tap_applies_once(rpc_home):  # noqa: F811
    from flowly.channels import feature_rpc
    f = _seed_flowlet()
    params = {"id": f["id"], "componentId": "drink250", "opId": "tap-1"}
    first, _ = await feature_rpc.dispatch("flowlets.action", params)
    again, _ = await feature_rpc.dispatch("flowlets.action", params)
    assert first["values"]["today_ml"] == 250
    assert again["duplicate"] is True and again["values"]["today_ml"] == 250
    assert again["rev"] == first["rev"]
    other, _ = await feature_rpc.dispatch(
        "flowlets.action", {**params, "opId": "tap-2"})
    assert other["values"]["today_ml"] == 500


async def test_a_failed_tap_can_be_retried_with_the_same_op_id(store):
    defn = {"catalog": 3, "name": "T", "state": {"n": {"type": "number", "default": 0}},
            "layout": [{"type": "number_input", "id": "n_in", "value": "n",
                        "action": {"op": "set", "key": "n"}}]}
    f = store.create("T", defn)
    with pytest.raises(FlowletActionError):
        await apply_action(store, f["id"], "n_in", value=None, op_id="op-1")
    out = await apply_action(store, f["id"], "n_in", value=5, op_id="op-1")
    assert out["values"]["n"] == 5 and "duplicate" not in out


async def test_toggle_with_a_value_is_idempotent(store):
    defn = {"catalog": 3, "name": "T", "state": {"on": {"type": "bool", "default": False}},
            "layout": [{"type": "toggle", "id": "sw", "value": "on",
                        "action": {"op": "toggle", "key": "on"}}]}
    f = store.create("T", defn)
    for _ in range(2):
        out = await apply_action(store, f["id"], "sw", value=True)
        assert out["values"]["on"] is True
    out = await apply_action(store, f["id"], "sw")  # legacy client: plain flip
    assert out["values"]["on"] is False


async def test_a_batch_that_fails_midway_changes_nothing(store):
    defn = {"catalog": 3, "name": "T",
            "state": {"a": {"type": "number", "default": 0},
                      "rows": {"type": "list", "max": 1, "item": {"t": "string"}}},
            "layout": [{"type": "button", "id": "go", "text": "Go", "action": {"op": "batch", "ops": [
                {"op": "increment", "key": "a", "by": 1},
                {"op": "item_add", "key": "rows", "item": {"t": "x"}},
            ]}}]}
    f = store.create("T", defn)
    out = await apply_action(store, f["id"], "go")
    assert out["values"]["a"] == 1 and len(out["values"]["rows"]) == 1
    rev = store.rev(f["id"])
    with pytest.raises(FlowletActionError, match="full"):
        await apply_action(store, f["id"], "go")      # the list is full
    state = store.get_state(f["id"])
    assert state["a"] == 1 and len(state["rows"]) == 1  # the increment was rolled back
    assert store.rev(f["id"]) > rev                     # clients still refetch


def test_store_migrates_an_old_database_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '3');
        CREATE TABLE flowlets (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
            icon TEXT, accent TEXT, definition TEXT NOT NULL DEFAULT '{}',
            catalog INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
            pinned INTEGER NOT NULL DEFAULT 0, origin_session TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        INSERT INTO flowlets (id, name, created_at, updated_at) VALUES ('flt_1_1', 'Old', 1, 1);
    """)
    conn.commit()
    conn.close()
    store = FlowletStore(path)
    assert store.get("flt_1_1")["rev"] == 0
    store.set_state("flt_1_1", "x", 1)
    assert store.rev("flt_1_1") == 1


async def test_today_is_the_users_day_not_the_hosts(rpc_home, monkeypatch):  # noqa: F811
    """A core in UTC must not end a Tokyo user's day at UTC midnight."""
    from flowly.channels import feature_rpc
    from flowly.flowlets import actions
    from flowly.flowlets.store import get_store
    from flowly.flowlets.watches import WatchEngine

    tokyo = ZoneInfo("Asia/Tokyo")
    # 08:30 in Tokyo on Oct 9 is still Oct 8 in UTC.
    morning = int(datetime(2026, 10, 9, 8, 30, tzinfo=tokyo).timestamp() * 1000)
    monkeypatch.setattr(actions, "queries_now_ms", lambda: morning)
    monkeypatch.setattr("flowly.flowlets.store.now_ms", lambda: morning)
    f = _seed_flowlet()
    await feature_rpc.dispatch("flowlets.action", {
        "id": f["id"], "componentId": "drink250", "tz": "Asia/Tokyo"})
    assert get_store().get(f["id"])["tz"] == "Asia/Tokyo"

    # Later the same Tokyo day, computed WITHOUT the client present.
    evening = int(datetime(2026, 10, 9, 22, 0, tzinfo=tokyo).timestamp() * 1000)
    monkeypatch.setattr("flowly.flowlets.store.now_ms", lambda: evening)
    state, _ = await feature_rpc.dispatch("flowlets.state", {"id": f["id"]})
    assert state["values"]["today_ml"] == 250

    sent = []

    async def notify(fid, title, body):
        sent.append(body)

    defn = load_fixture("water")
    defn["watches"] = [{"id": "w", "trigger": "schedule", "at": "21:00",
                        "notify": {"title": "Water", "body": "{today_ml} ml"}}]
    get_store().update(f["id"], definition=defn)
    await WatchEngine(get_store(), notify=notify, tz=ZoneInfo("UTC")).evaluate_all(now_ms=evening)
    assert sent == ["250 ml"]  # 21:00 Tokyo time, and the Tokyo day's total


async def test_an_unknown_zone_is_ignored(rpc_home):  # noqa: F811
    from flowly.channels import feature_rpc
    from flowly.flowlets.store import get_store
    f = _seed_flowlet()
    await feature_rpc.dispatch("flowlets.state", {"id": f["id"], "tz": "Mars/Olympus"})
    assert get_store().get(f["id"])["tz"] is None
