"""The goal card under a reply, and the owner's own changes to a goal."""

from __future__ import annotations

import json

import pytest

from flowly.agenda.store import get_agenda_store
from flowly.channels import feature_rpc
from flowly.objectives.cards import goal_cards_from_turn
from flowly.objectives.store import ObjectiveError, ObjectiveStore, get_objective_store
from flowly.profile import get_flowly_home
from flowly.session.manager import Session


def _call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _deferred(call_id: str, name: str, args: dict) -> dict:
    return _call(call_id, "tool_call", {"name": name, "arguments": args})


@pytest.fixture(autouse=True)
def fresh_home():
    home = get_flowly_home()
    for path in (home / "agenda" / "agenda.json", home / "objectives" / "objectives.json"):
        path.unlink(missing_ok=True)
    yield


def test_a_turn_that_starts_tracking_a_goal_gets_its_card(tmp_path):
    store = ObjectiveStore(tmp_path)
    goal = store.create(title="A month of physics", category="learning", plan=["Kinematics", "Newton"],
                        ends_at=4_000_000_000_000)
    turn = [
        {"role": "assistant", "content": "", "tool_calls": [
            _call("a", "objective", {"action": "create", "title": "A month of physics"}),
            _call("b", "web_search", {"query": "physics course"}),
        ]},
        {"role": "tool", "tool_call_id": "a", "name": "objective",
         "content": f"Tracking goal {goal['id']}: A month of physics. First check-in Tue 2026-10-14 20:00."},
        {"role": "tool", "tool_call_id": "b", "name": "web_search", "content": "results"},
        {"role": "assistant", "content": "I'll keep track of this month with you."},
    ]
    [card] = goal_cards_from_turn(turn, store)
    assert card == {
        "id": goal["id"], "event": "created", "title": "A month of physics", "category": "learning",
        "status": "active", "createdAt": goal["createdAt"], "endsAt": 4_000_000_000_000,
        "progress": {"current": None, "target": None, "unit": ""}, "steps": 2, "stepsDone": 0,
    }


def test_closing_shows_a_card_and_updates_or_errors_do_not(tmp_path):
    store = ObjectiveStore(tmp_path)
    goal = store.create(title="Save 500")
    store.set_status(goal["id"], "done")
    turn = [
        {"role": "assistant", "content": "", "tool_calls": [
            _deferred("u", "objective", {"action": "update", "id": goal["id"], "stage": "x"}),
            _call("bad", "objective", {"action": "create", "title": "Nope"}),
            _call("c", "objective", {"action": "close", "id": goal["id"], "outcome": "done"}),
        ]},
        {"role": "tool", "tool_call_id": "u", "name": "tool_call", "content": f"Updated {goal['id']}."},
        {"role": "tool", "tool_call_id": "bad", "name": "objective", "content": "Error: category must be one of …"},
        {"role": "tool", "tool_call_id": "c", "name": "objective", "content": f"Closed {goal['id']} as done."},
    ]
    cards = goal_cards_from_turn(turn, store)
    assert [(c["id"], c["event"], c["status"]) for c in cards] == [(goal["id"], "done", "done")]
    assert goal_cards_from_turn([{"role": "assistant", "content": "hi"}], store) == []


def test_the_card_rides_the_closing_reply_and_never_reaches_the_model():
    session = Session(key="desktop:chat-1")
    cards = [{"id": "obj_aaaaaaaaaaaa", "event": "created", "title": "Physics"}]
    session.extend_with_turn_messages(
        user_content="I want to study physics for a month",
        new_messages=[{"role": "assistant", "content": "Let's do it."}],
        final_content="Let's do it.",
        goal_cards=cards,
    )
    assert session.messages[-1]["goal_cards"] == cards
    assert all("goal_cards" not in m for m in session.get_history())


def test_the_owner_ticks_a_step_and_renames_and_the_agent_reads_it():
    goal = get_objective_store().create(title="Physics", plan=["Kinematics", "Newton"])

    async def rpc(params):
        result, _ = await feature_rpc.dispatch("objectives.update", params)
        return result

    import asyncio

    ticked = asyncio.run(rpc({"id": goal["id"], "action": "step", "step": 2, "done": True}))["objective"]
    assert ticked["plan"][1]["done"] is True
    assert {k: ticked["log"][-1][k] for k in ("kind", "event", "text")} == {
        "kind": "owner", "event": "step_done", "text": "Newton"}
    renamed = asyncio.run(rpc({"id": goal["id"], "action": "rename", "title": "Physics for a month"}))["objective"]
    assert renamed["title"] == "Physics for a month"
    assert {k: renamed["log"][-1][k] for k in ("kind", "event", "text")} == {
        "kind": "owner", "event": "renamed", "text": "Physics for a month"}
    with pytest.raises(feature_rpc.FeatureRpcError):
        asyncio.run(rpc({"id": goal["id"], "action": "step", "step": 9, "done": True}))
    with pytest.raises(feature_rpc.FeatureRpcError):
        asyncio.run(rpc({"id": goal["id"], "action": "rename", "title": ""}))
    assert get_agenda_store().pending() == []


def test_learning_is_a_category():
    store = ObjectiveStore(get_flowly_home())
    assert store.create(title="Learn Rust", category="learning")["category"] == "learning"
    with pytest.raises(ObjectiveError):
        store.set_step("obj_missing", 1, True)


@pytest.mark.asyncio
async def test_a_reopened_conversation_shows_the_same_card(tmp_path):
    from unittest.mock import AsyncMock

    from flowly.gateway.server import GatewayServer
    from flowly.session.manager import SessionManager

    sm = SessionManager(tmp_path / "workspace")
    session = sm.get_or_create("desktop:chat-1")
    cards = [{"id": "obj_aaaaaaaaaaaa", "event": "created", "title": "Physics", "endsAt": 4_000_000_000_000}]
    session.extend_with_turn_messages(
        user_content="a month of physics",
        new_messages=[{"role": "assistant", "content": "On it."}],
        final_content="On it.",
        goal_cards=cards,
    )
    sm.save(session)
    server = object.__new__(GatewayServer)
    server.sessions = sm
    server._ws_rpc_reply = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    await server._ws_rpc_chat_history(None, "h", {"sessionKey": "desktop:chat-1", "limit": 50})
    rows = server._ws_rpc_reply.await_args.args[2]["messages"]
    assert [row.get("goalCards") for row in rows] == [None, cards]


@pytest.mark.asyncio
async def test_a_message_the_agent_wrote_first_carries_its_goal_card_to_clients():
    from flowly.gateway.server import GatewayServer

    sent: list[dict] = []
    server = object.__new__(GatewayServer)
    server._ws_clients = {"desktop": object()}
    server._scoped_event = lambda payload: payload

    async def ws_send(ws, payload):
        sent.append(payload)

    server._ws_send = ws_send
    card = {"id": "obj_aaaaaaaaaaaa", "event": "checkin", "title": "Physics"}
    await server.push_session_message("desktop:chat-1", "How did it go?", metadata={"goalCards": [card]})
    [event] = sent
    assert event["data"]["message"]["goalCards"] == [card]
    assert event["data"]["proactive"] is True
