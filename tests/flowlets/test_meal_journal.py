"""Photo/manual rows, civil dates and scheduled summaries share one truth."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from flowly.agent.tools.flowlet import FlowletTool
from flowly.flowlets.actions import apply_action
from flowly.flowlets.queries import resolve_values
from flowly.flowlets.schema import FlowletValidationError, validate_definition
from flowly.flowlets.sources import _coerce_field
from flowly.flowlets.templates import build_template
from flowly.flowlets.vision import FlowletCaptureError, apply_capture
from flowly.flowlets.watches import WatchEngine

TZ = ZoneInfo("Europe/Istanbul")
JPEG = b"\xff\xd8\xff\xe0jpeg-bytes"


def stamp(day, hour=20, minute=0):
    return int(datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp() * 1000)


async def test_photo_date_defaults_to_capture_day_across_midnight(store, monkeypatch):
    from flowly.flowlets import vision
    definition = build_template("meals", "tr")
    flowlet = store.create("Meals", definition)
    photo = definition["layout"][1]
    monkeypatch.setattr(vision, "_now_ms", lambda: stamp(8, 23, 59))

    async def runner(fl, prompt, path):
        assert "2026-10-08" in prompt
        monkeypatch.setattr(vision, "_now_ms", lambda: stamp(9, 0, 1))
        return '{"name":"Salata","kcal":420,"date":"2026-99-99"}'

    values = await apply_capture(store, flowlet, photo, JPEG, runner=runner, tz=TZ)
    assert values["meals"][0]["date"] == "2026-10-08"
    assert values["today_kcal"] == 0
    yesterday = resolve_values(definition, store.get_state(flowlet["id"]), [], stamp(8, 23, 59), TZ)
    assert yesterday["today_kcal"] == 420


async def test_explicit_photo_date_wins_and_unreadable_photo_stays_rejected(store):
    definition = build_template("meals")
    flowlet = store.create("Meals", definition)
    photo = definition["layout"][1]

    async def runner(*args):
        return '{"name":"Soup","kcal":180,"date":"2026-10-01"}'

    await apply_capture(store, flowlet, photo, JPEG, runner=runner, tz=TZ)
    assert store.get_state(flowlet["id"])["meals"][0]["date"] == "2026-10-01"

    async def unreadable(*args):
        return '{}'

    with pytest.raises(FlowletCaptureError, match="couldn't read"):
        await apply_capture(store, flowlet, photo, JPEG, runner=unreadable, tz=TZ)
    assert len(store.get_state(flowlet["id"])["meals"]) == 1


async def test_full_journal_rejects_before_model_or_attachment(store):
    definition = build_template("meals")
    definition["state"]["meals"]["max"] = 1
    flowlet = store.create("Meals", definition)
    store.set_state(flowlet["id"], "meals", [{"id": "one", "name": "Soup", "kcal": 180}])

    async def forbidden(*args):
        pytest.fail("A full journal must not spend a vision call")

    with pytest.raises(FlowletCaptureError, match="full"):
        await apply_capture(store, flowlet, definition["layout"][1], JPEG, runner=forbidden)
    assert not list(store._attach_dir(flowlet["id"]).glob("*.jpg"))


async def test_photo_manual_edit_and_daily_summary_agree(store, monkeypatch):
    from flowly.flowlets import actions, vision
    monkeypatch.setattr(vision, "_now_ms", lambda: stamp(8))
    monkeypatch.setattr(actions, "queries_now_ms", lambda: stamp(8))
    tool = FlowletTool(store)
    created = json.loads(await tool.execute("create", template_id="meals", lang="tr", watches=[{
        "id": "summary", "trigger": "schedule", "at": "21:00",
        "notify": {"title": "Özet", "body": "{today_kcal} kcal; {meal_count} öğün"},
    }]))
    fid = created["flowlet"]["id"]
    flowlet = store.get(fid)

    async def photo_runner(*args):
        return '{"name":"Soup","kcal":180}'

    await apply_capture(store, flowlet, flowlet["definition"]["layout"][1], JPEG,
                        runner=photo_runner, tz=TZ)
    for component, value in [("addMeal_name", "Salad"), ("addMeal_kcal", 420),
                             ("addMeal_date", "2026-10-08")]:
        await apply_action(store, fid, component, value=value, tz=TZ)
    values = (await apply_action(store, fid, "addMeal_submit", tz=TZ))["values"]
    assert values["today_kcal"] == 600 and values["meal_count"] == 2
    photo_id = values["meals"][0]["id"]
    values = (await apply_action(store, fid, "edit_kcal", value={"value": 200, "itemId": photo_id}, tz=TZ))["values"]
    assert values["today_kcal"] == 620
    sent = []

    async def notify(fid, title, body):
        sent.append(body)

    engine = WatchEngine(store, notify=notify, tz=TZ)
    await engine.evaluate_all(now_ms=stamp(8, 21))
    await engine.evaluate_all(now_ms=stamp(8, 22))
    assert sent == ["620 kcal; 2 öğün"]
    # A restart still respects the persisted daily firing, and tomorrow is empty.
    await WatchEngine(store, notify=notify, tz=TZ).evaluate_all(now_ms=stamp(9, 21))
    assert sent == ["620 kcal; 2 öğün", "0 kcal; 0 öğün"]


@pytest.mark.parametrize("value", ["2026-02-30", "2026-13-01", "2025-02-29"])
def test_impossible_calendar_dates_are_not_accepted(value):
    assert _coerce_field("date", value) is None


def test_vision_date_defaults_only_reference_date_fields():
    definition = build_template("meals")
    definition["layout"][1]["action"]["dateDefaults"] = ["kcal"]
    with pytest.raises(FlowletValidationError, match="dateDefaults"):
        validate_definition(definition)
