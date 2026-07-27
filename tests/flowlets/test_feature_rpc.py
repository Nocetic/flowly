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
            "flowlets.action", "flowlets.pin", "flowlets.delete",
            "flowlets.templates", "flowlets.createFromTemplate"} <= methods


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


async def test_dispatch_templates_are_localized(rpc_home):
    from flowly.channels import feature_rpc
    en, _ = await feature_rpc.dispatch("flowlets.templates", {})
    tr, _ = await feature_rpc.dispatch("flowlets.templates", {"lang": "tr-TR"})
    assert [t["id"] for t in en["templates"]] == [t["id"] for t in tr["templates"]]
    assert en["templates"][0]["title"] != tr["templates"][0]["title"]


async def test_dispatch_create_from_template_creates_and_broadcasts(rpc_home):
    from flowly.channels import feature_rpc
    events = []

    async def capture(name, data):
        events.append((name, data))

    feature_rpc.set_flowlet_broadcast(capture)
    try:
        res, _ = await feature_rpc.dispatch(
            "flowlets.createFromTemplate", {"templateId": "water", "lang": "tr"}
        )
        created = res["flowlet"]
        assert created["name"] == "Su Takibi"
        assert "values" in created            # a card can render it immediately
        assert any(n == "flowlet.created" for n, _ in events)

        # It is an ORDINARY flowlet from here on — listed, gettable, editable.
        listed, _ = await feature_rpc.dispatch("flowlets.list", {})
        assert [f["id"] for f in listed["flowlets"]] == [created["id"]]
        got, _ = await feature_rpc.dispatch("flowlets.get", {"id": created["id"]})
        assert got["values"]["goal_ml"] == 2000
    finally:
        feature_rpc.set_flowlet_broadcast(None)


async def test_dispatch_create_from_template_rejects_unknown(rpc_home):
    from flowly.channels import feature_rpc
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch("flowlets.createFromTemplate", {"templateId": "nope"})
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch("flowlets.createFromTemplate", {})


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


async def test_agent_action_is_rate_limited(rpc_home, monkeypatch):
    # A tapped `agent` op is a paid model turn — throttled like captures.
    from flowly.channels import feature_rpc
    from flowly.flowlets import catalog
    monkeypatch.setattr(catalog, "MAX_AGENT_ACTIONS_PER_FLOWLET_PER_WINDOW", 2)
    feature_rpc._agent_action_hits.clear()
    feature_rpc._agent_action_hits_global.clear()

    async def runner(flowlet, message):
        pass

    feature_rpc.set_flowlet_agent_runner(runner)
    try:
        f = _seed_flowlet()
        p = {"id": f["id"], "componentId": "coach"}
        await feature_rpc.dispatch("flowlets.action", p)
        await feature_rpc.dispatch("flowlets.action", p)
        with pytest.raises(feature_rpc.FeatureRpcError) as ei:
            await feature_rpc.dispatch("flowlets.action", p)   # 3rd within window → blocked
        assert ei.value.code == "RATE_LIMITED"
    finally:
        feature_rpc.set_flowlet_agent_runner(None)


# ── photo capture: decode guard, magic bytes, rate limit ──────────────────────

def test_decode_capture_image_guards():
    import base64

    from flowly.channels.feature_rpc import _decode_capture_image
    jpeg = base64.b64encode(b"\xff\xd8\xff" + b"x" * 100).decode()
    assert _decode_capture_image("data:image/jpeg;base64," + jpeg, 1_000_000) is not None
    assert _decode_capture_image(jpeg, 1_000_000) is not None            # bare b64 ok
    # non-JPEG bytes (valid base64, wrong magic) → rejected
    png = base64.b64encode(b"\x89PNG\r\n" + b"x" * 100).decode()
    assert _decode_capture_image(png, 1_000_000) is None
    # oversized rejected BEFORE decode (b64 longer than the byte budget)
    big = base64.b64encode(b"\xff\xd8\xff" + b"x" * 5000).decode()
    assert _decode_capture_image(big, 1000) is None
    assert _decode_capture_image("not base64!!", 1_000_000) is None
    assert _decode_capture_image(None, 1_000_000) is None


def test_capture_rate_limit(rpc_home, monkeypatch):
    from flowly.channels import feature_rpc
    from flowly.flowlets import catalog
    monkeypatch.setattr(catalog, "MAX_CAPTURES_PER_FLOWLET_PER_WINDOW", 2)
    # reset the module-level windows so other tests don't bleed in
    feature_rpc._capture_hits.clear()
    feature_rpc._capture_hits_global.clear()
    fid = "flt_dead_beef"
    assert feature_rpc._capture_rate_ok(fid) is True
    assert feature_rpc._capture_rate_ok(fid) is True
    assert feature_rpc._capture_rate_ok(fid) is False   # 3rd within the window → blocked


def test_attachment_rpc_rejects_unknown_flowlet(rpc_home):
    from flowly.channels import feature_rpc
    with pytest.raises(feature_rpc.FeatureRpcError):
        feature_rpc.flowlets_attachment({"id": "../../etc", "attachmentId": "att_00000000"})


# ── swipe-to-delete (flowlets.itemRemove) ─────────────────────────────────────

def _seed_list_flowlet():
    from flowly.flowlets.schema import validate_definition
    from flowly.flowlets.store import get_store
    defn = {
        "catalog": 2, "name": "Kalori",
        "state": {"meals": {"type": "list", "item": {"name": "string", "kcal": "number"}}},
        "layout": [
            {"type": "repeater", "id": "list", "source": "meals",
             "item": {"type": "text", "text": "{$.name}"}},
        ],
    }
    validate_definition(defn)
    store = get_store()
    f = store.create(defn["name"], defn, catalog=2)
    store.set_state(f["id"], "meals", [
        {"id": "itm_a", "name": "Tost", "kcal": 300},
        {"id": "itm_b", "name": "Salata", "kcal": 120},
    ])
    return store, f


async def test_item_remove_rpc_deletes_a_row(rpc_home):
    from flowly.channels import feature_rpc
    store, f = _seed_list_flowlet()
    r, _ = await feature_rpc.dispatch(
        "flowlets.itemRemove", {"id": f["id"], "source": "meals", "itemId": "itm_a"}
    )
    names = [m["name"] for m in r["values"]["meals"]]
    assert names == ["Salata"]
    # persisted, not just in the reply
    assert [m["id"] for m in store.get_state(f["id"])["meals"]] == ["itm_b"]


async def test_item_remove_rpc_rejects_source_owned_list(rpc_home):
    from flowly.channels import feature_rpc
    from flowly.flowlets.schema import validate_definition
    from flowly.flowlets.store import get_store
    defn = {
        "catalog": 2, "name": "Commits",
        "state": {"commits": {"type": "list", "item": {"title": "string"}, "source": True}},
        "sources": {"commits": {"kind": "agent", "prompt": "recent commits", "into": "commits"}},
        "layout": [{"type": "repeater", "id": "list", "source": "commits",
                    "item": {"type": "text", "text": "{$.title}"}}],
    }
    validate_definition(defn)
    f = get_store().create(defn["name"], defn, catalog=2)
    with pytest.raises(feature_rpc.FeatureRpcError):
        await feature_rpc.dispatch(
            "flowlets.itemRemove", {"id": f["id"], "source": "commits", "itemId": "x"}
        )


async def test_item_remove_rpc_unknown_row_is_noop(rpc_home):
    from flowly.channels import feature_rpc
    store, f = _seed_list_flowlet()
    r, _ = await feature_rpc.dispatch(
        "flowlets.itemRemove", {"id": f["id"], "source": "meals", "itemId": "nope"}
    )
    assert len(r["values"]["meals"]) == 2  # nothing removed, no error
