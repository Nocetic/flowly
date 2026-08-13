"""Direct CLI standing-goal delivery must outlive the initiating reply."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from flowly.cli import agent_cmd
from flowly.cli.agent_cmd import _wait_for_goal_runtime


@pytest.mark.asyncio
async def test_one_shot_wait_yields_before_draining_goal_runtime() -> None:
    scheduled = False

    def mark_scheduled() -> None:
        nonlocal scheduled
        scheduled = True

    async def wait_idle(session_key: str) -> None:
        assert scheduled is True
        assert session_key == "cli:one-shot"

    runtime = SimpleNamespace(wait_idle=AsyncMock(side_effect=wait_idle))
    loop = SimpleNamespace(goal_runtime=runtime)
    asyncio.get_running_loop().call_soon(mark_scheduled)

    await _wait_for_goal_runtime(loop, "cli:one-shot")

    runtime.wait_idle.assert_awaited_once_with("cli:one-shot")


@pytest.mark.asyncio
async def test_one_shot_wait_is_safe_when_goals_are_unavailable() -> None:
    await _wait_for_goal_runtime(SimpleNamespace(goal_runtime=None), "cli:one-shot")


def test_cli_wires_autonomous_output_and_nonblocking_input() -> None:
    source = inspect.getsource(agent_cmd)

    assert "agent_loop.set_goal_output_callback(_display_goal_output)" in source
    assert "user_input = await asyncio.to_thread(" in source
    assert "await _wait_for_goal_runtime(agent_loop, session_id)" in source
