"""Chip-driven goal controls: pause/resume RPC path and the rule that goal
status is chip state — never a chat bubble."""

from __future__ import annotations

from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.channels.web import WebChannel
from flowly.gateway.server import GatewayServer
from flowly.goals.models import GoalState


class _Manager:
    def __init__(self) -> None:
        self.state = GoalState(session_key="web:c1", goal="ship it")
        self.calls: list[str] = []

    def pause(self, session_key: str, reason: str = "") -> GoalState:
        self.calls.append(f"pause:{reason}")
        self.state.status = type(self.state.status)("paused")
        return self.state

    def resume(self, session_key: str) -> GoalState:
        self.calls.append("resume")
        self.state.status = type(self.state.status)("active")
        return self.state


class _Runtime:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.woken: list[str] = []

    def cancel_session(self, session_key: str) -> None:
        self.cancelled.append(session_key)

    def wake(self, session_key: str, delivery: Any = None) -> bool:
        self.woken.append(session_key)
        return True


class _Bus:
    def __init__(self) -> None:
        self.outbound: list[Any] = []

    async def publish_outbound(self, msg: Any) -> None:
        self.outbound.append(msg)


def _loop() -> tuple[AgentLoop, _Manager, _Runtime, _Bus]:
    loop = AgentLoop.__new__(AgentLoop)
    manager = _Manager()
    runtime = _Runtime()
    bus = _Bus()
    loop.goal_manager = manager
    loop.goal_runtime = runtime
    loop.bus = bus
    loop._gateway_server = None
    return loop, manager, runtime, bus


@pytest.mark.asyncio
async def test_goal_control_pause_cancels_queued_work_and_pushes_snapshot():
    loop, manager, runtime, bus = _loop()

    result = await loop.goal_control("web:c1", "pause")

    assert manager.calls == ["pause:paused by user"]
    assert runtime.cancelled == ["web:c1"]
    assert result["goal"]["status"] == "paused"
    # Snapshot reaches the channel surface as chip state, not chat content.
    assert len(bus.outbound) == 1
    out = bus.outbound[0]
    assert out.content == ""
    assert out.metadata["goalStatus"] is True
    assert out.metadata["goal"]["goalId"] == result["goal"]["goalId"]


@pytest.mark.asyncio
async def test_goal_control_resume_wakes_the_runtime():
    loop, manager, runtime, _bus = _loop()

    result = await loop.goal_control("web:c1", "resume")

    assert manager.calls == ["resume"]
    assert runtime.woken == ["web:c1"]
    assert result["goal"]["status"] == "active"


@pytest.mark.asyncio
async def test_goal_control_rejects_unknown_action():
    loop, _manager, _runtime, _bus = _loop()
    with pytest.raises(ValueError):
        await loop.goal_control("web:c1", "explode")


@pytest.mark.asyncio
async def test_gateway_goal_status_push_is_event_only():
    server = GatewayServer.__new__(GatewayServer)
    broadcasts: list[tuple[str, dict]] = []

    async def _record(session_key: str, goal: dict, **_: Any) -> None:
        broadcasts.append((session_key, goal))

    server.broadcast_goal_updated = _record  # type: ignore[method-assign]

    await server.push_session_message(
        "web:c1",
        "⏸ Goal paused — waiting for user input: long explanation…",
        metadata={"goalStatus": True, "goal": {"goalId": "g1", "revision": 2}},
    )

    # Snapshot broadcast happened; no chat event was attempted (any socket
    # access would explode on this bare instance — reaching here proves the
    # early return).
    assert broadcasts == [("web:c1", {"goalId": "g1", "revision": 2})]


@pytest.mark.asyncio
async def test_web_channel_goal_status_is_event_only():
    channel = WebChannel.__new__(WebChannel)
    sent: list[str] = []
    emitted: list[tuple[str, dict]] = []

    async def _send(payload: str) -> None:
        sent.append(payload)

    async def _emit(name: str, data: dict) -> None:
        emitted.append((name, data))

    channel._send_or_queue = _send  # type: ignore[method-assign]
    channel._emit_local_event = _emit  # type: ignore[method-assign]
    channel._session_key_for_relay_id = lambda sid: f"web:{sid}"  # type: ignore[method-assign]

    from flowly.bus.events import OutboundMessage

    await channel.send(OutboundMessage(
        channel="web",
        chat_id="c1",
        content="↻ Continuing toward goal (2/20): still working…",
        metadata={"goalStatus": True, "goal": {"goalId": "g1", "revision": 3}},
    ))

    assert len(sent) == 1
    import json as _json

    event = _json.loads(sent[0])
    assert event["event"] == "goal.updated"
    assert event["data"]["goal"]["goalId"] == "g1"
