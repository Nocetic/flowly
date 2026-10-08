"""An evening summary reads like a summary: real numbers in the user's
format, a nudge instead of "0 kcal" on an empty day, and ready-made copy the
agent attaches by name instead of writing it."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from flowly.agent.tools.flowlet import FlowletTool
from flowly.flowlets.actions import apply_action
from flowly.flowlets.schema import FlowletValidationError, validate_definition
from flowly.flowlets.templates import list_templates
from flowly.flowlets.watches import WatchEngine

TZ = ZoneInfo("Europe/Istanbul")


def stamp(day, hour=21, minute=0):
    return int(datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp() * 1000)


async def _engine_run(store, now):
    sent = []

    async def notify(fid, title, body):
        sent.append((title, body))

    await WatchEngine(store, notify=notify, tz=TZ).evaluate_all(now_ms=now)
    return sent


async def test_preset_summary_by_name_with_localized_numbers(store, monkeypatch):
    from flowly.flowlets import actions
    monkeypatch.setattr(actions, "queries_now_ms", lambda: stamp(8, 12))
    tool = FlowletTool(store)
    created = json.loads(await tool.execute(
        "create", template_id="meals", lang="tr", pinned=True,
        watches=[{"preset": "daily_summary", "at": "21:30"}]))
    fid = created["flowlet"]["id"]
    watch = store.get(fid)["definition"]["watches"][0]
    assert watch["at"] == "21:30" and watch["when"] == "meal_count > 0"

    for cid, value in [("addMeal_name", "Mantı"), ("addMeal_kcal", 1650)]:
        await apply_action(store, fid, cid, value=value, tz=TZ)
    await apply_action(store, fid, "addMeal_submit", tz=TZ)

    assert await _engine_run(store, stamp(8, 21, 0)) == []          # not yet 21:30
    sent = await _engine_run(store, stamp(8, 21, 31))
    assert sent == [("Günün özeti", "1 öğün, 1.650 / 2.200 kcal. Hedefe 550 kcal kaldı.")]
    assert await _engine_run(store, stamp(8, 22, 0)) == []          # once per day


async def test_empty_day_sends_the_nudge_not_a_zero_summary(store):
    tool = FlowletTool(store)
    await tool.execute("create", template_id="meals", lang="en", watches=["daily_summary"])
    sent = await _engine_run(store, stamp(8, 21, 5))
    assert sent == [("Today's meals",
                     "Nothing logged today. One photo is enough to add a meal.")]


async def test_gate_without_otherwise_skips_quietly(store):
    defn = {"catalog": 3, "name": "Steps", "state": {"n": {"type": "number", "default": 0}},
            "layout": [{"type": "stat", "value": "n"}],
            "watches": [{"id": "s", "trigger": "schedule", "at": "20:00", "when": "n > 0",
                         "notify": {"title": "Steps", "body": "{n} today"}}]}
    store.create("Steps", defn)
    assert await _engine_run(store, stamp(8, 20, 1)) == []
    assert await _engine_run(store, stamp(8, 20, 2)) == []  # slot spent, no per-minute retry


def test_unknown_preset_lists_the_available_ones():
    out, err = FlowletTool._resolve_watches(["nightly"], {"daily_summary": {}})
    assert out is None and "daily_summary" in err


def test_otherwise_requires_a_gate_and_gate_keys_must_exist():
    base = {"catalog": 3, "name": "X", "state": {"n": {"type": "number"}},
            "layout": [{"type": "stat", "value": "n"}]}
    with pytest.raises(FlowletValidationError, match="needs a `when`"):
        validate_definition({**base, "watches": [{"id": "a", "trigger": "schedule", "at": "20:00",
                                                  "notify": {"title": "t"},
                                                  "otherwise": {"title": "u"}}]})
    with pytest.raises(FlowletValidationError, match="unknown key 'm'"):
        validate_definition({**base, "watches": [{"id": "a", "trigger": "schedule", "at": "20:00",
                                                  "when": "m > 0", "notify": {"title": "t"}}]})
    with pytest.raises(FlowletValidationError, match="locale"):
        validate_definition({**base, "locale": "de"})


def test_template_cards_name_their_reminders():
    meals = next(t for t in list_templates("tr") if t["id"] == "meals")
    assert meals["reminders"] == [{"id": "daily_summary",
                                   "when": "daily at 21:00 (only if something was logged, else a nudge)"}]
