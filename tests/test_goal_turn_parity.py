"""A goal turn is an ordinary turn.

The defect these tests exist for: standing-goal turns once had their own
delivery path, so they drifted from the chat path a user's message takes —
missing local stream events, bespoke run ids, a hidden prompt. Each test here
pins one half of the parity rather than the goal feature itself.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.agent.loop import AgentLoop, _AgentGoalDelivery
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _relay_channel() -> tuple[WebChannel, list[dict[str, Any]], list[str]]:
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    payloads: list[dict[str, Any]] = []
    local: list[str] = []

    async def capture(payload: str) -> None:
        payloads.append(json.loads(payload))

    async def capture_local(name: str, _data: dict[str, Any]) -> None:
        local.append(name)

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel._emit_local_event = capture_local  # type: ignore[method-assign]
    channel._session_key_to_relay_id["web:chat"] = "relay-1"
    return channel, payloads, local


@pytest.mark.asyncio
async def test_relay_user_and_goal_turns_stream_through_one_implementation():
    """Both turn kinds must emit the SAME event set for a delta."""
    channel, payloads, local = _relay_channel()

    # The callback chat.send builds…
    user_cb = channel._make_stream_callback("relay-1", "web:chat", "run-user")
    await user_cb("hi")
    await asyncio.sleep(0)
    user_wire = [p["data"] for p in payloads]
    user_local = list(local)

    payloads.clear()
    local.clear()

    # …and the one an autonomous goal turn gets are the same code.
    goal_cb = channel._make_stream_callback("relay-1", "web:chat", "run-goal")
    await goal_cb("hi")
    await asyncio.sleep(0)
    goal_wire = [p["data"] for p in payloads]

    assert user_local == local == ["chat", "agent"]
    assert [{**d, "runId": "x"} for d in user_wire] == [
        {**d, "runId": "x"} for d in goal_wire
    ]


@pytest.mark.asyncio
async def test_goal_run_id_is_an_ordinary_run_id():
    """A `goal-` prefix made clients treat the run as a foreign lifecycle."""
    channel, payloads, _local = _relay_channel()
    published: list[Any] = []

    async def _publish(msg: Any) -> None:
        published.append(msg)

    channel.bus.publish_inbound = _publish  # type: ignore[assignment]

    await channel.run_autonomous_turn("web:chat", {"goal_run": True})
    await asyncio.sleep(0)

    run_id = published[0].metadata["run_id"]
    assert not run_id.startswith("goal-")
    assert len(run_id) >= 32  # a plain uuid4, like chat.send's idempotency key


@pytest.mark.asyncio
async def test_the_prompt_is_announced_only_after_the_agents_guards_pass():
    """A user row must never appear for a turn the goal then refuses to run."""
    channel, payloads, _local = _relay_channel()
    published: list[Any] = []

    async def _publish(msg: Any) -> None:
        published.append(msg)

    channel.bus.publish_inbound = _publish  # type: ignore[assignment]

    await channel.run_autonomous_turn("web:chat", {"goal_run": True})
    await asyncio.sleep(0)

    # Submitting alone shows nothing…
    assert [p["data"].get("state") for p in payloads] == []
    # …the agent announces from inside its lock, once the goal still holds.
    await published[0].metadata["on_user_message"]("keep going")
    assert [p["data"]["state"] for p in payloads] == ["user"]


def test_goal_prompts_are_persisted_visibly():
    """The transcript must show what caused each autonomous reply."""
    import inspect

    source = inspect.getsource(inspect.getmodule(AgentLoop))
    assert "user_display_hidden=False" in source
    assert "user_display_hidden=bool(msg.metadata.get(_GOAL_CONTINUATION_ID))" not in source


@pytest.mark.asyncio
async def test_a_goal_turn_is_judged_once_through_the_normal_delivery_hook():
    """Submitting must not also evaluate: the delivered-turn hook does that."""
    loop = object.__new__(AgentLoop)
    loop._process_message = AsyncMock()
    loop._goal_turn_from_outbound = Mock()
    submitted: list[str] = []

    async def submitter(session_key: str, _metadata: dict) -> bool:
        submitted.append(session_key)
        return True

    loop.register_goal_turn_submitter("web", submitter)
    delivery = _AgentGoalDelivery(
        loop, session_key="web:chat", channel="web", chat_id="relay-1", direct=False,
    )

    turn = await delivery.run_continuation(
        session_key="web:chat", goal_id="g1", user_epoch=1, kickoff=False,
    )

    assert turn is None, "returning a turn here would judge it a second time"
    assert submitted == ["web:chat"]
    loop._goal_turn_from_outbound.assert_not_called()


@pytest.mark.asyncio
async def test_direct_turn_carries_the_same_run_identity_end_to_end():
    """User row, deltas and terminal share one id — clients key state on it."""
    captured: dict[str, Any] = {}

    async def on_chat_message(session_key, message, run_id, stream_cb, *args):
        captured["run_id"] = run_id
        await args[-1]["on_user_message"]("keep going")
        await stream_cb("out")
        return "done", {}

    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=on_chat_message)
    ws = _FakeWS()
    server._session_ws["desktop:chat"] = ws

    await server.run_autonomous_turn("desktop:chat", {"goal_run": True})

    ids = {e["data"]["runId"] for e in ws.sent if e["data"].get("runId")}
    assert ids == {captured["run_id"]}


# ── /goal <text> is the turn ────────────────────────────────────────────────
#
# The defect: setting a goal replied with an acknowledgement and only THEN
# started a second turn, after a blocking contract-drafting call. Typing
# `/goal X` must feel exactly like typing `X`.


@pytest.mark.asyncio
async def test_setting_a_goal_runs_this_turn_instead_of_replying(tmp_path):
    from flowly.goals.commands import GoalCommandHandler
    from flowly.goals.manager import GoalManager
    from flowly.goals.store import GoalStore

    class _Judge:
        model = None

        async def draft_contract(self, objective: str):
            raise AssertionError("drafting must never block the first turn")

    class _Runtime:
        def cancel_session(self, _key: str) -> None:
            pass

    manager = GoalManager(GoalStore(tmp_path), judge=_Judge())
    handler = GoalCommandHandler(manager, _Runtime())

    result = await handler.goal("web:chat", "inspect the desktop", conversation_epoch=0)

    # The command hands the caller a prompt to run, not a reply to send…
    assert result.start_turn == "inspect the desktop"
    # …and never asks for a separate kickoff turn afterwards.
    assert result.kickoff_goal_id is None
    # Drafting was scheduled, not awaited.
    assert handler._settling
    for task in list(handler._settling):
        task.cancel()


@pytest.mark.asyncio
async def test_a_superseded_goal_never_receives_a_late_contract(tmp_path):
    from flowly.goals.manager import GoalManager
    from flowly.goals.models import GoalContract
    from flowly.goals.store import GoalStore

    manager = GoalManager(GoalStore(tmp_path), judge=None)
    first = manager.set("web:chat", "first objective", conversation_epoch=0)
    second = manager.set("web:chat", "second objective", conversation_epoch=0)
    assert first.goal_id != second.goal_id

    late = manager.attach_contract(
        "web:chat", first.goal_id, GoalContract(outcome="stale"),
    )

    assert late is None
    assert manager.get("web:chat").contract.is_empty


@pytest.mark.asyncio
async def test_a_hand_written_contract_wins_over_the_background_draft(tmp_path):
    from flowly.goals.manager import GoalManager
    from flowly.goals.models import GoalContract
    from flowly.goals.store import GoalStore

    manager = GoalManager(GoalStore(tmp_path), judge=None)
    state = manager.set(
        "web:chat",
        "objective",
        contract=GoalContract(outcome="mine"),
        conversation_epoch=0,
    )

    manager.attach_contract("web:chat", state.goal_id, GoalContract(outcome="drafted"))

    assert manager.get("web:chat").contract.outcome == "mine"
