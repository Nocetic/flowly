"""Cancelled waiters must not make still-running Python workers look idle."""

import asyncio
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.desktop_runtime import installation_gate as gate
from flowly.desktop_runtime.agent_activity import background_agent_activity, run_agent_worker
from flowly.desktop_runtime.authority import ScreenError
from tests.test_screen_install_jobs import fixture as job_fixture

installation_job = job_fixture
pytestmark = pytest.mark.skipif(os.name != "posix", reason="real POSIX environment leases")


@pytest.fixture(autouse=True)
def linux_gate(monkeypatch, installation_job):
    monkeypatch.setattr(gate, "sys", SimpleNamespace(platform="linux", prefix=sys.prefix))


async def test_background_admission_is_deferred_without_executing_work():
    body = AsyncMock()
    guarded = background_agent_activity(body)
    with gate.environment_activity(exclusive=True):
        assert await guarded("argument") is None
    body.assert_not_called()
    await guarded("argument")
    body.assert_awaited_once_with("argument")


async def test_background_body_errors_are_preserved_and_do_not_leak_leases():
    body = AsyncMock(side_effect=ScreenError("INSTALL_BUSY"))
    with pytest.raises(ScreenError, match="INSTALL_BUSY"):
        await background_agent_activity(body)()
    with gate.environment_activity(exclusive=True):
        pass


async def test_background_cancellation_releases_the_task_activity():
    entered = asyncio.Event()
    @background_agent_activity
    async def work():
        entered.set()
        await asyncio.Future()
    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            with gate.environment_activity(exclusive=True):
                pytest.fail("maintenance entered active background task")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with gate.environment_activity(exclusive=True):
        pass


async def test_thread_keeps_activity_after_async_waiter_is_cancelled():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def work():
        entered.set()
        assert release.wait(3)
    def worker():
        try:
            run_agent_worker(work)
        finally:
            finished.set()
    task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not finished.is_set()
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            with gate.environment_activity(exclusive=True):
                pytest.fail("cancelled waiter released a running worker's lease")
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    with gate.environment_activity(exclusive=True):
        pass


async def test_thread_checks_maintenance_at_actual_execution_not_scheduling():
    entered = threading.Event()
    with gate.environment_activity(exclusive=True):
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            await asyncio.to_thread(run_agent_worker, entered.set)
    assert not entered.is_set()


@pytest.mark.parametrize("method,args", [
    ("_maybe_consolidate", ("timer",)),
    ("_maybe_skill_pass", ("curate",)),
    ("_autotitle_session", (None, "question", "answer")),
    ("_post_turn_compaction", (None,)),
])
async def test_real_background_entry_points_defer_before_accessing_state(method, args):
    from flowly.agent.loop import AgentLoop

    # No initialized fields: reaching any body would fail instead of deferring.
    agent = object.__new__(AgentLoop)
    with gate.environment_activity(exclusive=True):
        assert await getattr(agent, method)(*args) is None


@pytest.mark.parametrize("method", ["_maybe_consolidate", "_maybe_skill_pass", "_autotitle_session"])
async def test_real_background_work_holds_activity_until_cancelled(method, monkeypatch):
    from flowly.agent.loop import AgentLoop

    agent = object.__new__(AgentLoop)
    entered = asyncio.Event()

    async def wait_for_cancellation(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    if method == "_maybe_consolidate":
        agent._memory_gov = Mock()
        agent._memory_gov.is_dirty.return_value = True
        agent._consolidate_tool = SimpleNamespace(execute=wait_for_cancellation)
        agent._auto_consolidate = True
        agent._consolidate_lock = None
        args = ("timer",)
    elif method == "_maybe_skill_pass":
        agent._skill_gov = Mock()
        agent._skill_improve_tool = SimpleNamespace(execute=wait_for_cancellation)
        agent._skill_lock = None
        args = ("mine",)
    else:
        agent.provider, agent.model = None, "test-model"
        monkeypatch.setattr("flowly.session.title.generate_title", wait_for_cancellation)
        args = (SimpleNamespace(key="session"), "question", "answer")
    task = asyncio.create_task(getattr(agent, method)(*args))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            with gate.environment_activity(exclusive=True):
                pytest.fail("maintenance entered background operation")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with gate.environment_activity(exclusive=True):
        pass


async def test_real_compaction_holds_activity_before_opening_cycle():
    from flowly.agent.loop import AgentLoop

    agent = object.__new__(AgentLoop)
    checked = False

    def open_cycle(key):
        nonlocal checked
        assert key == "session"
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            with gate.environment_activity(exclusive=True):
                pytest.fail("maintenance entered compaction")
        checked = True
        raise RuntimeError("cycle sentinel")

    agent.compaction_cycle = open_cycle
    with pytest.raises(RuntimeError, match="cycle sentinel"):
        await agent._post_turn_compaction(SimpleNamespace(session_key="session"))
    assert checked
    with gate.environment_activity(exclusive=True):
        pass


async def test_real_dreamer_worker_retains_lease_after_waiter_cancellation():
    from flowly.agent.loop import AgentLoop

    agent = object.__new__(AgentLoop)
    entered, release = threading.Event(), threading.Event()
    def dream(*, max_messages):
        assert max_messages == 25
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(ran=False, reason="test")

    agent._dreamer = SimpleNamespace(run=dream)
    agent._dreamer_lock = asyncio.Lock()
    agent._dreamer_max_messages = 25
    task = asyncio.create_task(agent._maybe_run_dreamer("timer"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(ScreenError, match="INSTALL_BUSY"):
            with gate.environment_activity(exclusive=True):
                pytest.fail("cancelled dreamer waiter unlocked its worker")
    finally:
        release.set()
        # Drain this loop's actual executor: the cancelled task no longer joins
        # the worker, and no test should leave its environment lease behind.
        await asyncio.get_running_loop().shutdown_default_executor()
    with gate.environment_activity(exclusive=True):
        pass


async def test_real_dreamer_does_not_start_during_maintenance():
    from flowly.agent.loop import AgentLoop

    agent = object.__new__(AgentLoop)
    agent._dreamer = SimpleNamespace(run=Mock())
    agent._dreamer_lock = asyncio.Lock()
    agent._dreamer_max_messages = 25
    with gate.environment_activity(exclusive=True):
        await agent._maybe_run_dreamer("timer")
    agent._dreamer.run.assert_not_called()
