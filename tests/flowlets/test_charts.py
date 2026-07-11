"""Rich charts (catalog 2) — multi-series overlay, categorical pie/donut, and
list-backed scatter: resolve shapes + author-time validation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from flowly.flowlets import catalog
from flowly.flowlets.queries import _category_breakdown, resolve_values
from flowly.flowlets.schema import FlowletValidationError, validate_definition

UTC = timezone.utc


def _ms(y, mo, d, h=12, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def _ev(series, value, ts, category=None):
    e = {"series": series, "value": value, "ts": ts}
    e["meta"] = {"category": category} if category is not None else None
    return e


# ── multi-series overlay ──────────────────────────────────────────────────────

def test_multi_series_resolves_to_multi_shape():
    now = _ms(2026, 7, 8, 23)
    defn = {
        "catalog": 2, "name": "Kilo",
        "series": {"weight": {}, "goal": {}},
        "layout": [{
            "type": "chart", "id": "wc", "kind": "line",
            "data": {"series": [{"key": "weight", "label": "Kilo"},
                                {"key": "goal", "color": "#8b5cf6"}],
                     "agg": "avg", "bucket": "day", "window": "7d"},
        }],
    }
    events = [
        _ev("weight", 80, _ms(2026, 7, 8, 9)),
        _ev("goal", 75, _ms(2026, 7, 8, 9)),
    ]
    out = resolve_values(defn, {}, events, now, UTC)
    assert isinstance(out["wc"], dict) and "multi" in out["wc"]
    multi = out["wc"]["multi"]
    assert [m["k"] for m in multi] == ["weight", "goal"]
    # every series has a full 7-bucket window; the weight series' last bucket = 80
    assert all(len(m["points"]) == len(multi[0]["points"]) for m in multi)
    assert multi[0]["points"][-1]["v"] == 80
    assert multi[1]["points"][-1]["v"] == 75


def test_single_series_string_form_unchanged():
    now = _ms(2026, 7, 8, 23)
    defn = {
        "catalog": 2, "name": "Su", "series": {"water": {}},
        "layout": [{"type": "chart", "id": "wc",
                    "data": {"series": "water", "window": "today", "bucket": "hour"}}],
    }
    out = resolve_values(defn, {}, [_ev("water", 250, _ms(2026, 7, 8, 9))], now, UTC)
    assert isinstance(out["wc"], list)           # unchanged [{t, v}] shape
    assert sum(b["v"] for b in out["wc"]) == 250


# ── categorical breakdown (pie / donut) ───────────────────────────────────────

def test_category_breakdown_groups_and_sorts():
    now = _ms(2026, 7, 30, 23)
    events = [
        _ev("spend", 100, _ms(2026, 7, 10), "food"),
        _ev("spend", 40, _ms(2026, 7, 11), "transport"),
        _ev("spend", 50, _ms(2026, 7, 12), "food"),
        _ev("spend", 10, _ms(2026, 7, 13), None),        # → "other"
    ]
    rows = _category_breakdown(events, "sum", "30d", now, UTC)
    assert rows == [{"k": "food", "v": 150}, {"k": "transport", "v": 40}, {"k": "other", "v": 10}]


def test_category_count_agg_is_a_tally():
    now = _ms(2026, 7, 30, 23)
    events = [
        _ev("s", 999, _ms(2026, 7, 10), "a"),
        _ev("s", 1, _ms(2026, 7, 11), "a"),
        _ev("s", 1, _ms(2026, 7, 12), "b"),
    ]
    rows = _category_breakdown(events, "count", "30d", now, UTC)
    assert rows == [{"k": "a", "v": 2}, {"k": "b", "v": 1}]


def test_category_window_filters_old_events():
    now = _ms(2026, 7, 30, 23)
    events = [
        _ev("s", 5, _ms(2026, 7, 29), "recent"),
        _ev("s", 9, _ms(2026, 1, 1), "old"),   # outside 7d
    ]
    rows = _category_breakdown(events, "sum", "7d", now, UTC)
    assert rows == [{"k": "recent", "v": 5}]


def test_category_caps_slices_and_folds_tail_into_other():
    now = _ms(2026, 7, 30, 23)
    # 10 distinct categories, descending values 100..10
    events = [_ev("s", 100 - i * 10, _ms(2026, 7, 10 + i), f"c{i}") for i in range(10)]
    rows = _category_breakdown(events, "sum", "30d", now, UTC)
    assert len(rows) == catalog.MAX_PIE_SLICES
    assert rows[-1]["k"] == "other"
    # 7 top slices kept (100..40), the remaining three (30+20+10) fold in
    assert rows[-1]["v"] == 30 + 20 + 10


def test_category_tail_merges_existing_other():
    now = _ms(2026, 7, 30, 23)
    # "other" is a real high category; low tail must merge into it, not duplicate
    events = [_ev("s", 100 - i * 5, _ms(2026, 7, 10 + i), f"c{i}") for i in range(9)]
    events.append(_ev("s", 200, _ms(2026, 7, 20), "other"))
    rows = _category_breakdown(events, "sum", "30d", now, UTC)
    assert [r["k"] for r in rows].count("other") == 1


def test_category_resolves_through_the_component_pass():
    now = _ms(2026, 7, 30, 23)
    defn = {
        "catalog": 2, "name": "Harcama", "series": {"spend": {}},
        "layout": [{"type": "chart", "id": "pie", "kind": "pie",
                    "data": {"series": "spend", "by": "category", "agg": "sum", "window": "30d"}}],
    }
    events = [_ev("spend", 30, _ms(2026, 7, 10), "food")]
    out = resolve_values(defn, {}, events, now, UTC)
    assert out["pie"] == [{"k": "food", "v": 30}]


# ── scatter (list-backed) ─────────────────────────────────────────────────────

def test_scatter_is_skipped_in_resolve():
    now = _ms(2026, 7, 8, 23)
    defn = {
        "catalog": 2, "name": "Koşular",
        "state": {"runs": {"type": "list", "item": {"km": "number", "pace": "number"}}},
        "layout": [{"type": "chart", "id": "plot", "kind": "scatter",
                    "data": {"list": "runs", "x": "km", "y": "pace"}}],
    }
    out = resolve_values(defn, {}, [], now, UTC)
    assert "plot" not in out          # the client reads `runs` directly
    assert "runs" in out              # the list itself is present


# ── list-backed time/category charts ──────────────────────────────────────────
# A chart ABOUT a list aggregates the rows themselves — no parallel series to
# drift (a vision add never logged; item_remove couldn't un-log; deleted
# expenses haunted the charts).

_EXPENSES_DEF = {
    "catalog": 2, "name": "Harcamalar",
    "state": {"expenses": {"type": "list", "item": {
        "title": "string", "amount": "number", "category": "string", "date": "date",
    }}},
    "layout": [
        {"type": "chart", "id": "trend", "kind": "bar",
         "data": {"list": "expenses", "field": "amount", "bucket": "day", "window": "30d"}},
        {"type": "chart", "id": "pie", "kind": "donut",
         "data": {"list": "expenses", "by": "category", "field": "amount",
                  "agg": "sum", "window": "30d"}},
    ],
}


def _expense(id, amount, date, category="Fatura"):
    return {"id": id, "title": id, "amount": amount, "category": category, "date": date}


def test_list_time_chart_aggregates_the_rows():
    now = _ms(2026, 7, 11, 23)
    rows = [_expense("a", 200, "2026-07-11"), _expense("b", 5292.5, "2026-07-08")]
    out = resolve_values(_EXPENSES_DEF, {"expenses": rows}, [], now, UTC)
    by_day = {b["t"]: b["v"] for b in out["trend"]}
    assert by_day["2026-07-11"] == 200
    assert by_day["2026-07-08"] == 5292.5


def test_list_chart_reflects_a_deleted_row():
    # THE bug: delete an expense → the charts must forget it (pure function of
    # the list, unlike a logged series which couldn't be un-logged).
    now = _ms(2026, 7, 11, 23)
    rows = [_expense("b", 5292.5, "2026-07-08", category="Alışveriş")]
    out = resolve_values(_EXPENSES_DEF, {"expenses": rows}, [], now, UTC)
    by_day = {b["t"]: b["v"] for b in out["trend"]}
    assert by_day["2026-07-11"] == 0            # the deleted 200 is GONE
    assert by_day["2026-07-08"] == 5292.5
    assert out["pie"] == [{"k": "Alışveriş", "v": 5292.5}]


def test_list_category_chart_groups_and_sums():
    now = _ms(2026, 7, 11, 23)
    rows = [
        _expense("a", 200, "2026-07-11", category="Fatura"),
        _expense("b", 100, "2026-07-10", category="Fatura"),
        _expense("c", 50, "2026-07-09", category="Ulaşım"),
    ]
    out = resolve_values(_EXPENSES_DEF, {"expenses": rows}, [], now, UTC)
    assert out["pie"] == [{"k": "Fatura", "v": 300}, {"k": "Ulaşım", "v": 50}]


def test_list_category_count_without_field_tallies_rows():
    now = _ms(2026, 7, 11, 23)
    defn = {
        "catalog": 2, "name": "x",
        "state": {"tasks": {"type": "list", "item": {"title": "string", "status": "string"}}},
        "layout": [{"type": "chart", "id": "c", "kind": "pie",
                    "data": {"list": "tasks", "by": "status", "agg": "count"}}],
    }
    rows = [{"id": "1", "title": "a", "status": "open"},
            {"id": "2", "title": "b", "status": "open"},
            {"id": "3", "title": "c", "status": "done"}]
    out = resolve_values(defn, {"tasks": rows}, [], now, UTC)
    assert out["c"] == [{"k": "open", "v": 2}, {"k": "done", "v": 1}]


def test_list_time_chart_skips_undated_and_windowed_out_rows():
    now = _ms(2026, 7, 11, 23)
    rows = [
        _expense("ok", 100, "2026-07-11"),
        _expense("undated", 999, ""),               # unparseable date → skipped
        _expense("old", 500, "2025-01-01"),         # outside 30d → excluded
    ]
    out = resolve_values(_EXPENSES_DEF, {"expenses": rows}, [], now, UTC)
    assert sum(b["v"] for b in out["trend"]) == 100


def test_list_time_chart_window_filters_the_category_form_too():
    now = _ms(2026, 7, 11, 23)
    rows = [_expense("old", 500, "2025-01-01", category="Eski")]
    out = resolve_values(_EXPENSES_DEF, {"expenses": rows}, [], now, UTC)
    assert out["pie"] == []


def test_list_chart_skips_rows_missing_the_number_field():
    now = _ms(2026, 7, 11, 23)
    rows = [{"id": "x", "title": "no amount", "category": "Fatura", "date": "2026-07-11"}]
    out = resolve_values(_EXPENSES_DEF, {"expenses": rows}, [], now, UTC)
    assert sum(b["v"] for b in out["trend"]) == 0
    assert out["pie"] == []


def test_list_category_over_an_undated_list_keeps_all_rows():
    # No date field on the item schema → rows can't be windowed out.
    now = _ms(2026, 7, 11, 23)
    defn = {
        "catalog": 2, "name": "x",
        "state": {"books": {"type": "list", "item": {"title": "string", "genre": "string"}}},
        "layout": [{"type": "chart", "id": "c", "kind": "pie",
                    "data": {"list": "books", "by": "genre", "agg": "count", "window": "7d"}}],
    }
    rows = [{"id": "1", "title": "a", "genre": "sci-fi"}]
    out = resolve_values(defn, {"books": rows}, [], now, UTC)
    assert out["c"] == [{"k": "sci-fi", "v": 1}]


# ── shadow-series redirect ────────────────────────────────────────────────────
# Old-pattern flowlets pair item_add with a log into a parallel series and bind
# their charts to the SERIES. Those charts drift (a vision add never logs; a
# delete can't un-log) — so resolution detects the shadow pairing and computes
# the chart from the LIST rows, healing existing flowlets with zero re-author.

def _shadow_defn():
    # Mirrors the user's real "Harcama Takibi": batch[item_add expenses,
    # log spend], charts bound to `spend`.
    return {
        "catalog": 2, "name": "Harcama Takibi",
        "series": {"spend": {"unit": "₺"}},
        "state": {
            "draftTitle": {"type": "string", "default": ""},
            "draftCategory": {"type": "string", "default": "Market"},
            "expenses": {"type": "list", "item": {
                "title": "string", "amount": "number",
                "category": "string", "date": "date"}},
        },
        "layout": [
            {"id": "amountIn", "type": "number_input",
             "action": {"op": "batch", "ops": [
                 {"op": "item_add", "key": "expenses", "fields": {
                     "title": "{draftTitle}", "amount": "{value}",
                     "category": "{draftCategory}", "date": "today"}},
                 {"op": "log", "series": "spend", "value": "{value}",
                  "category": "{draftCategory}"},
             ]}},
            {"type": "chart", "id": "dailySpend", "kind": "bar",
             "data": {"series": "spend", "agg": "sum", "bucket": "day", "window": "30d"}},
            {"type": "chart", "id": "categoryBreakdown", "kind": "pie",
             "data": {"series": "spend", "by": "category", "agg": "sum", "donut": True}},
        ],
    }


def test_shadow_series_chart_resolves_from_the_list():
    # The user's exact bug: Fatura 200 + Market 3000 were logged then their
    # rows DELETED; only Cepax 5292.5 remains in the list. The stale events
    # must not haunt the charts.
    now = _ms(2026, 7, 12, 12)
    stale_events = [_ev("spend", 200, _ms(2026, 7, 11), "Fatura"),
                    _ev("spend", 3000, _ms(2026, 7, 12), "Market")]
    rows = [{"id": "i1", "title": "Cepax Bilişim alışveriş", "amount": 5292.5,
             "category": "Alışveriş", "date": "2026-07-08"}]
    out = resolve_values(_shadow_defn(), {"expenses": rows}, stale_events, now, UTC)
    assert out["categoryBreakdown"] == [{"k": "Alışveriş", "v": 5292.5}]
    by_day = {b["t"]: b["v"] for b in out["dailySpend"]}
    assert by_day["2026-07-08"] == 5292.5
    assert by_day["2026-07-11"] == 0     # deleted Fatura: gone
    assert by_day["2026-07-12"] == 0     # deleted Market: gone


def test_shadow_redirect_sees_a_vision_added_row():
    # A receipt lands in the list WITHOUT a log — the chart must still count it.
    now = _ms(2026, 7, 12, 12)
    rows = [{"id": "i1", "title": "fiş", "amount": 750,
             "category": "Market", "date": "2026-07-12"}]
    out = resolve_values(_shadow_defn(), {"expenses": rows}, [], now, UTC)
    assert out["categoryBreakdown"] == [{"k": "Market", "v": 750}]


def test_non_shadow_series_stays_event_based():
    # A plain log tracker (no item_add pairing) keeps its event math.
    now = _ms(2026, 7, 12, 12)
    defn = {
        "catalog": 2, "name": "Su", "series": {"water": {}},
        "layout": [
            {"id": "drink", "type": "button", "text": "250",
             "action": {"op": "log", "series": "water", "value": 250}},
            {"type": "chart", "id": "week",
             "data": {"series": "water", "agg": "sum", "bucket": "day", "window": "7d"}},
        ],
    }
    events = [_ev("water", 250, _ms(2026, 7, 12, 9))]
    out = resolve_values(defn, {}, events, now, UTC)
    assert {b["t"]: b["v"] for b in out["week"]}["2026-07-12"] == 250


def test_shadow_without_a_date_field_falls_back_to_events():
    # A dateless list can't be time-bucketed — better a stale chart than an
    # empty one; the redirect abandons and the event math stays.
    now = _ms(2026, 7, 12, 12)
    d = _shadow_defn()
    d["state"]["expenses"]["item"] = {"title": "string", "amount": "number",
                                      "category": "string"}  # no date
    events = [_ev("spend", 100, _ms(2026, 7, 12, 9))]
    out = resolve_values(d, {"expenses": []}, events, now, UTC)
    assert {b["t"]: b["v"] for b in out["dailySpend"]}["2026-07-12"] == 100
    # ...but the category chart needs no dates → it DOES redirect (empty list
    # → no slices, the stale Fatura/Market events are ignored).
    assert out["categoryBreakdown"] == []


# ── validation ────────────────────────────────────────────────────────────────

def _chart(data, *, series=None, state=None):
    return {
        "catalog": 2, "name": "x",
        "series": series if series is not None else {"a": {}, "b": {}},
        **({"state": state} if state else {}),
        "layout": [{"type": "chart", "id": "c", "data": data}],
    }


def test_valid_multi_series_passes():
    validate_definition(_chart({"series": [{"key": "a"}, {"key": "b", "color": "#22c55e"}]}))


def test_multi_series_rejects_unknown_key():
    with pytest.raises(FlowletValidationError, match="declared series"):
        validate_definition(_chart({"series": [{"key": "a"}, {"key": "nope"}]}))


def test_multi_series_rejects_too_many():
    series = {k: {} for k in ("a", "b", "c", "d", "e")}
    entries = [{"key": k} for k in series]
    with pytest.raises(FlowletValidationError, match="2–"):
        validate_definition(_chart({"series": entries}, series=series))


def test_multi_series_rejects_duplicate():
    with pytest.raises(FlowletValidationError, match="listed twice"):
        validate_definition(_chart({"series": [{"key": "a"}, {"key": "a"}]}))


def test_multi_series_rejects_bad_color():
    with pytest.raises(FlowletValidationError, match="color"):
        validate_definition(_chart({"series": [{"key": "a"}, {"key": "b", "color": "red"}]}))


def test_stacked_only_for_bar():
    with pytest.raises(FlowletValidationError, match="bar"):
        validate_definition({
            "catalog": 2, "name": "x", "series": {"a": {}, "b": {}},
            "layout": [{"type": "chart", "id": "c", "kind": "line",
                        "data": {"series": [{"key": "a"}, {"key": "b"}], "stacked": True}}],
        })


def test_valid_category_passes():
    validate_definition(_chart({"series": "a", "by": "category", "agg": "sum"}))


def test_category_rejects_avg_agg():
    with pytest.raises(FlowletValidationError, match="categorical"):
        validate_definition(_chart({"series": "a", "by": "category", "agg": "avg"}))


def test_category_rejects_bucket():
    with pytest.raises(FlowletValidationError, match="no time axis"):
        validate_definition(_chart({"series": "a", "by": "category", "bucket": "day"}))


def test_valid_scatter_passes():
    validate_definition(_chart(
        {"list": "runs", "x": "km", "y": "pace"},
        state={"runs": {"type": "list", "item": {"km": "number", "pace": "number"}}},
    ))


def test_scatter_rejects_non_number_axis():
    with pytest.raises(FlowletValidationError, match="must be a number"):
        validate_definition(_chart(
            {"list": "runs", "x": "km", "y": "label"},
            state={"runs": {"type": "list", "item": {"km": "number", "label": "string"}}},
        ))


def test_scatter_rejects_unknown_list():
    with pytest.raises(FlowletValidationError, match="declared list"):
        validate_definition(_chart({"list": "ghost", "x": "km", "y": "pace"}))


_EXP_STATE = {"expenses": {"type": "list", "item": {
    "title": "string", "amount": "number", "category": "string", "date": "date"}}}


def test_valid_list_time_chart_passes():
    validate_definition(_chart(
        {"list": "expenses", "field": "amount", "bucket": "day", "window": "30d"},
        state=_EXP_STATE,
    ))


def test_valid_list_category_chart_passes():
    validate_definition(_chart(
        {"list": "expenses", "by": "category", "field": "amount", "agg": "sum"},
        state=_EXP_STATE,
    ))


def test_list_chart_rejects_unknown_list():
    with pytest.raises(FlowletValidationError, match="declared list"):
        validate_definition(_chart({"list": "ghost", "field": "amount"}))


def test_list_chart_rejects_non_number_field():
    with pytest.raises(FlowletValidationError, match="number field"):
        validate_definition(_chart({"list": "expenses", "field": "title"}, state=_EXP_STATE))


def test_list_chart_rejects_non_string_by():
    with pytest.raises(FlowletValidationError, match="string field"):
        validate_definition(_chart(
            {"list": "expenses", "by": "amount", "agg": "count"}, state=_EXP_STATE,
        ))


def test_list_sum_by_category_requires_a_field():
    with pytest.raises(FlowletValidationError, match="needs `data.field`"):
        validate_definition(_chart({"list": "expenses", "by": "category"}, state=_EXP_STATE))


def test_list_time_chart_requires_a_date_field():
    with pytest.raises(FlowletValidationError, match="date field"):
        validate_definition(_chart(
            {"list": "tasks", "field": "n"},
            state={"tasks": {"type": "list", "item": {"title": "string", "n": "number"}}},
        ))


def test_list_category_rejects_bucket():
    with pytest.raises(FlowletValidationError, match="no time axis"):
        validate_definition(_chart(
            {"list": "expenses", "by": "category", "field": "amount", "bucket": "day"},
            state=_EXP_STATE,
        ))


def test_list_chart_rejects_bad_explicit_date():
    with pytest.raises(FlowletValidationError, match="date field"):
        validate_definition(_chart(
            {"list": "expenses", "field": "amount", "date": "title"}, state=_EXP_STATE,
        ))


def test_new_forms_rejected_on_sparkline():
    with pytest.raises(FlowletValidationError, match="only for `chart`"):
        validate_definition({
            "catalog": 2, "name": "x", "series": {"a": {}, "b": {}},
            "layout": [{"type": "sparkline", "id": "s",
                        "data": {"series": [{"key": "a"}, {"key": "b"}]}}],
        })


def test_log_category_validation():
    for cat, ok in (("food", True), ("", False), ("x" * 200, False), (5, False)):
        defn = {
            "catalog": 2, "name": "x", "series": {"spend": {}},
            "layout": [{"type": "button", "id": "b", "text": "add",
                        "action": {"op": "log", "series": "spend", "value": 10,
                                   "category": cat}}],
        }
        if ok:
            validate_definition(defn)
        else:
            with pytest.raises(FlowletValidationError, match="category"):
                validate_definition(defn)


# ── log category → event meta (feeds the pie) ─────────────────────────────────

async def test_log_literal_category_stored_in_meta(store):
    from flowly.flowlets.actions import apply_action
    defn = {
        "catalog": 2, "name": "Harcama", "series": {"spend": {}},
        "layout": [{"type": "button", "id": "food", "text": "Yemek 250",
                    "action": {"op": "log", "series": "spend", "value": 250,
                               "category": "food"}}],
    }
    f = store.create("Harcama", defn)
    await apply_action(store, f["id"], "food", tz=UTC)
    events = store.get_events(f["id"])
    assert len(events) == 1
    assert events[0]["value"] == 250
    assert events[0]["meta"] == {"category": "food"}


async def test_log_templated_category_from_state(store):
    from flowly.flowlets.actions import apply_action
    defn = {
        "catalog": 2, "name": "Harcama",
        "series": {"spend": {}},
        "state": {"cat": {"type": "string", "default": "transport"}},
        "layout": [
            {"type": "select", "id": "cat", "options": ["food", "transport"],
             "action": {"op": "set", "key": "cat"}},
            {"type": "button", "id": "add", "text": "Ekle",
             "action": {"op": "log", "series": "spend", "value": 40, "category": "{cat}"}},
        ],
    }
    f = store.create("Harcama", defn)
    await apply_action(store, f["id"], "add", tz=UTC)          # default state → "transport"
    assert store.get_events(f["id"])[0]["meta"] == {"category": "transport"}
