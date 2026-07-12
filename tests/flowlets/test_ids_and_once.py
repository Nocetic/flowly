"""Missing-id auto-assignment + the `once` action latch.

Two authoring-failure classes killed at the system level:
* a forgotten `id` used to REJECT a button at create and let an id-less chart
  silently render "No data yet" (its series is keyed by id in `values`);
* a "complete the day" button could fire 8 times in 14 seconds — `visibleWhen`
  can't stop a re-fire once the checkboxes reset. `once` latches server-side.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from flowly.flowlets import actions as actions_mod
from flowly.flowlets.actions import apply_action
from flowly.flowlets.normalize import assign_missing_ids
from flowly.flowlets.queries import resolve_values
from flowly.flowlets.schema import FlowletValidationError, validate_definition

UTC = timezone.utc


def _ms(y, mo, d, h=12):
    return int(datetime(y, mo, d, h, tzinfo=UTC).timestamp() * 1000)


# ── assign_missing_ids ────────────────────────────────────────────────────────

def _habit_defn(with_ids: bool = False) -> dict:
    # Mirrors the user's real "Günlük Alışkanlıklar" (id-less chart included).
    return {
        "catalog": 2, "name": "Alışkanlıklar",
        "state": {"read_done": {"type": "bool", "default": False},
                  "code_done": {"type": "bool", "default": False}},
        "series": {"days": {}},
        "computed": {"weekly_total": {"series": "days", "agg": "sum", "window": "7d"}},
        "layout": [
            {"type": "checklist", **({"id": "habits"} if with_ids else {}),
             "items": [{"key": "read_done", "label": "kitap"},
                       {"key": "code_done", "label": "kod"}]},
            {"type": "button", "text": "Günü Tamamla",
             **({"id": "complete_day"} if with_ids else {}),
             "action": {"op": "batch", "once": "day", "ops": [
                 {"op": "log", "series": "days", "value": 1},
                 {"op": "reset", "key": "read_done"},
                 {"op": "reset", "key": "code_done"}]}},
            {"type": "stat", "value": "weekly_total"},
            {"type": "chart", "kind": "bar",
             "data": {"series": "days", "agg": "sum", "bucket": "day", "window": "7d"}},
        ],
    }


def test_missing_ids_are_assigned_deterministically():
    out = assign_missing_ids(_habit_defn())
    types_ids = [(n["type"], n.get("id")) for n in out["layout"]]
    assert types_ids[0] == ("checklist", "checklist_1")
    assert types_ids[1] == ("button", "button_1")
    assert types_ids[2] == ("stat", None)          # a stat needs no id
    assert types_ids[3] == ("chart", "chart_1")
    # deterministic + idempotent
    again = assign_missing_ids(_habit_defn())
    assert again == out
    assert assign_missing_ids(out) is out          # nothing missing → no copy


def test_existing_ids_and_scalar_keys_are_never_clobbered():
    d = _habit_defn(with_ids=True)
    d["state"]["chart_1"] = {"type": "number", "default": 0}   # collision bait
    out = assign_missing_ids(d)
    assert out["layout"][1]["id"] == "complete_day"            # author id kept
    assert out["layout"][3]["id"] == "chart_2"                 # skipped chart_1


async def test_id_less_definition_now_validates_and_creates(store):
    # The reported failure: {"error": "... button carries an action, so it
    # needs a unique `id`"}. The tool now assigns instead of rejecting.
    from flowly.agent.tools.flowlet import FlowletTool

    tool = FlowletTool(store)
    res = json.loads(await tool.execute("create", definition=_habit_defn()))
    assert "error" not in res
    stored = store.get(res["flowlet"]["id"])["definition"]
    assert stored["layout"][1]["id"] == "button_1"             # persisted
    assert stored["layout"][3]["id"] == "chart_1"


def test_id_less_chart_resolves_its_series():
    # The "No data yet" bug: an id-less chart was SKIPPED by resolve_values.
    d = _habit_defn()
    events = [{"series": "days", "value": 1, "ts": _ms(2026, 7, 12, 9), "meta": None}]
    vals = resolve_values(d, {}, events, _ms(2026, 7, 12, 23), UTC)
    assert "chart_1" in vals
    assert {b["t"]: b["v"] for b in vals["chart_1"]}["2026-07-12"] == 1


def test_expanded_assigned_definition_validates():
    validate_definition(assign_missing_ids(_habit_defn()))


# ── the once latch ────────────────────────────────────────────────────────────

@pytest.fixture
def frozen_now(monkeypatch):
    holder = {"ms": _ms(2026, 7, 12, 9)}
    monkeypatch.setattr(actions_mod, "queries_now_ms", lambda: holder["ms"])
    return holder


async def test_once_day_latches_repeat_taps(store, frozen_now):
    f = store.create("Alışkanlıklar", assign_missing_ids(_habit_defn()))
    fid = f["id"]
    await apply_action(store, fid, "button_1", None, tz=UTC)
    assert len(store.get_events(fid)) == 1
    # The user's exact bug: tap again (and again) the same day → NO new day.
    await apply_action(store, fid, "button_1", None, tz=UTC)
    await apply_action(store, fid, "button_1", None, tz=UTC)
    assert len(store.get_events(fid)) == 1                     # still one
    # ...and the repeat is a silent no-op that still returns fresh values.
    res = await apply_action(store, fid, "button_1", None, tz=UTC)
    assert res["values"]["weekly_total"] == 1


async def test_once_day_fires_again_the_next_day(store, frozen_now):
    f = store.create("Alışkanlıklar", assign_missing_ids(_habit_defn()))
    fid = f["id"]
    await apply_action(store, fid, "button_1", None, tz=UTC)
    frozen_now["ms"] = _ms(2026, 7, 13, 9)                     # next local day
    await apply_action(store, fid, "button_1", None, tz=UTC)
    assert len(store.get_events(fid)) == 2


async def test_once_true_latches_forever(store, frozen_now):
    d = {
        "catalog": 2, "name": "x",
        "state": {"claimed": {"type": "bool", "default": False}},
        "layout": [{"id": "claim", "type": "button", "text": "Al",
                    "action": {"op": "toggle", "key": "claimed", "once": True}}],
    }
    f = store.create("x", d)
    await apply_action(store, f["id"], "claim", None, tz=UTC)
    frozen_now["ms"] = _ms(2027, 1, 1)                         # a year later
    res = await apply_action(store, f["id"], "claim", None, tz=UTC)
    assert res["values"]["claimed"] is True                    # not re-toggled


def test_once_guard_state_never_leaks_to_values(store):
    f = store.create("Alışkanlıklar", assign_missing_ids(_habit_defn()))
    store.set_state(f["id"], "__once__button_1", "2026-07-12")
    vals = resolve_values(f["definition"], store.get_state(f["id"]), [],
                          _ms(2026, 7, 12, 23), UTC)
    assert not any(k.startswith("__once__") for k in vals)


def test_schema_rejects_a_bad_once():
    d = _habit_defn(with_ids=True)
    d["layout"][1]["action"]["once"] = "month"
    with pytest.raises(FlowletValidationError, match="`once` must be"):
        validate_definition(d)


def test_lint_flags_a_complete_day_batch_without_once():
    from flowly.flowlets.lint import lint_definition
    d = _habit_defn(with_ids=True)
    del d["layout"][1]["action"]["once"]
    assert "L13" in {f["id"] for f in lint_definition(d)}
    # ...and with the latch present it's clean.
    assert "L13" not in {f["id"] for f in lint_definition(_habit_defn(with_ids=True))}
