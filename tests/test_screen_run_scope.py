"""Every agent run of a profile uses its running screen: real authority/checkpoints."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.events import InboundMessage, OutboundMessage
from flowly.channels.web import _WebInboundMessage
from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.handoff import RunHandoffs
from flowly.desktop_runtime.host_screen_run import (
    HOST_SCREEN_RUN,
    bound_screen_run,
    current_screen_run,
)
from flowly.gateway.server import GatewayServer


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr("flowly.desktop_runtime.handoff.process_identity", lambda _: {"pid": 42, "start": "100", "boot": "one"})
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda _: True)
    monkeypatch.setattr("flowly.desktop_runtime.installation_gate.agent_environment_activity", nullcontext)
    authority = ScreenAuthority(tmp_path)
    authority.start("generation")
    runtime = SimpleNamespace(directory=tmp_path, authority=authority,
                              status=lambda: {"phase": "running", "generation": "generation"})
    provider = HostBrowserProvider(runtime)
    provider._connect = AsyncMock()
    provider._dispatch = AsyncMock(return_value={"success": True, "text": "fresh page"})
    identity = {"hostId": "host", "profileId": "default", "screenId": "browser"}
    gateway = Mock()
    gateway._get_host_screen_browser.return_value = (identity, provider)
    gateway._host_screen_checkpoint_identity.return_value = {"hostId": "host", "profileId": "default", "botId": "bot"}
    loop = object.__new__(AgentLoop)
    loop._gateway_server = gateway
    relay = _WebInboundMessage(channel="web", sender_id="browser", chat_id="browser", content="Read page",
                               metadata={"run_id": "run"}, _session_key="web:original")
    return SimpleNamespace(gateway=gateway, provider=provider, runtime=runtime, authority=authority, loop=loop,
                           relay=relay, handoffs=lambda: RunHandoffs(tmp_path, authority).status())


def browse(host, request_id="read"):
    return GatewayServer.send_browser_tool_request(host.gateway, request_id, "read_page", {})


async def test_a_relay_turn_uses_the_screen_and_finishes_its_checkpoint(host):
    async def process(message):
        run = current_screen_run()
        assert run.run_id == "run" and run.session_key == "web:original" and run.uses_screen()
        # Entering the run claimed nothing.
        assert host.authority.snapshot()["runId"] is None and bound_screen_run() is None
        assert (await browse(host))["text"] == "fresh page"
        assert host.authority.snapshot()["runId"] == "run"
        assert host.handoffs()["originSessionKey"] == "web:original"
        return OutboundMessage(channel="web", chat_id="browser", content="Done")

    host.loop._process_message_in_environment = process
    response = await host.loop._process_message(host.relay)
    assert response.content == "Done"
    assert host.authority.snapshot()["runId"] is None
    assert host.handoffs()["state"] == "finished"
    assert HOST_SCREEN_RUN.get() is None


async def test_a_turn_that_never_browses_leaves_no_trace_on_the_screen(host):
    async def process(message):
        return OutboundMessage(channel="web", chat_id="browser", content="Hello")

    host.loop._process_message_in_environment = process
    await host.loop._process_message(host.relay)
    assert host.authority.snapshot()["runId"] is None
    assert host.handoffs() is None
    host.provider._dispatch.assert_not_awaited()


async def test_a_routine_without_a_run_id_still_gets_its_own_screen_run(host):
    seen = {}

    async def process(message):
        seen["run"] = current_screen_run().run_id
        assert (await browse(host))["success"]
        seen["claimed"] = host.authority.snapshot()["runId"]
        return OutboundMessage(channel="cron", chat_id="job", content="Done")

    host.loop._process_message_in_environment = process
    await host.loop._process_message(InboundMessage(channel="cron", sender_id="cron", chat_id="job", content="Check"))
    assert seen["run"].startswith("screen-") and seen["claimed"] == seen["run"]
    assert host.authority.snapshot()["runId"] is None


async def test_a_nested_run_gets_its_own_scope_and_restores_the_outer_one(host):
    inner_message = InboundMessage(channel="system", sender_id="agent", chat_id="inner", content="Help",
                                   metadata={"run_id": "inner"})

    async def process(message):
        if message is inner_message:
            assert current_screen_run().run_id == "inner"
            return OutboundMessage(channel="system", chat_id="inner", content="inner")
        outer = current_screen_run()
        assert (await browse(host))["success"]
        await host.loop._process_message(inner_message)
        assert current_screen_run() is outer and bound_screen_run() is outer.binding
        return OutboundMessage(channel="web", chat_id="browser", content="outer")

    host.loop._process_message_in_environment = process
    await host.loop._process_message(host.relay)
    assert host.authority.snapshot()["runId"] is None


async def test_a_cancelled_turn_preserves_human_control(host):
    entered = asyncio.Event()

    async def process(message):
        assert (await browse(host))["success"]
        entered.set()
        await asyncio.Event().wait()

    host.loop._process_message_in_environment = process
    task = asyncio.create_task(host.loop._process_message(host.relay))
    await asyncio.wait_for(entered.wait(), 2)
    host.authority.acquire("human", "generation", 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.authority.snapshot()["control"] == "human"
    assert host.authority.snapshot()["runId"] is None
    assert host.handoffs()["state"] == "cancelled"


@pytest.mark.parametrize(("raised", "reported"), [
    ("HANDOFF_RECOVERY_REQUIRED", "HANDOFF_RECOVERY_REQUIRED"), ("RUNTIME_STATE_INVALID", "CONTROL_UNAVAILABLE"),
])
async def test_a_screen_failure_ends_the_run_with_one_typed_error_on_every_transport(host, raised, reported):
    async def process(message):
        assert (await browse(host))["success"]
        raise ScreenError(raised)

    host.loop._process_message_in_environment = process
    response = await host.loop._process_message(host.relay)
    assert response.metadata["error"]["code"] == reported
    assert response.metadata["run_id"] == "run"
    assert host.authority.snapshot()["runId"] is None
    assert host.handoffs()["state"] == "cancelled"


async def test_a_host_without_profile_screens_runs_turns_unscoped(host, monkeypatch):
    monkeypatch.setattr("flowly.desktop_runtime.host_screen_run.sys.platform", "darwin")

    async def process(message):
        assert current_screen_run() is None
        return OutboundMessage(channel="web", chat_id="browser", content="Done")

    host.loop._process_message_in_environment = process
    assert (await host.loop._process_message(host.relay)).content == "Done"
    host.gateway._get_host_screen_browser.assert_not_called()


async def test_the_screen_run_waits_for_an_explicit_resume_after_a_takeover(host):
    async def process(message):
        assert GatewayServer.has_browser_provider(host.gateway)
        assert (await browse(host, "observe"))["success"]
        checkpoint = bound_screen_run().checkpoint
        checkpoint.INTERVAL = 0.005
        human = host.authority.acquire("human", "generation", 1)
        waiting = asyncio.create_task(checkpoint.wait())
        async with asyncio.timeout(2):
            while checkpoint.store.status()["state"] != "waiting_human":
                await asyncio.sleep(0.005)
        blocked = await GatewayServer.send_browser_tool_request(host.gateway, "stale", "click", {"selector": "button"})
        assert blocked["error_code"] in {"HUMAN_HAS_CONTROL", "SCREEN_HANDOFF_REQUIRED"}
        host.authority.release("human", "generation", human["epoch"])
        async with asyncio.timeout(2):
            while checkpoint.store.status()["state"] != "waiting_resume":
                await asyncio.sleep(0.005)
        assert not waiting.done()
        status = checkpoint.store.status()
        checkpoint.store.request_resume(status["handoffId"], status["controlEpoch"], "resume")
        assert await asyncio.wait_for(waiting, 2) == {"success": True, "text": "fresh page"}
        assert [call.args[0] for call in host.provider._dispatch.await_args_list] == ["read_page", "read_page"]
        return OutboundMessage(channel="web", chat_id="browser", content="Done")

    host.loop._process_message_in_environment = process
    assert (await host.loop._process_message(host.relay)).content == "Done"
    assert host.handoffs()["state"] == "finished"
