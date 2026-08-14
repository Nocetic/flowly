from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.agent import inflight
from flowly.agent.loop import (
    _GOAL_BASE_USER_EPOCH,
    _GOAL_CONTINUATION_ID,
    _GOAL_KICKOFF,
    AgentLoop,
    _AgentGoalDelivery,
)
from flowly.bus.events import InboundMessage, OutboundMessage
from flowly.goals.commands import GoalCommandResult
from flowly.goals.models import GoalStatus


@dataclass
class _State:
    goal_id: str = "goal-1"
    goal: str = "ship the change"
    is_active: bool = True


class _Manager:
    def __init__(self) -> None:
        self.state = _State()

    def get(self, _session_key: str) -> _State:
        return self.state

    def continuation_prompt(self, _state: _State) -> str:
        return "fresh continuation"


def _bare_loop() -> AgentLoop:
    loop = object.__new__(AgentLoop)
    loop._goal_user_epochs = {}
    loop._session_turn_locks = {}
    loop.goal_runtime = object()
    loop.goal_manager = _Manager()
    loop._process_message_unlocked = AsyncMock(
        return_value=OutboundMessage(channel="web", chat_id="chat", content="ok")
    )
    return loop


@pytest.mark.asyncio
async def test_goal_continuation_uses_the_surfaces_own_turn_entry() -> None:
    """A registered surface runs the turn; the delivery never runs its own."""
    loop = object.__new__(AgentLoop)
    loop._process_message = AsyncMock()
    submitted: list[tuple[str, dict]] = []

    async def submitter(session_key: str, metadata: dict) -> bool:
        submitted.append((session_key, metadata))
        return True

    loop.register_goal_turn_submitter("web", submitter)
    delivery = _AgentGoalDelivery(
        loop,
        session_key="web:stable-chat",
        channel="web",
        chat_id="relay-1",
        direct=False,
    )

    result = await delivery.run_continuation(
        session_key="web:stable-chat",
        goal_id="goal-1",
        user_epoch=3,
        kickoff=False,
    )

    # Nothing is executed here: the surface's normal runner owns the turn and
    # the judge picks it up through the ordinary delivered-turn hook.
    assert result is None
    loop._process_message.assert_not_awaited()
    assert len(submitted) == 1
    session_key, metadata = submitted[0]
    assert session_key == "web:stable-chat"
    assert {k: v for k, v in metadata.items() if k != "on_run_started"} == {
        "_goal_continuation_goal_id": "goal-1",
        "_goal_base_user_epoch": 3,
        "_goal_kickoff": False,
        "goal_run": True,
    }
    # The surface reports its run id back so a goal control can stop it.
    assert callable(metadata["on_run_started"])


@pytest.mark.asyncio
async def test_goal_continuation_falls_back_to_the_bus_for_plain_channels() -> None:
    """Unregistered surfaces still get an ordinary inbound user turn."""
    loop = object.__new__(AgentLoop)
    loop.bus = Mock()
    loop.bus.publish_inbound = AsyncMock()
    delivery = _AgentGoalDelivery(
        loop,
        session_key="web:stable-chat",
        channel="telegram",
        chat_id="42",
        direct=False,
    )

    result = await delivery.run_continuation(
        session_key="web:stable-chat",
        goal_id="goal-1",
        user_epoch=3,
        kickoff=True,
    )

    assert result is None
    published = loop.bus.publish_inbound.await_args.args[0]
    # The stable relay session key survives the channel/chat_id split.
    assert published.session_key == "web:stable-chat"
    assert published.chat_id == "42"
    assert published.metadata["_goal_kickoff"] is True
    assert published.metadata["goal_run"] is True


@pytest.mark.asyncio
async def test_direct_surface_prefers_the_gateway_runner() -> None:
    loop = object.__new__(AgentLoop)
    loop.bus = Mock()
    loop.bus.publish_inbound = AsyncMock()
    direct_calls: list[str] = []
    web_calls: list[str] = []

    async def direct(session_key: str, metadata: dict) -> bool:
        direct_calls.append(session_key)
        return True

    async def web(session_key: str, metadata: dict) -> bool:
        web_calls.append(session_key)
        return True

    loop.register_goal_turn_submitter("direct", direct)
    loop.register_goal_turn_submitter("web", web)
    delivery = _AgentGoalDelivery(
        loop,
        session_key="web:stable-chat",
        channel="web",
        chat_id="relay-1",
        direct=True,
    )

    await delivery.run_continuation(
        session_key="web:stable-chat",
        goal_id="goal-1",
        user_epoch=0,
        kickoff=False,
    )

    assert direct_calls == ["web:stable-chat"]
    assert web_calls == []
    loop.bus.publish_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_goal_notice_reads_state_from_relay_stable_session_key() -> None:
    loop = object.__new__(AgentLoop)
    loop.goal_manager = Mock()
    state = Mock()
    state.to_public_dict.return_value = {"goalId": "goal-1"}
    loop.goal_manager.get.return_value = state
    loop.publish_goal_snapshot = AsyncMock()
    delivery = _AgentGoalDelivery(
        loop,
        session_key="web:stable-chat",
        channel="web",
        chat_id="relay-1",
        direct=False,
    )

    decision = Mock(message="completed", status=GoalStatus.DONE)
    await delivery.deliver_notice(decision)

    loop.goal_manager.get.assert_called_once_with("web:stable-chat")
    kwargs = loop.publish_goal_snapshot.await_args
    assert kwargs.args[:4] == ("web:stable-chat", "web", "relay-1", "completed")
    # A goal that ENDED reports in words, not only as chip state.
    assert kwargs.kwargs["terminal"] is True


@pytest.mark.asyncio
async def test_routine_goal_progress_stays_chip_state() -> None:
    loop = object.__new__(AgentLoop)
    loop.goal_manager = Mock()
    state = Mock()
    state.to_public_dict.return_value = {"goalId": "goal-1"}
    loop.goal_manager.get.return_value = state
    loop.publish_goal_snapshot = AsyncMock()
    delivery = _AgentGoalDelivery(
        loop,
        session_key="web:stable-chat",
        channel="web",
        chat_id="relay-1",
        direct=False,
    )

    await delivery.deliver_notice(Mock(message="↻ continuing", status=GoalStatus.ACTIVE))

    assert loop.publish_goal_snapshot.await_args.kwargs["terminal"] is False


def test_goal_turn_recognizes_safe_context_recovery_exhaustion() -> None:
    loop = object.__new__(AgentLoop)
    exhausted = loop._goal_turn_from_outbound(
        "web:chat",
        OutboundMessage(
            "web",
            "chat",
            "too large",
            metadata={"error": {"category": "input_too_large"}},
        ),
        user_epoch=2,
    )
    partial = loop._goal_turn_from_outbound(
        "web:chat",
        OutboundMessage(
            "web",
            "chat",
            "partial",
            metadata={
                "error": {
                    "category": "context_overflow",
                    "partial_content_delivered": True,
                }
            },
        ),
        user_epoch=2,
    )

    assert exhausted.compaction_failed is True
    assert partial.compaction_failed is False
    assert partial.provider_error is True


@pytest.mark.asyncio
async def test_goal_continuation_rebuilds_fresh_content_without_tool_policy() -> None:
    loop = _bare_loop()
    loop._goal_user_epochs["web:chat"] = 4
    message = InboundMessage(
        channel="web",
        sender_id="goal",
        chat_id="chat",
        content="stale prompt",
        metadata={
            _GOAL_CONTINUATION_ID: "goal-1",
            _GOAL_BASE_USER_EPOCH: 4,
            _GOAL_KICKOFF: False,
        },
    )

    await loop._process_message(message)

    submitted = loop._process_message_unlocked.await_args.args[0]
    assert submitted.content == "fresh continuation"
    assert "tools_allowed" not in submitted.metadata
    assert "tool_policy" not in submitted.metadata


@pytest.mark.asyncio
async def test_real_user_arrival_invalidates_a_stale_goal_continuation() -> None:
    loop = _bare_loop()
    loop._goal_user_epochs["web:chat"] = 2
    user = InboundMessage("web", "person", "chat", "new instruction")
    await loop._process_message(user)

    stale = InboundMessage(
        channel="web",
        sender_id="goal",
        chat_id="chat",
        content="",
        metadata={
            _GOAL_CONTINUATION_ID: "goal-1",
            _GOAL_BASE_USER_EPOCH: 2,
        },
    )
    loop._process_message_unlocked.reset_mock()

    assert await loop._process_message(stale) is None
    loop._process_message_unlocked.assert_not_awaited()
    assert loop.goal_user_epoch("web:chat") == 3


@pytest.mark.asyncio
async def test_real_user_reply_resumes_a_goal_waiting_for_input() -> None:
    loop = _bare_loop()
    loop.goal_manager.resume_for_user_input = Mock()

    await loop._process_message(
        InboundMessage("web", "person", "chat", "Use the staging environment")
    )

    loop.goal_manager.resume_for_user_input.assert_called_once_with("web:chat")


@pytest.mark.asyncio
async def test_same_session_turns_are_serialized_but_arrivals_get_distinct_epochs() -> None:
    loop = _bare_loop()
    active = 0
    peak = 0

    async def process(message: InboundMessage) -> OutboundMessage:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return OutboundMessage("web", message.chat_id, "ok")

    loop._process_message_unlocked = AsyncMock(side_effect=process)
    first = InboundMessage("web", "person", "chat", "first")
    second = InboundMessage("web", "person", "chat", "second")

    await asyncio.gather(loop._process_message(first), loop._process_message(second))

    assert peak == 1
    assert first.metadata["_goal_user_epoch"] == 1
    assert second.metadata["_goal_user_epoch"] == 2


@pytest.mark.asyncio
async def test_goal_control_commands_do_not_preempt_the_continuation_epoch() -> None:
    loop = _bare_loop()
    loop._goal_user_epochs["web:chat"] = 6

    await loop._process_message(InboundMessage("web", "person", "chat", "/subgoal preserve relay"))

    assert loop.goal_user_epoch("web:chat") == 6


@pytest.mark.asyncio
async def test_goal_slash_command_surfaces_snapshot_and_kickoff_metadata() -> None:
    loop = object.__new__(AgentLoop)
    loop._context_epoch = {"web:chat": 2}
    loop._goal_user_epochs = {"web:chat": 5}
    state = _State()
    state.to_public_dict = Mock(return_value={"goalId": "goal-1"})  # type: ignore[attr-defined]
    result = GoalCommandResult(
        "goal accepted",
        state=state,  # type: ignore[arg-type]
        kickoff_goal_id="goal-1",
    )
    loop.goal_commands = Mock()
    loop.goal_commands.goal = AsyncMock(return_value=result)

    response = await loop._process_message_inner(
        InboundMessage("web", "person", "chat", "/goal ship")
    )

    assert response is not None
    assert response.content == "goal accepted"
    assert response.metadata["goal"] == {"goalId": "goal-1"}
    assert response.metadata["_goal_kickoff_goal_id"] == "goal-1"
    loop.goal_commands.goal.assert_awaited_once_with("web:chat", "ship", conversation_epoch=2)


@pytest.mark.asyncio
async def test_a_surface_that_cannot_take_the_turn_falls_back_to_the_bus() -> None:
    """No bound socket must not mean the goal silently stops working."""
    loop = object.__new__(AgentLoop)
    loop.bus = Mock()
    loop.bus.publish_inbound = AsyncMock()
    declined: list[str] = []

    async def submitter(session_key: str, _metadata: dict) -> bool:
        declined.append(session_key)
        return False

    loop.register_goal_turn_submitter("web", submitter)
    delivery = _AgentGoalDelivery(
        loop,
        session_key="web:stable-chat",
        channel="web",
        chat_id="relay-1",
        direct=False,
    )

    await delivery.run_continuation(
        session_key="web:stable-chat", goal_id="goal-1", user_epoch=0, kickoff=False,
    )

    assert declined == ["web:stable-chat"]
    published = loop.bus.publish_inbound.await_args.args[0]
    assert published.session_key == "web:stable-chat"
    assert published.metadata["_goal_continuation_goal_id"] == "goal-1"
    # The run-start hook belongs to the surface that minted a run id; the bus
    # path has none, so it must not leak a stale callback into the turn.
    assert "on_run_started" not in published.metadata
