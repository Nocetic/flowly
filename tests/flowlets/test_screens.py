"""Drill-down / navigation (catalog 2) — top-level `screens` fragments pushed by
a repeater/table row `navigate`. Validated against the navigator's item scope."""

from __future__ import annotations

import pytest

from flowly.flowlets.schema import FlowletValidationError, validate_definition

_LIST = {"commits": {"type": "list", "item": {"title": "string", "who": "string"}}}


def _def(*, layout, screens, state=None):
    return {
        "catalog": 2, "name": "x",
        "state": state if state is not None else _LIST,
        "layout": layout,
        "screens": screens,
    }


def _repeater(**extra):
    return {"type": "repeater", "id": "list", "source": "commits",
            "item": {"type": "text", "text": "{$.title}"}, **extra}


def test_repeater_navigate_valid():
    validate_definition(_def(
        layout=[_repeater(navigate="detail")],
        screens={"detail": {"title": "{$.title}", "layout": [
            {"type": "text", "text": "{$.who}"},
            {"type": "text", "text": "by {$.who}"},
        ]}},
    ))


def test_table_navigate_valid():
    validate_definition(_def(
        layout=[{"type": "table", "id": "t", "source": "commits", "navigate": "detail",
                 "columns": [{"field": "title"}]}],
        screens={"detail": {"layout": [{"type": "text", "text": "{$.who}"}]}},
    ))


def test_navigate_unknown_screen():
    with pytest.raises(FlowletValidationError, match="declared in top-level `screens`"):
        validate_definition(_def(layout=[_repeater(navigate="ghost")], screens={}))


def test_screen_never_navigated_to():
    with pytest.raises(FlowletValidationError, match="never navigated to"):
        validate_definition(_def(
            layout=[_repeater()],
            screens={"orphan": {"layout": [{"type": "text", "text": "hi"}]}},
        ))


def test_screen_field_checked_against_navigator_list():
    # a `$.field` bind in the screen must be a field of the navigating list
    with pytest.raises(FlowletValidationError, match="ghost"):
        validate_definition(_def(
            layout=[_repeater(navigate="detail")],
            screens={"detail": {"layout": [{"type": "stat", "value": "$.ghost"}]}},
        ))


def test_screen_cannot_contain_a_repeater():
    with pytest.raises(FlowletValidationError, match="cannot nest inside another repeater"):
        validate_definition(_def(
            layout=[_repeater(navigate="detail")],
            screens={"detail": {"layout": [
                {"type": "repeater", "source": "commits", "item": {"type": "text", "text": "x"}},
            ]}},
        ))


def test_navigate_cannot_appear_inside_a_screen():
    with pytest.raises(FlowletValidationError, match="isn't allowed inside a screen"):
        validate_definition(_def(
            layout=[_repeater(navigate="detail")],
            screens={"detail": {"layout": [
                {"type": "table", "id": "inner", "source": "commits",
                 "columns": [{"field": "title"}], "navigate": "detail"},
            ]}},
        ))


def test_too_many_screens():
    screens = {f"s{i}": {"layout": [{"type": "text", "text": "x"}]} for i in range(7)}
    with pytest.raises(FlowletValidationError, match="too many screens"):
        validate_definition(_def(layout=[_repeater()], screens=screens))


def test_screen_layout_must_be_nonempty():
    with pytest.raises(FlowletValidationError, match="non-empty array"):
        validate_definition(_def(layout=[_repeater(navigate="d")], screens={"d": {"layout": []}}))


def test_screen_row_action_resolves_item_ops():
    # a delete button inside the detail screen targets the list — needs item scope
    validate_definition(_def(
        layout=[_repeater(navigate="detail")],
        screens={"detail": {"layout": [
            {"type": "button", "id": "del", "text": "Sil",
             "action": {"op": "item_remove", "key": "commits"}},
        ]}},
    ))
