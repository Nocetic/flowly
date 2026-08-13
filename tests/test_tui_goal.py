"""TUI standing goal: client dispatch + goal.get params, and the composer
goal strip (visibility, revision guard, cleared rule)."""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App, ComposeResult

from flowly.tui.client import AgentAuthoredUser, GatewayClient, GoalUpdated
from flowly.tui.panes.composer import GoalPanel

HOSTILE = "[/dim] kötü [b]içerik [unknown]tag [i]x"


def _goal(status: str = "active", revision: int = 3) -> dict[str, Any]:
    return {
        "goalId": "goal_t1",
        "revision": revision,
        "goal": f"Ship the release {HOSTILE}",
        "status": status,
        "turnsUsed": 2,
        "maxTurns": 20,
        "pausedReason": "budget" if status == "paused" else None,
        "wait": None,
    }


def _make() -> GatewayClient:
    import asyncio

    client = GatewayClient.__new__(GatewayClient)
    client._inbox = asyncio.Queue()
    client._pending = {}
    return client


@pytest.mark.asyncio
async def test_dispatch_goal_updated_flattens_session_key():
    client = _make()
    await client._dispatch({
        "type": "event",
        "event": "goal.updated",
        "data": {"sessionKey": "tui:default", "goal": _goal()},
    })
    ev = client._inbox.get_nowait()
    assert isinstance(ev, GoalUpdated)
    assert ev.goal["goalId"] == "goal_t1"
    assert ev.goal["sessionKey"] == "tui:default"


@pytest.mark.asyncio
async def test_dispatch_goal_updated_drops_unparseable():
    client = _make()
    await client._dispatch({
        "type": "event",
        "event": "goal.updated",
        "data": {"sessionKey": "tui:default", "goal": {"status": "active"}},
    })
    assert client._inbox.empty()


class _Host(App):
    def compose(self) -> ComposeResult:
        yield GoalPanel(id="composer-goal-panel")


@pytest.mark.asyncio
async def test_goal_panel_renders_hides_and_survives_hostile_markup():
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(GoalPanel)
        panel.set_goal(_goal())
        await pilot.pause()
        assert panel.has_class("has-goal")
        panel.set_goal(None)
        await pilot.pause()
        assert not panel.has_class("has-goal")


@pytest.mark.asyncio
async def test_goal_panel_cleared_snapshot_hides():
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(GoalPanel)
        panel.set_goal(_goal())
        await pilot.pause()
        panel.set_goal(_goal(status="cleared", revision=9))
        await pilot.pause()
        assert not panel.has_class("has-goal")


@pytest.mark.asyncio
async def test_goal_panel_revision_guard_drops_stale():
    app = _Host()
    async with app.run_test() as pilot:
        panel = app.query_one(GoalPanel)
        panel.set_goal(_goal(revision=5))
        await pilot.pause()
        held = panel._revision
        stale = _goal(revision=4)
        stale["goal"] = "STALE"
        panel.set_goal(stale)
        await pilot.pause()
        assert panel._revision == held


@pytest.mark.asyncio
async def test_dispatch_agent_authored_user_turn():
    """A goal's prompt reaches the transcript as the user row it is."""
    client = _make()
    await client._dispatch({
        "type": "event",
        "event": "chat",
        "data": {
            "state": "user",
            "runId": "run-1",
            "sessionKey": "tui:default",
            "goalRun": True,
            "message": {"role": "user", "content": [{"type": "text", "text": "keep going"}]},
        },
    })
    ev = client._inbox.get_nowait()
    assert isinstance(ev, AgentAuthoredUser)
    assert (ev.run_id, ev.session_key, ev.text) == ("run-1", "tui:default", "keep going")


@pytest.mark.asyncio
async def test_agent_authored_user_turn_without_text_is_dropped():
    client = _make()
    await client._dispatch({
        "type": "event",
        "event": "chat",
        "data": {"state": "user", "runId": "run-1", "message": {"content": []}},
    })
    assert client._inbox.empty()
