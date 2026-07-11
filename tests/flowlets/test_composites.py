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
