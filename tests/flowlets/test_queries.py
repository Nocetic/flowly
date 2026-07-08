"""Aggregation, computed resolution, safe-expr guarantees, timezone rollover."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from flowly.flowlets.queries import (
    _UnresolvedNameError,
    aggregate_buckets,
    aggregate_scalar,
    eval_expr,
    resolve_values,
    validate_expr,
)

UTC = timezone.utc


def _ms(y, mo, d, h=12, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


# ── expr safety ───────────────────────────────────────────────────────────────

def test_expr_basic():
    assert eval_expr("max(0, a - b)", {"a": 5, "b": 2}) == 3
    assert eval_expr("a * 2 + 1", {"a": 10}) == 21


def test_expr_rejects_calls_and_attrs():
    for bad in ("__import__('os')", "a.foo", "open('x')", "[1,2][0]", "lambda: 1"):
        with pytest.raises(ValueError):
            validate_expr(bad)


def test_expr_unresolved_name_raises():
    with pytest.raises(_UnresolvedNameError):
        eval_expr("a + missing", {"a": 1})


def test_expr_div_by_zero_is_zero():
    assert eval_expr("a / b", {"a": 1, "b": 0}) == 0.0


# ── scalar aggregation over windows ───────────────────────────────────────────

def test_sum_today_window():
    now = _ms(2026, 7, 8, 15)
    events = [
        {"value": 250, "ts": _ms(2026, 7, 8, 9)},
        {"value": 250, "ts": _ms(2026, 7, 8, 14)},
        {"value": 500, "ts": _ms(2026, 7, 7, 20)},   # yesterday, excluded
    ]
    assert aggregate_scalar(events, "sum", "today", now, UTC) == 500


def test_count_and_avg():
    now = _ms(2026, 7, 8, 23)
    events = [{"value": v, "ts": _ms(2026, 7, 8, 10 + i)} for i, v in enumerate([2, 4, 6])]
    assert aggregate_scalar(events, "count", "today", now, UTC) == 3
    assert aggregate_scalar(events, "avg", "today", now, UTC) == 4
    assert aggregate_scalar(events, "max", "today", now, UTC) == 6
    assert aggregate_scalar(events, "last", "today", now, UTC) == 6


def test_empty_series_is_zero():
    assert aggregate_scalar([], "sum", "today", _ms(2026, 7, 8), UTC) == 0.0
    assert aggregate_scalar([], "avg", "7d", _ms(2026, 7, 8), UTC) == 0.0


# ── bucketed series for charts ────────────────────────────────────────────────

def test_buckets_fill_empty_days():
    now = _ms(2026, 7, 8, 12)
    events = [
        {"value": 1000, "ts": _ms(2026, 7, 8, 9)},
        {"value": 500, "ts": _ms(2026, 7, 6, 9)},
    ]
    out = aggregate_buckets(events, "sum", "day", "7d", now, UTC)
    assert len(out) == 7                      # today + 6 prior days
    assert out[-1] == {"t": "2026-07-08", "v": 1000.0}
    assert {"t": "2026-07-06", "v": 500.0} in out
    assert {"t": "2026-07-07", "v": 0.0} in out  # empty day present as zero


def test_day_rollover_boundary():
    # An event at 23:59 counts for its day; 00:01 next day starts fresh.
    late = {"value": 300, "ts": _ms(2026, 7, 8, 23, 59)}
    early = {"value": 100, "ts": _ms(2026, 7, 9, 0, 1)}
    now_9th = _ms(2026, 7, 9, 8)
    # "today" on the 9th sees only the early event
    assert aggregate_scalar([late, early], "sum", "today", now_9th, UTC) == 100


# ── full resolve_values on the water fixture ──────────────────────────────────

def test_resolve_water_values(water_def):
    now = _ms(2026, 7, 8, 15)
    events = [
        {"series": "water", "value": 250, "ts": _ms(2026, 7, 8, 9)},
        {"series": "water", "value": 500, "ts": _ms(2026, 7, 8, 14)},
    ]
    values = resolve_values(water_def, {}, events, now, UTC)
    assert values["goal_ml"] == 2000          # default
    assert values["today_ml"] == 750          # sum today
    assert values["remaining"] == 1250        # max(0, 2000 - 750)
    assert isinstance(values["week"], list) and len(values["week"]) == 7
    # whole numbers are ints, so a label renders "750" not "750.0"
    assert isinstance(values["today_ml"], int)
    assert values["week"][-1]["v"] == 750 and isinstance(values["week"][-1]["v"], int)


def test_resolve_respects_state_override(water_def):
    now = _ms(2026, 7, 8, 15)
    values = resolve_values(water_def, {"goal_ml": 3000}, [], now, UTC)
    assert values["goal_ml"] == 3000
    assert values["remaining"] == 3000        # nothing logged yet


def test_flowlet_preview_progress(water_def):
    from flowly.flowlets.queries import flowlet_preview
    values = {"today_ml": 750, "goal_ml": 2000}
    p = flowlet_preview(water_def, values)
    assert p is not None
    assert p["text"] == "750 / 2000 ml"        # the interpolated progress label
    assert abs(p["pct"] - 0.375) < 1e-9         # 750 / 2000


def test_flowlet_preview_stat_fallback():
    from flowly.flowlets.queries import flowlet_preview
    defn = {
        "catalog": 1, "name": "x",
        "state": {"n": {"type": "number", "default": 42}},
        "layout": [{"type": "stat", "value": "n", "label": "toplam"}],
    }
    p = flowlet_preview(defn, {"n": 42})
    assert p == {"text": "42 · toplam", "pct": None}


def test_computed_order_independent():
    # `b` depends on `a` but is declared first — fixpoint resolve handles it.
    defn = {
        "catalog": 1, "name": "x",
        "state": {"base": {"type": "number", "default": 10}},
        "computed": {
            "b": {"expr": "a + 1"},
            "a": {"expr": "base * 2"},
        },
        "layout": [{"type": "stat", "value": "b"}],
    }
    values = resolve_values(defn, {}, [], _ms(2026, 7, 8), UTC)
    assert values["a"] == 20
    assert values["b"] == 21
