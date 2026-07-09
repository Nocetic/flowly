"""List search / sort / filter (catalog 2) — `search` component + repeater/table
`where` + `sortBy` validation. Filtering + sorting run client-side; the bot only
validates the declaration."""

from __future__ import annotations

import pytest

from flowly.flowlets.schema import FlowletValidationError, validate_definition

_TASKS = {"tasks": {"type": "list", "item": {"title": "string", "done": "bool", "due": "date"}}}


def _def(*layout, state=None):
    return {
        "catalog": 2, "name": "x",
        "state": state if state is not None else _TASKS,
        "layout": list(layout),
    }


def _repeater(**extra):
    return {"type": "repeater", "id": "list", "source": "tasks",
            "item": {"type": "text", "text": "{$.title}"}, **extra}


# ── repeater where / sortBy ───────────────────────────────────────────────────

def test_repeater_where_and_sortby_valid():
    validate_definition(_def(_repeater(where="done == 0", sortBy={"field": "due", "dir": "asc"})))


def test_repeater_where_unknown_field():
    with pytest.raises(FlowletValidationError, match="unknown field 'ghost'"):
        validate_definition(_def(_repeater(where="ghost == 1")))


def test_repeater_where_allows_date_fn():
    validate_definition(_def(_repeater(where="days_until(due) <= 1")))


def test_repeater_sortby_unknown_field():
    with pytest.raises(FlowletValidationError, match="sortBy.field"):
        validate_definition(_def(_repeater(sortBy={"field": "ghost"})))


def test_repeater_sortby_bad_dir():
    with pytest.raises(FlowletValidationError, match="asc or desc"):
        validate_definition(_def(_repeater(sortBy={"field": "title", "dir": "up"})))


# ── search component ──────────────────────────────────────────────────────────

def test_search_targets_a_repeater():
    validate_definition(_def(
        {"type": "search", "target": "list", "placeholder": "Ara…", "fields": ["title"]},
        _repeater(),
    ))


def test_search_targets_a_source_table():
    validate_definition(_def(
        {"type": "search", "target": "tbl"},
        {"type": "table", "id": "tbl", "source": "tasks", "columns": [{"field": "title"}]},
    ))


def test_search_target_must_exist():
    with pytest.raises(FlowletValidationError, match="must name a repeater"):
        validate_definition(_def({"type": "search", "target": "ghost"}, _repeater()))


def test_search_target_cannot_be_a_plain_component():
    with pytest.raises(FlowletValidationError, match="must name a repeater"):
        validate_definition(_def(
            {"type": "search", "target": "hdr"},
            {"type": "header", "id": "hdr", "text": "hi"},
            _repeater(),
        ))


def test_search_fields_must_be_list_fields():
    with pytest.raises(FlowletValidationError, match="not a field"):
        validate_definition(_def(
            {"type": "search", "target": "list", "fields": ["ghost"]},
            _repeater(),
        ))


def test_search_forward_reference_ok():
    # search appears BEFORE its target in reading order — must still validate.
    validate_definition(_def(
        {"type": "search", "target": "list"},
        _repeater(),
    ))


def test_search_requires_target():
    with pytest.raises(FlowletValidationError, match="target"):
        validate_definition(_def({"type": "search"}, _repeater()))
