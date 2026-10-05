"""Live agent work and optional package maintenance cannot enter together."""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.events import InboundMessage
from flowly.desktop_runtime import install_jobs
from flowly.desktop_runtime import installation_gate as gate
from flowly.desktop_runtime.authority import ScreenError
from flowly.providers.base import LLMResponse
from tests.test_screen_install_jobs import OP, PLAN
from tests.test_screen_install_jobs import fixture as job_fixture
from tests.test_subagent_safety import _build_manager

installation_job = job_fixture
pytestmark = pytest.mark.skipif(os.name != "posix", reason="real POSIX maintenance leases")


@pytest.fixture(autouse=True)
def linux_gate(monkeypatch, installation_job):
    # Exercise real flock on POSIX without changing Python's global platform.
    monkeypatch.setattr(gate, "sys", SimpleNamespace(platform="linux", prefix=sys.prefix))


def agent():
    loop = AgentLoop.__new__(AgentLoop)
    loop._process_message_in_environment = AsyncMock(return_value=None)
    return loop


def message():
    return InboundMessage(channel="cli", sender_id="user", chat_id="test", content="test")


async def test_agent_turn_blocks_installation_until_completion(installation_job):
    service, _ = installation_job
    loop = agent()
    entered, release = asyncio.Event(), asyncio.Event()
    async def body(_):
        entered.set()
        await release.wait()
    loop._process_message_in_environment.side_effect = body
    task = asyncio.create_task(loop._process_message(message()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            service.submit(PLAN, OP)
        assert not service.path.exists()
        install_jobs.install_system.assert_not_called()
    finally:
        release.set()
        await task
    with gate.environment_activity(exclusive=True):
        pass


async def test_maintenance_rejects_turn_before_any_conversation_side_effect():
    loop = agent()
    with gate.environment_activity(exclusive=True):
        result = await loop._process_message(message())
    assert result.metadata["error"]["code"] == "INSTALL_BUSY"
    assert "being set up" in result.content
    loop._process_message_in_environment.assert_not_called()


@pytest.mark.parametrize("code", ["INSTALL_RECOVERY_REQUIRED", "INSTALL_RESTART_REQUIRED", "INSTALL_STATE_INVALID"])
async def test_durable_maintenance_condition_blocks_agent_after_lock_loss(monkeypatch, code):
    monkeypatch.setattr(install_jobs, "assert_environment_installation_idle", Mock(side_effect=ScreenError(code)))
    loop = agent()
    result = await loop._process_message(message())
    assert result.metadata["error"]["code"] == code
    loop._process_message_in_environment.assert_not_called()
    if code != "INSTALL_RESTART_REQUIRED":
        # A paused agent says how to get it back.
        assert "flowly screen install" in result.content and "Flowly Desktop" in result.content
    with gate.environment_activity(exclusive=True):
        pass


async def test_one_cancelled_turn_does_not_release_another_turn_lease():
    entered = [asyncio.Event(), asyncio.Event()]
    loops = [agent(), agent()]
    tasks = []
    for index, loop in enumerate(loops):
        async def body(_, index=index):
            entered[index].set()
            await asyncio.Future()
        loop._process_message_in_environment.side_effect = body
        tasks.append(asyncio.create_task(loop._process_message(message())))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 2)
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            with gate.environment_activity(exclusive=True):
                pytest.fail("other turn lost its lease")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    with gate.environment_activity(exclusive=True):
        pass


async def test_body_errors_are_not_mislabeled_as_maintenance_and_release_activity():
    loop = agent()
    loop._process_message_in_environment.side_effect = RuntimeError("body failure")
    with pytest.raises(RuntimeError, match="body failure"):
        await loop._process_message(message())
    with gate.environment_activity(exclusive=True):
        pass


async def test_subagent_holds_its_own_activity_until_result_is_saved(tmp_path):
    manager = _build_manager(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def chat(**kwargs):
        entered.set()
        await release.wait()
        return LLMResponse(content="done", tool_calls=[])
    manager.provider.chat = AsyncMock(side_effect=chat)
    task = asyncio.create_task(manager.spawn("task", "worker", "cli", "test", wait=True))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            with gate.environment_activity(exclusive=True):
                pytest.fail("maintenance entered live subagent")
    finally:
        release.set()
        await task
    assert manager._registry.all()[0].outcome == "ok"
    with gate.environment_activity(exclusive=True):
        pass


async def test_subagent_refusal_is_durable_and_never_calls_provider(tmp_path):
    manager = _build_manager(tmp_path)
    manager.provider.chat = AsyncMock()
    with gate.environment_activity(exclusive=True):
        await manager.spawn("task", "worker", "cli", "test", wait=True)
    manager.provider.chat.assert_not_called()
    record = manager._registry.all()[0]
    assert record.outcome == "error" and record.error_code == "INSTALL_BUSY"


@pytest.mark.parametrize("platform", ["darwin", "win32"])
async def test_other_hosts_do_not_enter_linux_maintenance(monkeypatch, platform):
    monkeypatch.setattr(gate, "sys", SimpleNamespace(platform=platform))
    check = Mock(side_effect=AssertionError("Linux maintenance must not run"))
    monkeypatch.setattr(install_jobs, "assert_environment_installation_idle", check)
    loop = agent()
    assert await loop._process_message(message()) is None
    loop._process_message_in_environment.assert_awaited_once()
    check.assert_not_called()
