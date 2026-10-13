"""Owner-facing RPCs for tracked goals and the agenda (flowly/agenda/rpc.py)."""

from __future__ import annotations

import pytest

from flowly.agenda.store import get_agenda_store
from flowly.channels import feature_rpc
from flowly.objectives.store import get_objective_store
from flowly.profile import get_flowly_home
from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc

CHAT = "desktop:chat-1"
METHODS = ("objectives.list", "objectives.update", "agenda.list", "agenda.settings.get", "agenda.settings.set")


@pytest.fixture(autouse=True)
def fresh_home():
    home = get_flowly_home()
    for path in (home / "agenda" / "agenda.json", home / "objectives" / "objectives.json"):
        path.unlink(missing_ok=True)
    yield


async def _rpc(method: str, params: dict | None = None) -> dict:
    result, restart = await feature_rpc.dispatch(method, params or {})
    assert restart is False
    return result


def _goal_with_check_in() -> dict:
    goal = get_objective_store().create(title="Work out every day", category="health", session_key=CHAT)
    get_agenda_store().schedule(kind="objective", due_at=4_000_000_000_000, note="Day 1",
                                session_key=CHAT, objective_id=goal["id"])
    return goal


@pytest.mark.asyncio
async def test_goals_are_listed_with_their_next_check_in():
    goal = _goal_with_check_in()
    listed = await _rpc("objectives.list")
    assert [g["id"] for g in listed["objectives"]] == [goal["id"]]
    assert listed["objectives"][0]["nextCheckAt"] == 4_000_000_000_000
    assert listed["settings"]["level"] == "normal"


@pytest.mark.asyncio
@pytest.mark.parametrize("action, status", [("pause", "paused"), ("done", "done"), ("drop", "dropped")])
async def test_the_owner_pausing_or_ending_a_goal_cancels_its_check_in(action, status):
    goal = _goal_with_check_in()
    result = await _rpc("objectives.update", {"id": goal["id"], "action": action})
    assert result["objective"]["status"] == status and result["objective"]["nextCheckAt"] is None
    assert get_agenda_store().pending() == []


@pytest.mark.asyncio
async def test_a_resumed_goal_waits_for_the_agent_to_plan_its_next_check_in():
    goal = _goal_with_check_in()
    await _rpc("objectives.update", {"id": goal["id"], "action": "pause"})
    resumed = await _rpc("objectives.update", {"id": goal["id"], "action": "resume"})
    assert resumed["objective"]["status"] == "active" and resumed["objective"]["nextCheckAt"] is None


@pytest.mark.asyncio
async def test_a_deleted_goal_is_gone_with_its_check_in():
    goal = _goal_with_check_in()
    assert (await _rpc("objectives.update", {"id": goal["id"], "action": "delete"}))["deleted"] == goal["id"]
    assert get_objective_store().list() == [] and get_agenda_store().pending() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("params, code", [
    ({"id": "obj_missing", "action": "pause"}, "NOT_FOUND"),
    ({"id": "obj_missing", "action": "delete"}, "NOT_FOUND"),
    ({"action": "pause"}, "INVALID"),
    ({"id": "obj_x", "action": "explode"}, "INVALID"),
])
async def test_bad_updates_are_structured_errors(params, code):
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await _rpc("objectives.update", params)
    assert error.value.code == code


@pytest.mark.asyncio
async def test_the_owner_sets_how_often_the_agent_writes_first():
    got = await _rpc("agenda.settings.get")
    assert got["levels"] == ["off", "low", "normal", "high"] and got["dailyCaps"]["normal"] == 2
    saved = await _rpc("agenda.settings.set", {"level": "low", "quietStart": "23:00"})
    assert saved["settings"]["level"] == "low" and saved["settings"]["quietStart"] == "23:00"
    for bad in ({"level": "loud"}, {"volume": 3}, {}):
        with pytest.raises(feature_rpc.FeatureRpcError):
            await _rpc("agenda.settings.set", bad)


@pytest.mark.asyncio
async def test_the_agenda_shows_what_is_planned_and_what_came_of_it():
    store = get_agenda_store()
    planned = store.schedule(kind="follow_up", due_at=4_000_000_000_000, note="Laptop prices", session_key=CHAT)
    gone = store.schedule(kind="follow_up", due_at=4_000_000_000_001, note="Old", session_key=CHAT)
    store.cancel(gone["id"], reason="agent")
    listed = await _rpc("agenda.list")
    assert [i["id"] for i in listed["pending"]] == [planned["id"]]
    assert listed["history"][0]["id"] == gone["id"] and listed["history"][0]["outcome"] == "cancelled"
    assert "state" not in listed["pending"][0] and "attempts" not in listed["pending"][0]


def test_named_agents_serve_them_through_the_profile_host():
    for method in METHODS:
        assert method in feature_rpc.FEATURE_METHODS
        assert validate_profile_rpc(method, {})[0] == method
    with pytest.raises(ProfileHostError):
        validate_profile_rpc("agenda.delete_everything", {})
