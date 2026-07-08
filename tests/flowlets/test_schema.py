"""Definition validation — every fixture is valid, and every guard fires."""

from __future__ import annotations

import copy

import pytest

from flowly.flowlets import catalog
from flowly.flowlets.schema import FlowletValidationError, validate_definition

from .conftest import FIXTURE_NAMES, load_fixture


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_all_fixtures_valid(name):
    defn = load_fixture(name)
    assert validate_definition(defn) is defn


def test_missing_name():
    with pytest.raises(FlowletValidationError, match="name"):
        validate_definition({"catalog": 1, "layout": [{"type": "text", "text": "hi"}]})


def test_catalog_required_int():
    with pytest.raises(FlowletValidationError, match="catalog"):
        validate_definition({"name": "x", "layout": [{"type": "text", "text": "hi"}]})


def test_future_catalog_rejected():
    with pytest.raises(FlowletValidationError, match="newer"):
        validate_definition({
            "catalog": catalog.CATALOG_VERSION + 1,
            "name": "x",
            "layout": [{"type": "text", "text": "hi"}],
        })


def test_unknown_component_type():
    with pytest.raises(FlowletValidationError, match="unknown component type"):
        validate_definition({
            "catalog": 1, "name": "x",
            "layout": [{"type": "hologram"}],
        })


def test_slider_min_max():
    with pytest.raises(FlowletValidationError, match="must be < max"):
        validate_definition({
            "catalog": 1, "name": "x",
            "state": {"g": {"type": "number", "default": 1}},
            "layout": [{
                "id": "s", "type": "slider", "min": 4000, "max": 1000,
                "value": "g", "action": {"op": "set", "key": "g"},
            }],
        })


def test_action_needs_id():
    with pytest.raises(FlowletValidationError, match="needs a unique `id`"):
        validate_definition({
            "catalog": 1, "name": "x",
            "state": {"g": {"type": "number", "default": 0}},
            "layout": [{"type": "button", "text": "go",
                        "action": {"op": "increment", "key": "g"}}],
        })


def test_action_unknown_state_key():
    with pytest.raises(FlowletValidationError, match="declared state key"):
        validate_definition({
            "catalog": 1, "name": "x",
            "layout": [{"id": "b", "type": "button", "text": "go",
                        "action": {"op": "set", "key": "ghost"}}],
        })


def test_log_unknown_series():
    with pytest.raises(FlowletValidationError, match="declared series"):
        validate_definition({
            "catalog": 1, "name": "x",
            "layout": [{"id": "b", "type": "button", "text": "go",
                        "action": {"op": "log", "series": "ghost"}}],
        })


def test_bind_unknown_key():
    with pytest.raises(FlowletValidationError, match="unknown key"):
        validate_definition({
            "catalog": 1, "name": "x",
            "layout": [{"id": "p", "type": "progress", "value": "ghost"}],
        })


def test_duplicate_id():
    with pytest.raises(FlowletValidationError, match="duplicate component id"):
        validate_definition({
            "catalog": 1, "name": "x",
            "series": {"s": {}},
            "layout": [
                {"id": "b", "type": "button", "text": "a",
                 "action": {"op": "log", "series": "s"}},
                {"id": "b", "type": "button", "text": "b",
                 "action": {"op": "log", "series": "s"}},
            ],
        })


def test_chart_id_collides_with_state_key():
    # Only chart/sparkline/heatmap ids share the values namespace with scalars.
    with pytest.raises(FlowletValidationError, match="collides with a state"):
        validate_definition({
            "catalog": 1, "name": "x",
            "state": {"goal": {"type": "number", "default": 1}},
            "series": {"s": {}},
            "layout": [{"id": "goal", "type": "chart",
                        "data": {"series": "s"}}],
        })


def test_input_id_may_equal_state_key():
    # An input writing to state key `note` may itself be id `note`.
    defn = {
        "catalog": 1, "name": "x",
        "state": {"note": {"type": "string", "default": ""}},
        "layout": [{"id": "note", "type": "input",
                    "action": {"op": "set", "key": "note"}}],
    }
    assert validate_definition(defn) is defn


def test_computed_expr_bad_symbol():
    with pytest.raises(FlowletValidationError):
        validate_definition({
            "catalog": 1, "name": "x",
            "state": {"g": {"type": "number", "default": 1}},
            "computed": {"bad": {"expr": "__import__('os').system('x')"}},
            "layout": [{"type": "stat", "value": "bad"}],
        })


def test_computed_needs_one_of_series_expr():
    with pytest.raises(FlowletValidationError, match="exactly one"):
        validate_definition({
            "catalog": 1, "name": "x",
            "series": {"s": {}},
            "computed": {"c": {"series": "s", "expr": "1+1"}},
            "layout": [{"type": "stat", "value": "c"}],
        })


def test_depth_limit():
    node: dict = {"type": "text", "text": "deep"}
    for _ in range(catalog.MAX_DEPTH + 2):
        node = {"type": "card", "children": [node]}
    with pytest.raises(FlowletValidationError, match="nested too deep"):
        validate_definition({"catalog": 1, "name": "x", "layout": [node]})


def test_component_count_limit():
    layout = [{"type": "text", "text": str(i)} for i in range(catalog.MAX_COMPONENTS + 5)]
    with pytest.raises(FlowletValidationError, match="too many components"):
        validate_definition({"catalog": 1, "name": "x", "layout": layout})


def test_checklist_item_key_must_be_state(water_def=None):
    with pytest.raises(FlowletValidationError, match="declared state key"):
        validate_definition({
            "catalog": 1, "name": "x",
            "state": {"a": {"type": "bool", "default": False}},
            "layout": [{"id": "c", "type": "checklist",
                        "items": [{"key": "a"}, {"key": "ghost"}]}],
        })


def test_batch_no_nesting():
    with pytest.raises(FlowletValidationError, match="cannot nest"):
        validate_definition({
            "catalog": 1, "name": "x",
            "state": {"g": {"type": "number", "default": 0}},
            "layout": [{"id": "b", "type": "button", "text": "go", "action": {
                "op": "batch", "ops": [
                    {"op": "increment", "key": "g"},
                    {"op": "batch", "ops": []},
                ]}}],
        })


def test_accent_hex_validation():
    defn = load_fixture("water")
    bad = copy.deepcopy(defn)
    bad["accent"] = "turquoise"
    with pytest.raises(FlowletValidationError, match="hex color"):
        validate_definition(bad)
