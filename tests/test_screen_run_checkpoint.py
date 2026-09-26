"""Original-run waits, cancellation and fresh observation over real authority."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.handoff import RunHandoffs
from flowly.desktop_runtime.run_checkpoint import ScreenRunCheckpoint


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    owner = {"pid": 42, "start": "100", "boot": "one"}
    monkeypatch.setattr("flowly.desktop_runtime.handoff.process_identity", lambda _: owner)
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda _: True)
    authority = ScreenAuthority(tmp_path)
    authority.start("gen")
    authority.claim_run("run", "gen")
    store = RunHandoffs(tmp_path, authority)
    execution = store.start({"hostId": "host", "botId": "bot", "profileId": "default"}, "original-chat", "run", "gen")
    provider = HostBrowserProvider(Mock(authority=authority))
    provider._connect = AsyncMock()
    provider._dispatch = AsyncMock(return_value={"success": True, "elements": "ref_new button Continue"})
    gate = ScreenRunCheckpoint(provider, store, execution, "run", "gen")
    gate.INTERVAL = 0.005
    return gate, authority, provider


async def wait_state(gate, state):
    async with asyncio.timeout(2):
        while (await asyncio.to_thread(gate.store.status))["state"] != state:
            await asyncio.sleep(0.001)


async def test_original_run_waits_through_plain_release_and_returns_only_fresh_dom(checkpoint):
    gate, authority, provider = checkpoint
    initial = await provider.request("run", "observe", "gen", "read_page", {})
    old = Mock(dispose=AsyncMock())
    provider._refs["old-ref"] = (Mock(), old)
    held = authority.acquire("human", "gen", 1)
    waiting = asyncio.create_task(gate.wait())
    await wait_state(gate, "waiting_human")
    assert not waiting.done()
    assert not provider.observation_valid(initial)
    authority.release("human", "gen", held["epoch"])
    await wait_state(gate, "waiting_resume")
    blocked = await provider.request("run", "old-planned-click", "gen", "click", {"ref": "old-ref"})
    assert blocked["error_code"] == "SCREEN_HANDOFF_REQUIRED"
    assert not waiting.done()
    status = gate.store.status()
    gate.store.request_resume(status["handoffId"], status["controlEpoch"], "resume")
    observation = await asyncio.wait_for(waiting, 2)
    assert observation == {"success": True, "elements": "ref_new button Continue"}
    assert authority.snapshot()["runId"] == "run"
    old.dispose.assert_awaited_once()
    assert [call.args[0] for call in provider._dispatch.await_args_list] == ["read_page", "read_page"]
    assert gate.store.status()["state"] == "ready"
    await gate.finish(cancelled=False)
    assert authority.snapshot()["runId"] is None


async def test_cancelled_wait_never_releases_human_or_revives_after_late_resume(checkpoint):
    gate, authority, provider = checkpoint
    authority.acquire("human", "gen", 1)
    waiting = asyncio.create_task(gate.wait())
    await wait_state(gate, "waiting_human")
    status = gate.store.status()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert gate.store.status()["state"] == "cancelled"
    assert authority.snapshot()["control"] == "human"
    with pytest.raises(ScreenError, match="RUN_ENDED"):
        gate.store.request_resume(status["handoffId"], status["controlEpoch"], "late-resume")
    provider._dispatch.assert_not_awaited()
    await gate.finish(cancelled=True)
    assert authority.snapshot()["control"] == "human"
    assert authority.snapshot()["runId"] is None


async def test_takeover_during_resume_read_discards_result_and_waits_for_new_approval(checkpoint):
    gate, authority, provider = checkpoint
    held = authority.acquire("human", "gen", 1)
    gate.store.checkpoint(gate.execution)
    authority.release("human", "gen", held["epoch"])
    status = gate.store.status()
    gate.store.request_resume(status["handoffId"], status["controlEpoch"], "resume")
    dispatched = asyncio.Event()
    finish_read = asyncio.Event()
    async def delayed_read(*_):
        dispatched.set()
        await finish_read.wait()
        return {"success": True, "elements": "must-not-escape"}
    provider._dispatch.side_effect = delayed_read
    waiting = asyncio.create_task(gate.wait())
    await asyncio.wait_for(dispatched.wait(), 2)
    held_again = authority.acquire("second-human", "gen", authority.snapshot()["epoch"])
    finish_read.set()
    await wait_state(gate, "waiting_human")
    assert not waiting.done()
    assert provider._dispatch.await_count == 1
    authority.confirm_acquire("second-human", "gen", held_again["epoch"])
    authority.release("second-human", "gen", held_again["epoch"])
    await wait_state(gate, "waiting_resume")
    assert not waiting.done()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await gate.finish(cancelled=True)


async def test_internal_refresh_cannot_be_used_to_execute_a_write(checkpoint):
    _, _, provider = checkpoint
    with pytest.raises(ScreenError, match="RESUME_NOT_AVAILABLE"):
        await provider.request("run", "write", "gen", "click", {}, _refresh_epoch=1)
    provider._dispatch.assert_not_awaited()


async def test_unknown_fresh_read_outcome_keeps_action_fenced_for_explicit_recovery(checkpoint):
    gate, authority, provider = checkpoint
    held = authority.acquire("human", "gen", 1)
    gate.store.checkpoint(gate.execution)
    authority.release("human", "gen", held["epoch"])
    status = gate.store.status()
    gate.store.request_resume(status["handoffId"], status["controlEpoch"], "resume")
    provider._browser = Mock(is_connected=lambda: False)
    provider._dispatch.side_effect = RuntimeError("private backend payload")
    with pytest.raises(ScreenError, match="HANDOFF_RECOVERY_REQUIRED"):
        await gate.wait()
    assert authority.snapshot()["actionInFlight"]
    assert gate.store.status()["state"] == "refreshing"
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        await gate.finish(cancelled=True)
    assert authority.snapshot()["runId"] == "run"
    assert authority.snapshot()["actionInFlight"]


async def test_cancel_during_refresh_waits_for_real_backend_before_retiring_run(checkpoint):
    gate, authority, provider = checkpoint
    held = authority.acquire("human", "gen", 1)
    gate.store.checkpoint(gate.execution)
    authority.release("human", "gen", held["epoch"])
    status = gate.store.status()
    gate.store.request_resume(status["handoffId"], status["controlEpoch"], "resume")
    dispatched, finish_read = asyncio.Event(), asyncio.Event()
    async def delayed(*_):
        dispatched.set()
        await finish_read.wait()
        return {"success": True, "elements": "new page"}
    provider._dispatch.side_effect = delayed
    waiting = asyncio.create_task(gate.wait())
    await asyncio.wait_for(dispatched.wait(), 2)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert authority.snapshot()["actionInFlight"]
    cleanup = asyncio.create_task(gate.finish(cancelled=True))
    await asyncio.sleep(0.01)
    assert not cleanup.done()
    assert gate.store.status()["state"] == "cancelled"
    finish_read.set()
    await asyncio.wait_for(cleanup, 2)
    assert authority.snapshot()["runId"] is None
    assert not authority.snapshot()["actionInFlight"]


async def test_cancelled_start_cleans_late_persisted_admission(tmp_path, monkeypatch):
    import threading

    owner = {"pid": 42, "start": "100", "boot": "one"}
    monkeypatch.setattr("flowly.desktop_runtime.handoff.process_identity", lambda _: owner)
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda _: True)
    authority = ScreenAuthority(tmp_path / "authority")
    authority.start("gen")
    provider = HostBrowserProvider(Mock(directory=tmp_path, authority=authority))
    entered, release = threading.Event(), threading.Event()
    original = RunHandoffs.start
    def delayed(store, *args):
        entered.set()
        assert release.wait(5)
        return original(store, *args)
    monkeypatch.setattr(RunHandoffs, "start", delayed)
    task = asyncio.create_task(ScreenRunCheckpoint.start(provider,
        {"hostId": "host", "botId": "bot", "profileId": "default"}, "session", "run", "gen"))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert RunHandoffs(tmp_path, authority).status()["state"] == "cancelled"
    assert authority.snapshot()["runId"] is None
    assert provider.execution._run_checks == {}
