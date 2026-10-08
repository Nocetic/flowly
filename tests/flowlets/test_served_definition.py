"""What clients render and what taps resolve against come from one function,
and that definition reads well: empty number fields stay empty, a date draft
shows the real date, and the screen title is not printed twice."""

from __future__ import annotations

import pytest

from flowly.channels import feature_rpc
from flowly.flowlets.actions import apply_action
from flowly.flowlets.normalize import drop_title_header, served_definition
from flowly.flowlets.queries import resolve_values
from flowly.flowlets.store import FlowletStore

_DEF = {
    "catalog": 3, "name": "Kalori Takibim",
    "state": {"meals": {"type": "list", "item": {"name": "string", "kcal": "number", "date": "date"}}},
    "layout": [
        {"type": "header", "text": "Kalori  takibim", "subtitle": "Öğünlerini kaydet"},
        {"type": "form", "id": "add", "into": "meals", "fields": [
            {"field": "name"}, {"field": "kcal"}, {"field": "date", "default": "today"}]},
    ],
}


@pytest.fixture
def store(tmp_path):
    return FlowletStore(tmp_path / "f.db")


def test_title_only_header_is_dropped_and_subtitle_kept():
    out = drop_title_header(_DEF, "Kalori Takibim")
    assert out["layout"][0] == {"type": "text", "text": "Öğünlerini kaydet", "style": "muted"}
    assert out["layout"][1]["type"] == "form"
    # A different heading is a deliberate section title.
    other = {**_DEF, "layout": [{"type": "header", "text": "Bugün"}, *_DEF["layout"][1:]]}
    assert drop_title_header(other, "Kalori Takibim") is other


def test_form_number_draft_is_empty_and_date_draft_is_a_real_date():
    served = served_definition(_DEF)
    values = resolve_values(served, {}, [], 1_791_460_800_000)  # 2026-10-08 12:00 UTC
    assert values["add__kcal"] is None
    assert values["add__date"] == "2026-10-08"


async def test_served_ids_resolve_taps_and_clearing_a_number_keeps_it_empty(store):
    f = store.create(name="Kalori Takibim", definition=_DEF, icon=None, accent=None, catalog=3)
    out = await apply_action(store, f["id"], "add_kcal", value=420)
    assert out["values"]["add__kcal"] == 420
    out = await apply_action(store, f["id"], "add_kcal", value="")
    assert out["values"]["add__kcal"] is None
    await apply_action(store, f["id"], "add_name", value="Çorba")
    await apply_action(store, f["id"], "add_kcal", value=180)
    out = await apply_action(store, f["id"], "add_submit")
    row = out["values"]["meals"][0]
    assert row["name"] == "Çorba" and row["kcal"] == 180 and len(row["date"]) == 10
    assert out["values"]["add__kcal"] is None  # reset to empty, ready for the next entry


def test_rpc_get_serves_the_shared_definition(store, monkeypatch):
    f = store.create(name="Kalori Takibim", definition=_DEF, icon=None, accent=None, catalog=3)
    monkeypatch.setattr(feature_rpc, "_flowlet_store", lambda: store)
    got = feature_rpc.flowlets_get({"id": f["id"]})["flowlet"]["definition"]
    assert got == served_definition(store.get(f["id"])["definition"], "Kalori Takibim")
    assert all(n.get("type") != "header" or n.get("text") != "Kalori  takibim" for n in got["layout"])


def test_injected_edit_fields_reuse_the_entry_form_labels():
    """A row's edit screen once labelled its fields "title", "amount" — raw
    identifiers. They now reuse the form's own labels, else a readable name."""
    defn = {
        "catalog": 3, "name": "Spend",
        "state": {"rows": {"type": "list", "item": {"title": "string", "amount": "number",
                                                     "payment_method": "string"}}},
        "layout": [
            {"type": "form", "id": "add", "into": "rows", "fields": [
                {"field": "title", "label": "Ne"}, {"field": "amount", "label": "Tutar"}]},
            {"type": "repeater", "source": "rows", "item": {"type": "list_row", "title": "$.title"}},
        ],
    }
    served = served_definition(defn)
    screen = next(iter(served["screens"].values()))
    labels = {n["value"]: n["label"] for n in screen["layout"] if "value" in n}
    assert labels == {"$.title": "Ne", "$.amount": "Tutar", "$.payment_method": "Payment method"}
