"""Dynamic lists — `list` state + `repeater` + item_* ops (todo/shopping/journal)."""

from __future__ import annotations

import copy
from datetime import timezone

import pytest

from flowly.flowlets.actions import FlowletActionError, apply_action
from flowly.flowlets.queries import flowlet_preview, resolve_values
from flowly.flowlets.schema import FlowletValidationError, validate_definition
from flowly.flowlets.store import now_ms

UTC = timezone.utc

TODO = {
    "catalog": 1,
    "name": "Görevler",
    "state": {
        "tasks": {"type": "list", "item": {"title": "string", "done": "bool"}, "max": 5},
    },
    "layout": [
        {"id": "new_task", "type": "input", "placeholder": "Yeni görev…",
         "action": {"op": "item_add", "key": "tasks"}},
        {"type": "repeater", "source": "tasks", "empty": "Henüz görev yok",
         "item": {"type": "row", "children": [
             {"id": "tgl", "type": "toggle", "value": "$.done",
              "action": {"op": "item_toggle", "key": "tasks", "field": "done"}},
             {"type": "text", "text": "{$.title}"},
             {"id": "del", "type": "icon_button", "icon": "trash",
              "action": {"op": "item_remove", "key": "tasks"}},
         ]}},
    ],
}


# ── schema ────────────────────────────────────────────────────────────────────

def test_todo_definition_valid():
    validate_definition(copy.deepcopy(TODO))


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d["state"]["tasks"].pop("item"), "item"),                       # no schema
        (lambda d: d["state"]["tasks"]["item"].update({"id": "string"}), "reserved"),
        (lambda d: d["state"]["tasks"]["item"].update({"x": "blob"}), "type"),     # bad ftype
        (lambda d: d["layout"][1].update({"source": "ghost"}), "source"),
        (lambda d: d["layout"][1]["item"]["children"][0].update(
            {"value": "$.ghost"}), "item field"),
        (lambda d: d["layout"][1]["item"]["children"][0]["action"].update(
            {"field": "title"}), "bool"),                                          # toggle non-bool
        (lambda d: d["layout"].append(
            {"id": "orphan", "type": "button", "text": "x",
             "action": {"op": "item_remove", "key": "tasks"}}), "inside the repeater"),
        (lambda d: d["state"]["tasks"].update({"max": 0}), "max"),
    ],
)
def test_schema_rejects(mutate, match):
    d = copy.deepcopy(TODO)
    mutate(d)
    with pytest.raises(FlowletValidationError, match=match):
        validate_definition(d)


def test_schema_rejects_nested_repeater():
    d = copy.deepcopy(TODO)
    d["layout"][1]["item"] = {"type": "repeater", "source": "tasks",
                              "item": {"type": "text", "text": "x"}}
    with pytest.raises(FlowletValidationError, match="nest"):
        validate_definition(d)


def test_list_key_is_not_a_scalar():
    d = copy.deepcopy(TODO)
    d["layout"].append({"type": "stat", "value": "tasks"})
    with pytest.raises(FlowletValidationError, match="unknown key"):
        validate_definition(d)


# ── actions (end-to-end on the store) ─────────────────────────────────────────

async def _add(store, fid, text):
    return await apply_action(store, fid, "new_task", value=text, tz=UTC)


async def test_add_toggle_remove_roundtrip(store):
    f = store.create("Görevler", copy.deepcopy(TODO))
    fid = f["id"]

    res = await _add(store, fid, "süt al")
    tasks = res["values"]["tasks"]
    assert len(tasks) == 1 and tasks[0]["title"] == "süt al" and tasks[0]["id"]
    assert tasks[0].get("done") in (None, False)

    await _add(store, fid, "ekmek al")
    item_id = (await _add(store, fid, "su al"))["values"]["tasks"][0]["id"]

    # toggle the first row
    res = await apply_action(store, fid, "tgl", value={"itemId": item_id}, tz=UTC)
    assert [t for t in res["values"]["tasks"] if t["id"] == item_id][0]["done"] is True

    # remove it
    res = await apply_action(store, fid, "del", value={"itemId": item_id}, tz=UTC)
    assert all(t["id"] != item_id for t in res["values"]["tasks"])
    assert len(res["values"]["tasks"]) == 2


async def test_add_empty_rejected_and_cap_enforced(store):
    f = store.create("Görevler", copy.deepcopy(TODO))
    fid = f["id"]
    with pytest.raises(FlowletActionError, match="nothing to add"):
        await _add(store, fid, "   ")
    for i in range(5):  # max: 5
        await _add(store, fid, f"görev {i}")
    with pytest.raises(FlowletActionError, match="full"):
        await _add(store, fid, "taşan görev")


async def test_item_ops_need_envelope(store):
    f = store.create("Görevler", copy.deepcopy(TODO))
    fid = f["id"]
    await _add(store, fid, "x")
    with pytest.raises(FlowletActionError, match="itemId"):
        await apply_action(store, fid, "tgl", value=None, tz=UTC)
    with pytest.raises(FlowletActionError, match="no longer exists"):
        await apply_action(store, fid, "tgl", value={"itemId": "ghost"}, tz=UTC)


async def test_item_move_reorders(store):
    d = copy.deepcopy(TODO)
    d["layout"][1]["item"]["children"].append(
        {"id": "mv", "type": "icon_button", "icon": "arrow-up",
         "action": {"op": "item_move", "key": "tasks"}})
    f = store.create("Görevler", d)
    fid = f["id"]
    a = (await _add(store, fid, "a"))["values"]["tasks"][0]["id"]
    await _add(store, fid, "b")
    res = await apply_action(store, fid, "mv", value={"itemId": a, "value": 1}, tz=UTC)
    assert [t["title"] for t in res["values"]["tasks"]] == ["b", "a"]


async def test_string_field_capped(store):
    f = store.create("Görevler", copy.deepcopy(TODO))
    res = await _add(store, f["id"], "a" * 2000)
    assert len(res["values"]["tasks"][0]["title"]) == 500


# ── resolve + preview ─────────────────────────────────────────────────────────

def test_resolve_exposes_items_and_preview_counts(store):
    f = store.create("Görevler", copy.deepcopy(TODO))
    fid = f["id"]
    store.set_state(fid, "tasks", [
        {"id": "i1", "title": "a", "done": True},
        {"id": "i2", "title": "b", "done": False},
        "garbage",                       # malformed rows drop out
        {"title": "no-id"},
    ])
    values = resolve_values(TODO, store.get_state(fid), [], now_ms(), UTC)
    assert [t["id"] for t in values["tasks"]] == ["i1", "i2"]
    pv = flowlet_preview(TODO, values)
    assert pv == {"text": "1/2", "pct": 0.5}


async def test_envelope_unwrapped_for_non_item_ops(store):
    """A non-item op inside a repeater row still works when the client wraps
    its value in the row envelope (templates stay fully general)."""
    d = copy.deepcopy(TODO)
    d["state"]["note"] = {"type": "string", "default": ""}
    d["layout"][1]["item"]["children"].append(
        {"id": "note_in", "type": "input",
         "action": {"op": "set", "key": "note"}})
    f = store.create("Görevler", d)
    fid = f["id"]
    await _add(store, fid, "x")
    item_id = (await apply_action(store, fid, "new_task", value="y", tz=UTC))["values"]["tasks"][0]["id"]
    res = await apply_action(store, fid, "note_in",
                             value={"itemId": item_id, "value": "hello"}, tz=UTC)
    assert res["values"]["note"] == "hello"
