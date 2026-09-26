import asyncio

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.execution import ActionOutcomeUnknownError, ScreenExecution


@pytest.fixture
def execution(tmp_path):
    authority = ScreenAuthority(tmp_path)
    authority.start("generation")
    return ScreenExecution(authority)


async def test_disconnect_does_not_release_an_unacknowledged_browser_command(execution):
    started, acknowledged = asyncio.Event(), asyncio.Event()

    async def operation():
        started.set()
        await acknowledged.wait()
        return "private observation"

    caller = asyncio.create_task(execution.action("run", "action", "generation", operation))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert execution.authority.snapshot()["actionInFlight"]
    takeover = await execution.acquire("viewer", "generation", 1, wait_seconds=0)
    assert takeover["control"] == "human-pending"
    assert not execution.authority.may_input("viewer", "generation", takeover["epoch"])
    finishing = asyncio.create_task(execution.finish_run("run", "generation"))
    await asyncio.sleep(0)
    assert not finishing.done()
    acknowledged.set()
    await finishing
    human = await execution.acquire("viewer", "generation", takeover["epoch"])
    assert human["control"] == "human"
    assert human["runId"] is None


async def test_takeover_discards_inflight_capture_before_returning_to_model(execution):
    started, acknowledged = asyncio.Event(), asyncio.Event()

    async def capture():
        started.set()
        await acknowledged.wait()
        return b"screenshot that must not escape"

    caller = asyncio.create_task(execution.action("run", "capture", "generation", capture))
    await started.wait()
    pending = await execution.acquire("viewer", "generation", 1, wait_seconds=0)
    acknowledged.set()
    with pytest.raises(ScreenError, match="STALE_OBSERVATION"):
        await caller
    assert (await execution.acquire("viewer", "generation", pending["epoch"]))["control"] == "human"
    assert execution.authority.snapshot()["runId"] == "run"


async def test_another_run_cannot_interleave_between_actions(execution):
    async def operation():
        return "result"

    assert await execution.action("run", "a", "generation", operation) == "result"
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        await execution.action("other", "b", "generation", operation)
    await execution.finish_run("run", "generation")
    assert await execution.action("other", "b", "generation", operation) == "result"


async def test_human_admission_does_not_even_create_backend_operation(execution):
    execution.authority.acquire("viewer", "generation", 1)

    def forbidden():
        pytest.fail("backend command must never be created")

    with pytest.raises(ScreenError, match="HUMAN_HAS_CONTROL"):
        await execution.action("run", "a", "generation", forbidden)


async def test_backend_failure_keeps_run_owned_but_releases_action(execution):
    async def failed():
        raise RuntimeError("backend failed")

    with pytest.raises(RuntimeError, match="backend failed"):
        await execution.action("run", "a", "generation", failed)
    state = execution.authority.snapshot()
    assert not state["actionInFlight"]
    assert state["runId"] == "run"
    await execution.finish_run("run", "generation")


async def test_takeover_is_still_pending_for_command_owned_by_another_worker(execution):
    admission = execution.authority.begin_action("run", "a", "generation")
    state = await execution.acquire("viewer", "generation", 1)
    assert state["control"] == "human-pending"
    assert not execution.authority.end_action(admission)
    assert (await execution.acquire("viewer", "generation", state["epoch"]))["control"] == "human"


async def test_unknown_backend_outcome_does_not_clear_admission(execution):
    async def disconnected():
        raise ActionOutcomeUnknownError()

    with pytest.raises(ActionOutcomeUnknownError):
        await execution.action("run", "a", "generation", disconnected)
    assert execution.authority.snapshot()["actionInFlight"]
    assert (await execution.acquire("viewer", "generation", 1))["control"] == "human-pending"


async def test_gateway_shutdown_cancelling_worker_leaves_durable_admission(execution):
    started = asyncio.Event()

    async def operation():
        started.set()
        await asyncio.Event().wait()

    caller = asyncio.create_task(execution.action("run", "a", "generation", operation))
    await started.wait()
    for worker in execution._pending:
        worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert execution.authority.snapshot()["actionInFlight"]
