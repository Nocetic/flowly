"""Real authority/checkpoint fencing across relay ingress and queued agent turns."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel, _WebInboundMessage
from flowly.config.schema import WebChannelConfig
from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.context import SCREEN_RUN_BINDING
from flowly.desktop_runtime.handoff import RunHandoffs
from flowly.desktop_runtime.relay_chat import (
    METADATA_KEY,
    admit_relay_screen_run,
    relay_screen_scope,
)
from flowly.gateway.server import _BROWSER_RUN_BINDING


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr("flowly.desktop_runtime.handoff.process_identity", lambda _: {"pid": 42, "start": "100", "boot": "one"})
    monkeypatch.setattr("flowly.desktop_runtime.handoff.is_alive", lambda _: True)
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
    gateway._host_screen_checkpoint_identity.return_value = {**identity, "botId": "bot"}
    grant = {**identity, "botId": "bot", "providerId": "host-desktop-browser", "generation": "generation"}
    current = SimpleNamespace(value=True)
    def admission():
        return admit_relay_screen_run(gateway, grant, "web:original", "run", lambda: current.value)
    message = _WebInboundMessage(channel="web", sender_id="browser", chat_id="browser", content="Read page",
                                 metadata={"run_id": "run"}, _session_key="web:original")
    return SimpleNamespace(gateway=gateway, provider=provider, runtime=runtime, authority=authority,
                           grant=grant, current=current, admission=admission, message=message)


async def test_bus_scope_owns_real_checkpoint_and_restores_context(host):
    before_browser, before_screen = _BROWSER_RUN_BINDING.get(), SCREEN_RUN_BINDING.get()
    admission = host.admission()
    async with relay_screen_scope(admission, host.message) as scope:
        binding = SCREEN_RUN_BINDING.get()
        assert binding.run_id == "run" and binding.provider is host.provider
        assert _BROWSER_RUN_BINDING.get().screen_generation == "generation"
        assert host.authority.snapshot()["runId"] == "run"
        assert binding.checkpoint.store.status()["originSessionKey"] == "web:original"
        scope.succeeded = True
    assert host.authority.snapshot()["runId"] is None
    assert RunHandoffs(host.runtime.directory, host.authority).status()["state"] == "finished"
    assert _BROWSER_RUN_BINDING.get() is before_browser
    assert SCREEN_RUN_BINDING.get() is before_screen
    with pytest.raises(ScreenError, match="BROWSER_ACCESS_NOT_GRANTED"):
        async with relay_screen_scope(admission, host.message):
            pytest.fail("admission replayed")


@pytest.mark.parametrize("mutation,code", [
    ("raw_metadata", "BROWSER_ACCESS_NOT_GRANTED"), ("session", "BROWSER_ACCESS_NOT_GRANTED"),
    ("run", "BROWSER_ACCESS_NOT_GRANTED"), ("disconnect", "BROWSER_ACCESS_NOT_GRANTED"),
    ("restart", "STALE_GENERATION"), ("identity", "BROWSER_PROVIDER_NOT_OWNED"),
])
async def test_queued_turn_revalidates_before_claiming_any_screen(host, mutation, code):
    admission = host.admission()
    if mutation == "raw_metadata":
        admission = dict(host.grant)
    elif mutation == "session":
        host.message._session_key = "web:other"
    elif mutation == "run":
        host.message.metadata["run_id"] = "other"
    elif mutation == "disconnect":
        host.current.value = False
    elif mutation == "restart":
        host.runtime.status = lambda: {"phase": "running", "generation": "next"}
    else:
        host.gateway._host_screen_checkpoint_identity.return_value["botId"] = "different"
    with pytest.raises(ScreenError, match=code):
        async with relay_screen_scope(admission, host.message):
            pytest.fail("stale admission entered")
    assert host.authority.snapshot()["runId"] is None
    assert SCREEN_RUN_BINDING.get() is None


async def test_cancelled_relay_turn_preserves_human_control(host):
    entered = asyncio.Event()
    async def run():
        async with relay_screen_scope(host.admission(), host.message):
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 2)
    host.authority.acquire("human", "generation", 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.authority.snapshot()["control"] == "human"
    assert host.authority.snapshot()["runId"] is None
    assert RunHandoffs(host.runtime.directory, host.authority).status()["state"] == "cancelled"


async def test_agent_bus_consumes_capability_and_finishes_actual_checkpoint(host, monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr("flowly.desktop_runtime.installation_gate.agent_environment_activity", nullcontext)
    loop = object.__new__(AgentLoop)
    async def process(message):
        assert METADATA_KEY not in message.metadata
        assert SCREEN_RUN_BINDING.get().run_id == "run"
        return OutboundMessage(channel="web", chat_id="browser", content="Done")
    loop._process_message_in_environment = process
    host.message.metadata[METADATA_KEY] = host.admission()
    response = await loop._process_message(host.message)
    assert response.content == "Done"
    assert RunHandoffs(host.runtime.directory, host.authority).status()["state"] == "finished"
    host.message.metadata[METADATA_KEY] = dict(host.grant)
    response = await loop._process_message(host.message)
    assert response.metadata["error"]["code"] == "BROWSER_ACCESS_NOT_GRANTED"


async def test_web_ingress_rejects_bad_grants_before_ack_or_bus_publish(host):
    bus = MessageBus()
    channel = WebChannel(WebChannelConfig(enabled=True), bus)
    socket = AsyncMock()
    channel._ws = socket
    channel.set_screen_gateway(host.gateway)
    owner = object()
    channel._screen_rpc = SimpleNamespace(clients={"browser": owner}, enabled=True, closed=False)
    request = {"type": "rpc", "id": "request", "method": "chat.send", "sessionId": "browser",
               "params": {"message": "Read page", "sessionKey": "web:original", "idempotencyKey": "run",
                          "browserAccess": {**host.grant, "botId": "foreign"}}}
    await channel._handle_rpc(socket, request)
    assert json.loads(socket.send.call_args.args[0])["error"]["code"] == "BROWSER_PROVIDER_NOT_OWNED"
    assert bus.inbound_size == 0
    request["params"]["browserAccess"] = host.grant
    await channel._handle_rpc(socket, request)
    assert json.loads(socket.send.call_args.args[0])["result"]["runId"] == "run"
    message = await asyncio.wait_for(bus.consume_inbound(), 2)
    admission = message.metadata[METADATA_KEY]
    async with relay_screen_scope(admission, message) as scope:
        assert SCREEN_RUN_BINDING.get().run_id == "run"
        scope.succeeded = True
    # Same textual browser id with a new connection cannot redeem old work.
    request["params"]["idempotencyKey"] = "next-run"
    await channel._handle_rpc(socket, request)
    queued = await asyncio.wait_for(bus.consume_inbound(), 2)
    channel._screen_rpc.clients["browser"] = object()
    with pytest.raises(ScreenError, match="BROWSER_ACCESS_NOT_GRANTED"):
        async with relay_screen_scope(queued.metadata[METADATA_KEY], queued):
            pytest.fail("old owner admitted")


async def test_relay_scope_routes_real_gateway_browser_tool_and_waits_for_explicit_resume(host):
    from flowly.gateway.server import GatewayServer

    async with relay_screen_scope(host.admission(), host.message) as scope:
        assert GatewayServer.has_browser_provider(host.gateway)
        observation = await GatewayServer.send_browser_tool_request(host.gateway, "observe", "read_page", {})
        assert observation["success"]
        checkpoint = SCREEN_RUN_BINDING.get().checkpoint
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
        fresh = await asyncio.wait_for(waiting, 2)
        assert fresh == {"success": True, "text": "fresh page"}
        assert [call.args[0] for call in host.provider._dispatch.await_args_list] == ["read_page", "read_page"]
        scope.succeeded = True
