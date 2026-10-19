"""A form saves a row only when what a person must type is there: the row's
name and its numbers. An empty submit adds nothing, says what is missing in
the screen's language, and leaves what was typed in place."""

import pytest

from flowly.flowlets.actions import FlowletActionError, apply_action
from flowly.flowlets.composites import expand_composites
from flowly.flowlets.templates import build_template


def _submit(defn):
    expanded = expand_composites(defn)
    for node in expanded["layout"]:
        for child in node.get("children") or []:
            if child.get("id", "").endswith("_submit"):
                return child
    raise AssertionError("no submit button")


def test_the_name_and_numbers_are_required_but_choices_and_dates_are_not():
    item_add = _submit(build_template("expenses", "tr"))["action"]["ops"][0]
    assert item_add["require"] == [{"field": "title", "label": "Ne"}, {"field": "amount", "label": "Tutar"}]


def test_an_author_decides_either_way():
    defn = build_template("expenses", "en")
    form = next(n for n in defn["layout"] if n.get("type") == "form")
    form["fields"][0]["required"] = False
    form["fields"][2]["required"] = True
    require = [r["field"] for r in _submit(defn)["action"]["ops"][0]["require"]]
    assert require == ["amount", "category"]


async def test_an_empty_submit_adds_nothing_and_keeps_what_was_typed(store):
    fid = store.create(name="Harcamalar", definition=build_template("expenses", "tr"))["id"]
    await apply_action(store, fid, "addExpense_title", value="Kahve")

    with pytest.raises(FlowletActionError) as err:
        await apply_action(store, fid, "addExpense_submit")
    assert err.value.code == "REQUIRED"
    assert err.value.message == "Önce şunları doldur: Tutar"
    assert store.get_state(fid).get("expenses") in (None, [])
    assert store.get_state(fid)["addExpense__title"] == "Kahve"   # not reset

    await apply_action(store, fid, "addExpense_amount", value=85)
    await apply_action(store, fid, "addExpense_submit")
    rows = store.get_state(fid)["expenses"]
    assert len(rows) == 1 and rows[0]["title"] == "Kahve" and rows[0]["amount"] == 85


async def test_a_hand_written_add_follows_the_same_rule(store):
    defn = {
        "catalog": 3, "name": "Fişler", "locale": "tr",
        "state": {
            "title": {"type": "string", "default": ""},
            "amount": {"type": "number", "default": None, "nullable": True},
            "kind": {"type": "string", "default": "Market"},
            "rows": {"type": "list", "item": {"title": "string", "amount": "number",
                                              "kind": "string", "date": "date"}},
        },
        "layout": [
            {"id": "t", "type": "input", "label": "Satıcı", "action": {"op": "set", "key": "title"}},
            {"id": "a", "type": "number_input", "label": "Tutar", "action": {"op": "set", "key": "amount"}},
            {"id": "save", "type": "button", "text": "Harcamayı kaydet", "action": {"op": "batch", "ops": [
                {"op": "item_add", "key": "rows",
                 "fields": {"title": "{title}", "amount": "{amount}", "kind": "{kind}", "date": "today"}},
                {"op": "reset", "key": "title"}, {"op": "reset", "key": "amount"}]}},
        ],
    }
    fid = store.create(name="Fişler", definition=defn)["id"]
    with pytest.raises(FlowletActionError) as err:
        await apply_action(store, fid, "save")
    assert err.value.message == "Önce şunları doldur: Satıcı, Tutar"
    assert not store.get_state(fid).get("rows")
