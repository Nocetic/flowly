"""Catalog-3 composites: expansion to primitives + author-time validation.

A composite states intent; the bot owns the layout by expanding it. These pin
the expansion shape (the native renderers will later assert byte-identical
output) and the validation that keeps a composite honest.
"""

from __future__ import annotations

import copy

import pytest

from flowly.flowlets.composites import expand_composites
from flowly.flowlets.schema import FlowletValidationError, validate_definition


def _defn(item_row: dict, *, item=None) -> dict:
    item = item or {
        "title": "string", "amount": "number", "category": "string",
        "date": "date", "merchant": "string", "receipt": "image",
    }
    return {
        "catalog": 3, "name": "Harcamalar",
        "state": {"expenses": {"type": "list", "item": item}},
        "layout": [{"type": "repeater", "id": "list", "source": "expenses",
                    "item": item_row}],
    }


def _row(out: dict) -> dict:
    return out["layout"][0]["item"]


# ── expansion ─────────────────────────────────────────────────────────────────

def test_full_list_row_expands_to_canonical_row():
    d = _defn({
        "type": "list_row",
        "thumb": "$.receipt", "title": "$.title",
        "subtitle": "$.merchant", "badge": "$.category",
        "value": "{$.amount} ₺",
    })
    row = _row(expand_composites(d))
    assert row["type"] == "row"
    kids = row["children"]
    assert kids[0] == {"type": "image", "src": "$.receipt", "height": 44}
    assert kids[1] == {"type": "column", "grow": True, "children": [
        {"type": "text", "text": "{$.title}", "grow": False},
        {"type": "text", "text": "{$.merchant}", "grow": False},
    ]}
    assert kids[2] == {"type": "badge", "text": "{$.category}"}
    assert kids[3] == {"type": "text", "text": "{$.amount} ₺", "grow": False}


def test_bare_field_becomes_interpolation_template_becomes_literal():
    d = _defn({"type": "list_row", "title": "$.title", "value": "{$.amount} ₺",
               "badge": "Fatura"})
    kids = _row(expand_composites(d))["children"]
    assert kids[0] == {"type": "text", "text": "{$.title}"}       # bare $. → {…}
    assert kids[1] == {"type": "badge", "text": "Fatura"}         # literal kept
    assert kids[2] == {"type": "text", "text": "{$.amount} ₺", "grow": False}  # template kept


def test_title_only_row_is_a_single_greedy_text():
    # No thumb, no subtitle → one greedy text, no wrapping column.
    d = _defn({"type": "list_row", "title": "$.title"})
    kids = _row(expand_composites(d))["children"]
    assert kids == [{"type": "text", "text": "{$.title}"}]


def test_thumb_with_only_title_still_wraps_the_text_block():
    # A thumb pushes the text out of the greedy-first-text position, so it must
    # live in a greedy column or it wouldn't fill.
    d = _defn({"type": "list_row", "thumb": "$.receipt", "title": "$.title"})
    kids = _row(expand_composites(d))["children"]
    assert kids[0]["type"] == "image"
    assert kids[1] == {"type": "column", "grow": True, "children": [
        {"type": "text", "text": "{$.title}", "grow": False}]}


def test_braced_thumb_is_normalized_to_a_field_ref():
    d = _defn({"type": "list_row", "thumb": "{$.receipt}", "title": "$.title"})
    kids = _row(expand_composites(d))["children"]
    assert kids[0] == {"type": "image", "src": "$.receipt", "height": 44}


def test_visible_when_is_carried_onto_the_expanded_row():
    d = _defn({"type": "list_row", "title": "$.title", "visibleWhen": "amount > 0"},
              item={"title": "string", "amount": "number"})
    # (validity of the expr is a schema concern; expansion just carries it)
    row = _row(expand_composites(d))
    assert row["visibleWhen"] == "amount > 0"


def test_expansion_is_idempotent():
    d = _defn({"type": "list_row", "title": "$.title", "value": "{$.amount} ₺"})
    once = expand_composites(d)
    twice = expand_composites(once)
    assert twice == once           # no composites left → second pass is a no-op
    assert _row(twice)["type"] == "row"


def test_no_composite_returns_the_original_object():
    d = _defn({"type": "row", "children": [{"type": "text", "text": "{$.title}"}]})
    assert expand_composites(d) is d          # cheap no-op, no copy


def test_stored_definition_never_mutated():
    d = _defn({"type": "list_row", "title": "$.title"})
    snap = copy.deepcopy(d)
    expand_composites(d)
    assert d == snap


def test_composite_inside_a_drill_screen_expands_too():
    d = _defn({"type": "list_row", "title": "$.title"})
    d["screens"] = {"detail": {"layout": [
        {"type": "list_row", "title": "$.title", "value": "{$.amount}"}]}}
    out = expand_composites(d)
    assert out["screens"]["detail"]["layout"][0]["type"] == "row"


# ── the expansion still validates as plain primitives ─────────────────────────

def test_expanded_definition_validates():
    d = _defn({
        "type": "list_row", "thumb": "$.receipt", "title": "$.title",
        "subtitle": "$.merchant", "badge": "$.category", "value": "{$.amount} ₺",
    })
    validate_definition(expand_composites(d))


# ── author-time validation ────────────────────────────────────────────────────

def test_list_row_validates_when_well_formed():
    assert validate_definition(_defn({
        "type": "list_row", "title": "$.title", "value": "{$.amount} ₺",
        "badge": "$.category", "subtitle": "$.date", "thumb": "$.receipt",
    })) is not None


def test_list_row_outside_a_repeater_is_rejected():
    d = {"catalog": 3, "name": "x",
         "state": {"e": {"type": "list", "item": {"title": "string"}}},
         "layout": [{"type": "list_row", "title": "$.title"}]}
    with pytest.raises(FlowletValidationError, match="only valid as a repeater"):
        validate_definition(d)


def test_list_row_missing_title_is_rejected():
    with pytest.raises(FlowletValidationError, match="missing required prop `title`"):
        validate_definition(_defn({"type": "list_row", "value": "{$.amount}"}))


def test_list_row_unknown_field_is_rejected():
    with pytest.raises(FlowletValidationError, match="unknown item field 'amout'"):
        validate_definition(_defn({"type": "list_row", "title": "$.title",
                                   "value": "{$.amout} ₺"}))


def test_list_row_unknown_field_in_badge_is_rejected():
    with pytest.raises(FlowletValidationError, match="unknown item field 'ghost'"):
        validate_definition(_defn({"type": "list_row", "title": "$.title",
                                   "badge": "$.ghost"}))


def test_catalog_3_is_accepted():
    validate_definition(_defn({"type": "list_row", "title": "$.title"}))


# ── form ──────────────────────────────────────────────────────────────────────

_EXPENSE_ITEM = {"title": "string", "amount": "number", "category": "string",
                 "date": "date", "note": "string", "receipt": "image"}


def _form_defn(form: dict, extra_layout=None) -> dict:
    return {
        "catalog": 3, "name": "Harcamalar",
        "state": {"expenses": {"type": "list", "item": _EXPENSE_ITEM}},
        "layout": [form, *(extra_layout or [])],
    }


_EXPENSE_FORM = {
    "type": "form", "id": "addExpense", "into": "expenses",
    "title": "Manuel harcama ekle",
    "fields": [
        {"field": "title", "label": "Açıklama", "placeholder": "örn. kahve"},
        {"field": "amount", "label": "Tutar"},
        {"field": "category", "options": ["Market", "Fatura", "Diğer"]},
        {"field": "date", "default": "today"},
    ],
    "submit": {"label": "Ekle"},
}


def test_form_expands_to_card_with_controls_and_submit():
    out = expand_composites(_form_defn(_EXPENSE_FORM))
    card = out["layout"][0]
    assert card["type"] == "card"
    kids = card["children"]
    assert kids[0] == {"type": "header", "text": "Manuel harcama ekle"}
    title_in = kids[1]
    assert title_in["type"] == "input"
    assert title_in["value"] == "addExpense__title"
    assert title_in["action"] == {"op": "set", "key": "addExpense__title"}
    assert kids[2]["type"] == "number_input"                       # amount
    assert kids[3]["type"] == "segmented"                          # 3 options
    assert kids[3]["options"] == ["Market", "Fatura", "Diğer"]
    assert kids[4]["type"] == "date"
    submit = kids[5]
    assert submit["type"] == "button" and submit["style"] == "primary"
    assert submit["text"] == "Ekle"
    ops = submit["action"]["ops"]
    assert ops[0] == {"op": "item_add", "key": "expenses", "fields": {
        "title": "{addExpense__title}", "amount": "{addExpense__amount}",
        "category": "{addExpense__category}", "date": "{addExpense__date}"}}
    assert {o["key"] for o in ops[1:]} == {
        "addExpense__title", "addExpense__amount",
        "addExpense__category", "addExpense__date"}


def test_form_injects_typed_draft_state():
    out = expand_composites(_form_defn(_EXPENSE_FORM))
    st = out["state"]
    assert st["addExpense__title"] == {"type": "string", "default": ""}
    assert st["addExpense__amount"] == {"type": "number", "default": 0}
    # first option preselected so the category is never unset
    assert st["addExpense__category"] == {"type": "string", "default": "Market"}
    # a date defaulting to today seeds the literal the item_add resolves
    assert st["addExpense__date"] == {"type": "string", "default": "today"}


def test_many_options_expand_to_a_select():
    form = {"type": "form", "id": "f", "into": "expenses",
            "fields": [{"field": "category",
                        "options": ["a", "b", "c", "d", "e"]}]}
    kids = expand_composites(_form_defn(form))["layout"][0]["children"]
    assert kids[0]["type"] == "select"


def test_expanded_form_validates():
    validate_definition(expand_composites(_form_defn(_EXPENSE_FORM)))


async def test_form_submit_adds_a_row_end_to_end(store):
    from datetime import datetime, timezone

    from flowly.flowlets.actions import apply_action

    utc = timezone.utc
    now = datetime(2026, 7, 12, 12, tzinfo=utc)
    f = store.create("Harcamalar", _form_defn(_EXPENSE_FORM))
    fid = f["id"]
    # Fill the drafts the way the client would (set inputs), then submit.
    await apply_action(store, fid, "addExpense_title", "Cepax", tz=utc)
    await apply_action(store, fid, "addExpense_amount", 5292.5, tz=utc)
    await apply_action(store, fid, "addExpense_category", "Market", tz=utc)
    res = await apply_action(store, fid, "addExpense_submit", None, tz=utc)
    rows = res["values"]["expenses"]
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Cepax"
    assert row["amount"] == 5292.5
    assert row["category"] == "Market"
    assert row["date"]  # today's date resolved from the "today" draft default
    # drafts cleared for the next entry
    assert res["values"]["addExpense__title"] == ""


def test_form_needs_a_mutable_list():
    d = {"catalog": 3, "name": "x",
         "state": {"c": {"type": "list", "source": True, "item": {"t": "string"}}},
         "layout": [{"type": "form", "id": "f", "into": "c",
                     "fields": [{"field": "t"}]}]}
    with pytest.raises(FlowletValidationError, match="read-only"):
        validate_definition(d)


def test_form_field_must_be_on_the_schema():
    with pytest.raises(FlowletValidationError, match="not on list"):
        validate_definition(_form_defn(
            {"type": "form", "id": "f", "into": "expenses",
             "fields": [{"field": "ghost"}]}))


def test_form_options_only_on_string_fields():
    with pytest.raises(FlowletValidationError, match="options are for string"):
        validate_definition(_form_defn(
            {"type": "form", "id": "f", "into": "expenses",
             "fields": [{"field": "amount", "options": ["1", "2"]}]}))


def test_form_needs_an_id():
    with pytest.raises(FlowletValidationError, match="missing required prop `id`"):
        validate_definition(_form_defn(
            {"type": "form", "into": "expenses", "fields": [{"field": "title"}]}))


# ── tracker_card ──────────────────────────────────────────────────────────────

_TRACKER = {"type": "tracker_card", "id": "spend", "list": "expenses",
            "field": "amount", "title": "Bu ay", "window": "30d", "chart": "bar"}


def test_tracker_expands_to_metric_and_chart_with_injected_computed():
    out = expand_composites(_form_defn(_TRACKER))
    card = out["layout"][0]
    assert card["type"] == "card"
    metric, chart = card["children"]
    assert metric["type"] == "metric" and metric["value"] == "spend__agg"
    assert chart["type"] == "chart" and chart["kind"] == "bar"
    assert chart["data"]["list"] == "expenses" and chart["data"]["field"] == "amount"
    comp = out["computed"]["spend__agg"]
    assert comp["list"] == "expenses" and comp["agg"] == "sum"
    assert comp["field"] == "amount"
    assert "days_since(date)" in comp["where"]      # windowed over the date field


async def test_tracker_metric_is_the_list_total(store):
    from datetime import datetime, timezone

    from flowly.flowlets.queries import resolve_values

    utc = timezone.utc
    now_ms = int(datetime(2026, 7, 12, 12, tzinfo=utc).timestamp() * 1000)
    defn = _form_defn(_TRACKER)
    rows = [{"id": "a", "amount": 200, "date": "2026-07-11", "category": "Fatura",
             "title": "x"},
            {"id": "b", "amount": 100, "date": "2026-07-10", "category": "Market",
             "title": "y"}]
    vals = resolve_values(defn, {"expenses": rows}, [], now_ms, utc)
    assert vals["spend__agg"] == 300               # pure function of the rows


def test_tracker_pie_prefers_a_category_like_field():
    t = {"type": "tracker_card", "id": "cat", "list": "expenses",
         "field": "amount", "chart": "pie"}
    chart = expand_composites(_form_defn(t))["layout"][0]["children"][1]
    assert chart["data"]["by"] == "category"        # not 'title' (category-like wins)
    assert chart["data"]["agg"] == "sum"


def test_tracker_pie_honors_an_explicit_by():
    t = {"type": "tracker_card", "id": "cat", "list": "expenses",
         "field": "amount", "chart": "donut", "by": "note"}
    chart = expand_composites(_form_defn(t))["layout"][0]["children"][1]
    assert chart["data"]["by"] == "note"


def test_tracker_field_must_be_numeric():
    with pytest.raises(FlowletValidationError, match="number field"):
        validate_definition(_form_defn(
            {"type": "tracker_card", "id": "t", "list": "expenses", "field": "category"}))


def test_tracker_expanded_validates():
    validate_definition(expand_composites(_form_defn(_TRACKER)))


def test_tracker_headlines_the_grid_tile_preview():
    # flowlet_preview runs on the STORED (composite) definition — it must expand
    # to find the metric, or a tracker flowlet's tile would show only an icon.
    from flowly.flowlets.queries import flowlet_preview
    defn = _form_defn(_TRACKER)
    pv = flowlet_preview(defn, {"spend__agg": 300})
    assert pv is not None and "300" in pv["text"] and "Bu ay" in pv["text"]
