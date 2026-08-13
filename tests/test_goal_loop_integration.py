from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from flowly.agent.loop import (
    _GOAL_BASE_USER_EPOCH,
    _GOAL_CONTINUATION_ID,
    _GOAL_KICKOFF,
    AgentLoop,
)
from flowly.bus.events import InboundMessage, OutboundMessage


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
