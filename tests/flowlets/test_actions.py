"""Action interpreter: each op, validation, security, batch, agent, checklist."""

from __future__ import annotations

from datetime import timezone

import pytest

from flowly.flowlets.actions import FlowletActionError, apply_action

from .conftest import load_fixture

UTC = timezone.utc


async def test_log_increments_today(store, water_def):
    f = store.create("Su", water_def)
    res = await apply_action(store, f["id"], "drink250", tz=UTC)
    assert res["values"]["today_ml"] == 250
    res = await apply_action(store, f["id"], "drink250", tz=UTC)
    assert res["values"]["today_ml"] == 500
    assert res["values"]["remaining"] == 1500


async def test_remove_last_undo(store, water_def):
    f = store.create("Su", water_def)
    await apply_action(store, f["id"], "drink250", tz=UTC)
    await apply_action(store, f["id"], "drink250", tz=UTC)
    res = await apply_action(store, f["id"], "undo", tz=UTC)
    assert res["values"]["today_ml"] == 250


async def test_slider_set_clamps_to_component_bounds(store, water_def):
    f = store.create("Su", water_def)
    # slider min=1000 max=4000; a value above max clamps to max
    res = await apply_action(store, f["id"], "goalSlider", value=99999, tz=UTC)
    assert res["values"]["goal_ml"] == 4000
    res = await apply_action(store, f["id"], "goalSlider", value=1500, tz=UTC)
    assert res["values"]["goal_ml"] == 1500


async def test_set_requires_value(store, water_def):
    f = store.create("Su", water_def)
    with pytest.raises(FlowletActionError):
        await apply_action(store, f["id"], "goalSlider", value=None, tz=UTC)


async def test_fixed_value_ignores_client_value(store, water_def):
    # drink250 has a fixed value:250; a client-passed value must not override it
    f = store.create("Su", water_def)
    res = await apply_action(store, f["id"], "drink250", value=99999, tz=UTC)
    assert res["values"]["today_ml"] == 250


async def test_unknown_component_rejected(store, water_def):
    f = store.create("Su", water_def)
    with pytest.raises(FlowletActionError) as ei:
        await apply_action(store, f["id"], "ghost", tz=UTC)
    assert ei.value.code == "NOT_FOUND"


async def test_unknown_flowlet_rejected(store):
    with pytest.raises(FlowletActionError) as ei:
        await apply_action(store, "flt_missing", "x", tz=UTC)
    assert ei.value.code == "NOT_FOUND"


async def test_stepper_increment(store):
    f_def = load_fixture("pomodoro")
    f = store.create("Pomodoro", f_def)
    res = await apply_action(store, f["id"], "goalStep", tz=UTC)
    assert res["values"]["sessions_goal"] == 9
    # increment clamps at the state max (20)
    for _ in range(20):
        res = await apply_action(store, f["id"], "goalStep", tz=UTC)
    assert res["values"]["sessions_goal"] == 20


async def test_checklist_toggle(store):
    f_def = load_fixture("habits")
    f = store.create("Alışkanlıklar", f_def)
    res = await apply_action(store, f["id"], "habits", value="water", tz=UTC)
    assert res["values"]["water"] is True
    res = await apply_action(store, f["id"], "habits", value="water", tz=UTC)
    assert res["values"]["water"] is False


async def test_checklist_rejects_foreign_key(store):
    f_def = load_fixture("habits")
    f = store.create("Alışkanlıklar", f_def)
    with pytest.raises(FlowletActionError):
        await apply_action(store, f["id"], "habits", value="ghost", tz=UTC)


async def test_rating_log(store):
    f_def = load_fixture("mood")
    f = store.create("Ruh", f_def)
    res = await apply_action(store, f["id"], "rate", value=4, tz=UTC)
    assert res["values"]["avg7"] == 4
    res = await apply_action(store, f["id"], "rate", value=2, tz=UTC)
    assert res["values"]["avg7"] == 3   # (4 + 2) / 2


async def test_input_set_and_truncate(store):
    f_def = load_fixture("mood")
    f = store.create("Ruh", f_def)
    res = await apply_action(store, f["id"], "note", value="x" * 500, tz=UTC)
    assert len(res["values"]["note"]) == 200   # maxLength enforced


async def test_agent_op_calls_runner(store, water_def):
    f = store.create("Su", water_def)
    called = {}

    async def runner(flowlet, message):
        called["id"] = flowlet["id"]
        called["msg"] = message

    res = await apply_action(store, f["id"], "coach", tz=UTC, agent_runner=runner)
    assert called["id"] == f["id"]
    assert "su" in called["msg"].lower()
    # values still returned unchanged
    assert "today_ml" in res["values"]


async def test_agent_op_unavailable_without_runner(store, water_def):
    f = store.create("Su", water_def)
    with pytest.raises(FlowletActionError) as ei:
        await apply_action(store, f["id"], "coach", tz=UTC, agent_runner=None)
    assert ei.value.code == "UNAVAILABLE"
