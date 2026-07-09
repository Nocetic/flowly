"""Data-bound table (catalog 2) — static `rows` vs `source` + `columns`."""

from __future__ import annotations

import pytest

from flowly.flowlets.schema import FlowletValidationError, validate_definition


def _def(table: dict, *, state: dict | None = None) -> dict:
    return {
        "catalog": 2, "name": "x",
        **({"state": state} if state else {}),
        "layout": [{"type": "table", "id": "t", **table}],
    }


_LIST_STATE = {"prs": {"type": "list", "item": {"title": "string", "who": "string", "n": "number"}}}


def test_static_rows_still_valid():
    validate_definition(_def({"rows": [["a", "b"], ["c", "d"]]}))


def test_source_mode_valid():
    validate_definition(_def(
        {"source": "prs", "columns": [{"field": "title", "label": "Başlık"},
                                      {"field": "n", "align": "right", "width": "20%"}],
         "sortBy": {"field": "n", "dir": "desc"}, "empty": "yok"},
        state=_LIST_STATE,
    ))


def test_needs_exactly_one_mode():
    with pytest.raises(FlowletValidationError, match="exactly one"):
        validate_definition(_def({"rows": [], "source": "prs"}, state=_LIST_STATE))
    with pytest.raises(FlowletValidationError, match="exactly one"):
        validate_definition(_def({}))  # neither


def test_source_must_be_a_list():
    with pytest.raises(FlowletValidationError, match="declared list"):
        validate_definition(_def({"source": "ghost", "columns": [{"field": "title"}]}, state=_LIST_STATE))


def test_column_field_must_exist():
    with pytest.raises(FlowletValidationError, match="must name a field"):
        validate_definition(_def({"source": "prs", "columns": [{"field": "nope"}]}, state=_LIST_STATE))


def test_columns_count_bounds():
    with pytest.raises(FlowletValidationError, match="1–6"):
        validate_definition(_def({"source": "prs", "columns": []}, state=_LIST_STATE))
    cols = [{"field": "title"}] * 7
    with pytest.raises(FlowletValidationError, match="1–6"):
        validate_definition(_def({"source": "prs", "columns": cols}, state=_LIST_STATE))


def test_bad_align_rejected():
    with pytest.raises(FlowletValidationError, match="align"):
        validate_definition(_def(
            {"source": "prs", "columns": [{"field": "title", "align": "middle"}]}, state=_LIST_STATE))


def test_sortby_field_must_be_a_column():
    with pytest.raises(FlowletValidationError, match="sortBy"):
        validate_definition(_def(
            {"source": "prs", "columns": [{"field": "title"}], "sortBy": {"field": "n"}},
            state=_LIST_STATE))


def test_sortby_dir_validated():
    with pytest.raises(FlowletValidationError, match="asc or desc"):
        validate_definition(_def(
            {"source": "prs", "columns": [{"field": "n"}], "sortBy": {"field": "n", "dir": "up"}},
            state=_LIST_STATE))
