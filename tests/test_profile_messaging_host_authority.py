"""Profile messaging follows the authenticated host, not the chat application."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

import flowly.profile as profiles
from flowly.agent.loop import AgentLoop
from flowly.agent.tools.message_profile import MessageProfileTool
from flowly.agent.tools.registry import ToolRegistry
from flowly.bus.events import InboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import Config, WebChannelConfig
from flowly.gateway.server import _PROFILE_RUN_BINDING, GatewayServer
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    default = tmp_path / ".flowly"
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    default.mkdir()
    (default / "workspace").mkdir()
    profiles.create_profile("writer", local_runtime=True)
    return default


class Socket:
    closed = False

    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["ios", "web", "desktop"])
async def test_plain_chat_messages_through_host_without_client_directory(profile_home, platform):
    observed = {}

    async def chat(session_key, message, run_id, *args):
        observed.update(args[-1])
        registry = ToolRegistry()
        registry.register(MessageProfileTool(server))
        assert registry.is_available("message_profile", platform=platform)
        reply = await registry.execute(
            "message_profile", {"target_profile": "writer", "message": message},
            platform=platform,
        )
        assert json.loads(reply)["response"] == "Hello from writer"
        return "Writer said hello", {}

    server = GatewayServer(host="127.0.0.1", on_chat_message=chat, enable_profile_host=True)
    host = server.profile_host
    host._broker = AsyncMock(return_value={"ok": True, "response": "Hello from writer"})
    socket = Socket()
    try:
        await server._ws_rpc_chat_send(socket, "phone", "rpc", {
            "sessionKey": f"{platform}:existing-history", "message": "Say hello",
            "idempotencyKey": "user-run",
            # A client hint is not the directory or identity authority.
            "profileDirectory": ["forged"],
            "profileMessageContext": {"sourceProfile": "forged", "correlationId": "fake", "hop": 3},
        })
        await asyncio.gather(*list(server._active_tasks.values()))
        host._broker.assert_awaited_once()
        source, request = host._broker.await_args.args
        assert source == request["sourceProfile"] == "default"
        assert request["sourceSessionKey"] == f"{platform}:existing-history"
        assert request["correlationId"] == "user-run"
        assert request["hop"] == 1
        assert observed["profile_directory"] == ["default", "writer"]
        assert not any(frame.get("type") == "profile_message_request" for frame in socket.frames)
        assert any(frame.get("data", {}).get("state") == "final" for frame in socket.frames)
    finally:
        await server.stop()


def queued_agent(host):
    """Use the real turn boundary, without a provider or background services."""
    agent = object.__new__(AgentLoop)
    agent.set_profile_collaboration_host(host)
    return agent


@pytest.mark.asyncio
async def test_queued_web_turns_get_distinct_authority_at_execution(profile_home):
    host = ProfileHost()
    host._broker = AsyncMock(return_value={"ok": True, "response": "ok"})
    agent = queued_agent(host)
    server = GatewayServer(host="127.0.0.1")
    bindings = []
    both_started = asyncio.Event()

    async def consume(msg):
        binding = _PROFILE_RUN_BINDING.get()
        bindings.append(binding)
        if len(bindings) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        assert _PROFILE_RUN_BINDING.get() is binding
        assert binding.session_key == msg.session_key
        assert msg.metadata["profile_directory"] == ["default", "writer"]
        await server.send_profile_message_request("request-" + msg.chat_id, "writer", "hello")

    agent._process_message_unlocked = consume
    try:
        await asyncio.gather(*(agent._process_message(InboundMessage(
            channel="web", sender_id="phone", chat_id=str(index), content="hello",
            metadata={"run_id": f"run-{index}", "profile_directory": ["forged"]},
        )) for index in range(2)))
        assert host._broker.await_count == 2
        assert {call.args[1]["sourceSessionKey"] for call in host._broker.await_args_list} == {"web:0", "web:1"}
        assert _PROFILE_RUN_BINDING.get() is None
        assert all(not binding.is_active for binding in bindings)
    finally:
        await server.stop()
        await host.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["system", "cron", "heartbeat"])
async def test_background_turn_does_not_gain_user_collaboration_authority(profile_home, channel):
    agent = queued_agent(ProfileHost())

    async def consume(msg):
        assert _PROFILE_RUN_BINDING.get() is None
        assert msg.metadata.get("profile_directory", []) == []

    agent._process_message_unlocked = consume
    await agent._process_message(InboundMessage(
        channel=channel, sender_id="system", chat_id="tick", content="hello",
        metadata={"profile_directory": ["default", "writer"]},
    ))


def request(**overrides):
    return {
        "sourceProfile": "default", "sourceSessionKey": "ios:source",
        "targetProfile": "writer", "message": "hello",
        "correlationId": "same-parent-run", "hop": 1, **overrides,
    }


@pytest.mark.asyncio
async def test_separate_messages_in_one_parent_run_have_distinct_target_run_ids(profile_home):
    host = ProfileHost()
    run_ids = []

    async def target_rpc(target, method, params, timeout):
        run_id = params["idempotencyKey"]
        run_ids.append(run_id)
        await host._handle_profile_event(target, "chat", {
            "runId": run_id, "sessionKey": params["sessionKey"],
            "state": "final", "message": {"content": "reply"},
        })
        return {"runId": run_id}

    host._target_rpc = target_rpc
    await host._broker("default", request(requestId="first-message"))
    await host._broker("default", request(requestId="second-message"))
    assert len(set(run_ids)) == 2


@pytest.mark.asyncio
async def test_buffered_failure_is_not_returned_as_a_successful_reply(profile_home):
    host = ProfileHost()

    async def target_rpc(target, method, params, timeout):
        run_id = params["idempotencyKey"]
        await host._handle_profile_event(target, "chat", {
            "runId": run_id, "sessionKey": params["sessionKey"],
            "state": "error", "message": {"content": "unfinished reply"},
        })
        return {"runId": run_id}

    host._target_rpc = target_rpc
    with pytest.raises(ProfileHostError) as error:
        await host._broker("default", request())
    assert error.value.code == "PROFILE_COLLABORATION_FAILED"
    assert not host._broker_waiters
    assert not host._broker_sessions


@pytest.mark.asyncio
async def test_cancelling_parent_aborts_accepted_target_and_cleans_waiters(profile_home):
    host = ProfileHost()
    sent = asyncio.Event()

    async def target_rpc(target, method, params, timeout):
        if method == "chat.send":
            sent.set()
            return {"runId": "accepted-target-run"}
        return {"aborted": True}

    host._target_rpc = AsyncMock(side_effect=target_rpc)
    task = asyncio.create_task(host._broker("default", request()))
    await sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    host._target_rpc.assert_awaited_with("writer", "chat.abort", {"runId": "accepted-target-run"}, 30)
    assert not host._broker_waiters
    assert not host._broker_sessions


@pytest.mark.asyncio
async def test_nested_request_does_not_deadlock_on_busy_target(profile_home):
    host = ProfileHost()
    lock = asyncio.Lock()
    host._broker_target_locks["writer"] = lock
    async with lock:
        with pytest.raises(ProfileHostError) as error:
            await asyncio.wait_for(host._broker("default", request(hop=2)), 0.1)
    assert error.value.code == "PROFILE_COLLABORATION_BUSY"


@pytest.mark.asyncio
async def test_completed_turn_cannot_message_from_an_inherited_child_context(profile_home):
    host = ProfileHost()
    host._broker = AsyncMock()
    server = GatewayServer(host="127.0.0.1")
    binding = host.collaboration_binding("default", "ios:one", "run")
    token = _PROFILE_RUN_BINDING.set(binding)
    gate = asyncio.Event()

    async def delayed():
        await gate.wait()
        return await server.send_profile_message_request("late", "writer", "hello")

    task = asyncio.create_task(delayed())
    binding.close()
    _PROFILE_RUN_BINDING.reset(token)
    gate.set()
    try:
        result = await task
        assert result["error_code"] == "PROFILE_BROKER_UNAVAILABLE"
        host._broker.assert_not_awaited()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_source_run_terminal_cancels_nested_request_but_not_another_run(profile_home):
    host = ProfileHost()
    started = asyncio.Event()

    async def pending(*args):
        started.set()
        await asyncio.Event().wait()

    host._broker = AsyncMock(side_effect=pending)
    source = SimpleNamespace(profile="writer", ws=Socket())
    task = asyncio.create_task(host._handle_broker_request(source, {
        "id": "nested-request", "params": request(
            sourceProfile="writer", sourceRunId="new-run", targetProfile="default",
        ),
    }))
    await started.wait()
    try:
        await host._handle_profile_event("writer", "chat", {
            "sessionKey": "ios:source", "runId": "old-run", "state": "final",
        })
        await asyncio.sleep(0)
        assert not task.done()
        await host._handle_profile_event("writer", "chat", {
            "sessionKey": "ios:source", "runId": "new-run", "state": "aborted",
        })
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.2)
        assert not host._profile_message_requests
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class DelegatingProvider(LLMProvider):
    """A deterministic model double; routing and tool execution remain real."""

    def __init__(self):
        super().__init__(api_key="test")
        self.surfaces = []
        self.results = []

    def get_default_model(self):
        return "test/model"

    async def chat(self, messages, tools=None, **kwargs):
        if not tools:
            return LLMResponse(content="Conversation")
        self.surfaces.append({tool["function"]["name"] for tool in tools})
        results = [row for row in messages if row.get("role") == "tool"]
        if results:
            self.results.append(results[-1]["content"])
            return LLMResponse(content="Writer replied: hello")
        return LLMResponse(content=None, tool_calls=[ToolCallRequest(
            id="consult-writer", name="message_profile",
            arguments={"target_profile": "writer", "message": "hello"},
        )])


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["direct", "relay"])
async def test_real_agent_loop_from_mobile_wire_to_host_tool_and_reply(profile_home, monkeypatch, transport):
    bus = MessageBus()
    provider = DelegatingProvider()
    config = Config()
    config.tools.routing.discovery.enabled = False
    agent = AgentLoop(bus=bus, provider=provider, workspace=profile_home / "workspace",
                      main_config=config, max_iterations=3, soft_warn_at_iteration=0)
    monkeypatch.setattr(agent, "_schedule_post_turn_compaction", lambda msg: None)

    async def chat(session, message, run_id, stream, media, voice, iteration, render, metadata):
        return await agent.process_direct(
            message, session_key=session, run_id=run_id, stream_callback=stream,
            on_iteration=iteration, extra_metadata=metadata, return_metadata=True,
            skip_memory=True, skip_context_files=True,
        )

    server = GatewayServer(host="127.0.0.1", port=0, auth_token="test-token",
                           require_loopback_auth=True, advertise_control=False,
                           enable_profile_host=True, on_chat_message=chat, sessions=agent.sessions)
    host = server.profile_host
    runtime = SimpleNamespace(active_runs=set())
    host._ensure_runtime = AsyncMock(return_value=runtime)

    async def child_rpc(active_runtime, method, params, timeout):
        assert active_runtime is runtime
        assert method == "chat.send"
        assert params["allowedTools"] and "exec" not in params["allowedTools"]
        run_id = params["idempotencyKey"]
        # Exercise the real broker and final-event handling, including a
        # reply arriving before the coroutine awaiting its ACK resumes.
        await host._handle_profile_event("writer", "chat", {
            "runId": run_id, "sessionKey": params["sessionKey"], "state": "final",
            "message": {"content": "hello from writer"},
        })
        return {"runId": run_id}

    host._rpc = AsyncMock(side_effect=child_rpc)
    agent.set_profile_collaboration_host(host)
    agent.tools.register(MessageProfileTool(server))
    try:
        if transport == "direct":
            await server.start()
            async with aiohttp.ClientSession() as client:
                base = f"http://127.0.0.1:{server.port}"
                async with client.post(base + "/api/auth/ws-ticket", headers={"Authorization": "Bearer test-token"}) as response:
                    assert response.status == 200
                    ticket = (await response.json())["ticket"]
                async with client.ws_connect(base + "/ws?ticket=" + ticket) as socket:
                    await socket.send_json({"type": "rpc", "id": "send", "method": "chat.send", "params": {
                        "sessionKey": "ios:unchanged", "message": "Ask writer to say hello",
                        "idempotencyKey": "phone-run",
                    }})
                    while True:
                        event = await socket.receive_json(timeout=10)
                        if event.get("event") == "chat" and event.get("data", {}).get("state") in {"final", "error"}:
                            assert event["data"]["state"] == "final", event
                            assert event["data"]["sessionKey"] == "ios:unchanged"
                            break
        else:
            channel = WebChannel(config=WebChannelConfig(enabled=True), bus=bus)
            channel.set_profile_host(host)
            channel._send_or_queue = AsyncMock()

            class RelaySocket:
                send = AsyncMock()

            await channel._handle_rpc(RelaySocket(), {
                "type": "rpc", "id": "send", "method": "chat.send", "sessionId": "phone",
                "params": {"sessionKey": "web:unchanged", "message": "Ask writer to say hello", "idempotencyKey": "phone-run"},
            })
            incoming = await asyncio.wait_for(bus.consume_inbound(), 2)
            assert incoming.session_key == "web:unchanged"
            result = await agent._process_message(incoming)
            assert result.content == "Writer replied: hello"
        assert "message_profile" in provider.surfaces[0]
        assert "hello from writer" in provider.results[0]
        host._ensure_runtime.assert_awaited_once_with("writer")
        host._rpc.assert_awaited_once()
        assert host._rpc.await_args.args[2]["profileMessageContext"]["sourceProfile"] == "default"
        assert not runtime.active_runs
        assert _PROFILE_RUN_BINDING.get() is None
    finally:
        agent.stop()
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing-host", "denied-policy"])
async def test_model_cannot_call_profile_tool_without_run_authority_and_permission(profile_home, monkeypatch, mode):
    provider = DelegatingProvider()
    config = Config()
    config.tools.routing.discovery.enabled = False
    if mode == "denied-policy":
        config.tools.routing.disabled_toolsets = ["delegation"]
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=profile_home / "workspace",
                      main_config=config, max_iterations=3, soft_warn_at_iteration=0)
    monkeypatch.setattr(agent, "_schedule_post_turn_compaction", lambda msg: None)
    host = ProfileHost()
    host._broker = AsyncMock()
    agent.set_profile_collaboration_host(host if mode == "denied-policy" else None)
    server = GatewayServer(host="127.0.0.1")
    agent.tools.register(MessageProfileTool(server))
    try:
        await agent._process_message(InboundMessage(
            channel="web", sender_id="phone", chat_id="thread", content="Ask writer to say hello",
            metadata={"profile_directory": ["default", "writer"], "run_id": "run"},
        ))
        assert "message_profile" not in provider.surfaces[0]
        assert "Error" in provider.results[0]
        host._broker.assert_not_awaited()
    finally:
        agent.stop()
        await server.stop()
        await host.shutdown()
