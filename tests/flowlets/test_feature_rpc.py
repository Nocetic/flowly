"""feature_rpc flowlets.* surface: dispatch, capabilities, action broadcast.

Runs under an isolated FLOWLY_HOME (set by the fixture) so the singleton store
the handlers use never touches the developer's real ~/.flowly.
"""

from __future__ import annotations

import pytest

from .conftest import load_fixture


@pytest.fixture
def rpc_home(tmp_path, monkeypatch):
    """Point get_flowly_home() at a temp dir and reset the store singleton."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    import flowly.flowlets.store as store_mod
    store_mod._CACHE.clear()
    yield tmp_path
    store_mod._CACHE.clear()


def _seed_flowlet(name="water"):
    from flowly.flowlets.schema import validate_definition
    from flowly.flowlets.store import get_store
    defn = load_fixture(name)
    validate_definition(defn)
    store = get_store()
    return store.create(
        defn["name"], defn, icon=defn.get("icon"), accent=defn.get("accent"),
        catalog=defn["catalog"], origin_session="desktop:main",
    )


def test_capabilities_advertises_flowlets(rpc_home):
    from flowly.channels import feature_rpc
    caps = feature_rpc.system_capabilities()
    methods = set(caps["featureMethods"])
    assert {"flowlets.list", "flowlets.get", "flowlets.state",
            "flowlets.action", "flowlets.pin", "flowlets.delete"} <= methods


async def test_dispatch_list_and_get(rpc_home):
    from flowly.channels import feature_rpc
    f = _seed_flowlet()
    listed, _ = await feature_rpc.dispatch("flowlets.list", {})
    assert len(listed["flowlets"]) == 1
    assert listed["flowlets"][0]["id"] == f["id"]
    assert "values" in listed["flowlets"][0]         # cards show live values

    got, _ = await feature_rpc.dispatch("flowlets.get", {"id": f["id"]})
    assert got["flowlet"]["definition"]["name"] == "Su Takibi"
    assert got["values"]["goal_ml"] == 2000


async def test_dispatch_action_updates_and_broadcasts(rpc_home):
    from flowly.channels import feature_rpc
    events = []

    async def capture(name, data):
        events.append((name, data))

    feature_rpc.set_flowlet_broadcast(capture)
    try:
        f = _seed_flowlet()
        res, _ = await feature_rpc.dispatch(
            "flowlets.action", {"id": f["id"], "componentId": "drink250"}
        )
        assert res["values"]["today_ml"] == 250
        # the OTHER clients get a flowlet.state broadcast
        assert any(n == "flowlet.state" for n, _ in events)
        state_evt = [d for n, d in events if n == "flowlet.state"][-1]
        assert state_evt["id"] == f["id"]
        assert state_evt["values"]["today_ml"] == 250
    finally:
        feature_rpc.set_flowlet_broadcast(None)


async def test_dispatch_action_invalid_component(rpc_home):
    from flowly.channels import feature_rpc
    f = _seed_flowlet()
    with pytest.raises(feature_rpc.FeatureRpcError) as ei:
        await feature_rpc.dispatch(
            "flowlets.action", {"id": f["id"], "componentId": "ghost"}
        )
    assert ei.value.code == "NOT_FOUND"


async def test_dispatch_delete_broadcasts(rpc_home):
    from flowly.channels import feature_rpc
    events = []

    async def capture(name, data):
        events.append((name, data))

    feature_rpc.set_flowlet_broadcast(capture)
    try:
        f = _seed_flowlet()
        res, _ = await feature_rpc.dispatch("flowlets.delete", {"id": f["id"]})
        assert res["ok"] is True
        assert any(n == "flowlet.deleted" for n, _ in events)
    finally:
        feature_rpc.set_flowlet_broadcast(None)


async def test_agent_action_uses_runner(rpc_home):
    from flowly.channels import feature_rpc
    called = {}

    async def runner(flowlet, message):
        called["msg"] = message

    feature_rpc.set_flowlet_agent_runner(runner)
    try:
        f = _seed_flowlet()
        await feature_rpc.dispatch(
            "flowlets.action", {"id": f["id"], "componentId": "coach"}
        )
        assert "su" in called["msg"].lower()
    finally:
        feature_rpc.set_flowlet_agent_runner(None)
