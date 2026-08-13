from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from flowly.agent import inflight
from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer
from flowly.goals.models import GoalState


@pytest.fixture(autouse=True)
def _reset_goal_provider():
    feature_rpc.set_goal_state_provider(None)
    yield
    feature_rpc.set_goal_state_provider(None)


@pytest.mark.asyncio
async def test_goal_get_and_chat_inflight_share_the_same_durable_snapshot() -> None:
    state = GoalState(session_key="web:chat", goal="ship", max_turns=12)
    feature_rpc.set_goal_state_provider(
        lambda session_key: state if session_key == "web:chat" else None
    )

    result, restart = await feature_rpc.dispatch("goal.get", {"sessionKey": "web:chat"})
    resume, _ = await feature_rpc.dispatch("chat.inflight", {"sessionKey": "web:chat"})

    assert restart is False
    assert result["goal"]["goalId"] == state.goal_id
    assert result["goal"]["status"] == "active"
    assert resume["goal"] == result["goal"]
    assert "goal.get" in feature_rpc.FEATURE_METHODS


@pytest.mark.asyncio
async def test_goal_get_requires_a_session_key() -> None:
    with pytest.raises(feature_rpc.FeatureRpcError) as exc_info:
        await feature_rpc.dispatch("goal.get", {})
    assert exc_info.value.code == "INVALID_PARAMS"


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_direct_gateway_final_precedes_goal_event_and_delivery_callback() -> None:
    order: list[str] = []
    ws = _FakeWS()
    state = GoalState(session_key="desktop:chat", goal="ship")

    async def on_chat_message(*_args: Any) -> tuple[str, dict[str, Any]]:
        return "goal accepted", {
            "goal": state.to_public_dict(),
            "_goal_kickoff_goal_id": state.goal_id,
        }

    def delivered(_session_key: str, _text: str, _metadata: dict[str, Any]) -> None:
        order.append(f"callback-after-{len(ws.sent)}")

    server = GatewayServer(
        host="127.0.0.1",
        port=0,
        on_chat_message=on_chat_message,
        on_chat_delivered=delivered,
    )
    server._schedule_offline_chat_push = lambda *args, **kwargs: None
    inflight._runs.clear()

    await server._run_chat(
        ws,
        "client",
        "desktop:chat",
        "/goal ship",
        "run-1",
        stream_callback=AsyncMock(),
    )

    assert [event["event"] for event in ws.sent] == ["chat", "goal.updated"]
    assert ws.sent[0]["data"]["goal"]["goalId"] == state.goal_id
    assert ws.sent[1]["data"] == {
        "sessionKey": "desktop:chat",
        "goal": state.to_public_dict(),
    }
    assert order == ["callback-after-2"]


@pytest.mark.asyncio
async def test_proactive_goal_status_keeps_old_final_and_adds_scoped_event() -> None:
    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=AsyncMock())
    ws = _FakeWS()
    server._ws_clients = {"client": ws}
    state = GoalState(session_key="desktop:chat", goal="ship")

    await server.push_session_message(
        "desktop:chat",
        "↻ Continuing",
        metadata={"goal": state.to_public_dict()},
    )

    assert [event["event"] for event in ws.sent] == ["chat", "goal.updated"]
    assert ws.sent[0]["data"]["proactive"] is True


@pytest.mark.asyncio
async def test_proactive_goal_provider_failure_uses_native_error_contract() -> None:
    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=AsyncMock())
    ws = _FakeWS()
    server._ws_clients = {"client": ws}

    await server.push_session_message(
        "desktop:chat",
        "request too large",
        metadata={
            "model": "test/model",
            "error": {
                "code": "MODEL_INPUT_LIMIT_EXCEEDED",
                "title": "This request is too large",
                "message": "Compact the conversation, then try again.",
                "retryable": False,
            },
        },
    )

    assert len(ws.sent) == 1
    assert ws.sent[0]["event"] == "chat"
    assert ws.sent[0]["data"]["state"] == "error"
    assert ws.sent[0]["data"]["proactive"] is True
    assert ws.sent[0]["data"]["errorCode"] == "MODEL_INPUT_LIMIT_EXCEEDED"
    assert "message" not in ws.sent[0]["data"]


@pytest.mark.asyncio
async def test_direct_autonomous_turn_reuses_the_client_chat_runner() -> None:
    """An agent-authored turn must run through _run_chat, not a second path."""
    seen: dict[str, Any] = {}

    async def on_chat_message(session_key, message, run_id, stream_cb, *args):
        seen["session_key"] = session_key
        seen["run_id"] = run_id
        seen["extra"] = args[-1]
        # The agent announces the resolved prompt from inside its guards.
        await args[-1]["on_user_message"]("keep going")
        await stream_cb("wor")
        await stream_cb("king")
        return "done", {}

    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=on_chat_message)
    ws = _FakeWS()
    server._session_ws["desktop:chat"] = ws

    await server.run_autonomous_turn(
        "desktop:chat", {"_goal_continuation_goal_id": "g1", "goal_run": True},
    )

    # One run identity for the user row, the deltas and the terminal.
    assert not seen["run_id"].startswith("goal-"), "autonomous runs use normal ids"
    assert seen["extra"]["_goal_continuation_goal_id"] == "g1"
    kinds = [(e.get("event"), e["data"].get("state") or e["data"].get("stream")) for e in ws.sent]
    assert ("chat", "user") in kinds, "the agent-authored prompt is announced"
    assert ("agent", "assistant") in kinds, "deltas stream on the normal path"
    assert ("chat", "final") in kinds, "the turn settles with a normal final"
    run_ids = {
        e["data"].get("runId")
        for e in ws.sent
        if e["data"].get("runId")
    }
    assert run_ids == {seen["run_id"]}


@pytest.mark.asyncio
async def test_direct_autonomous_turn_without_a_bound_socket_is_a_no_op() -> None:
    called = AsyncMock()
    server = GatewayServer(host="127.0.0.1", port=0, on_chat_message=called)

    await server.run_autonomous_turn("desktop:gone", {"goal_run": True})

    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_relay_final_is_followed_by_conversation_scoped_goal_event() -> None:
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    payloads: list[dict[str, Any]] = []

    async def capture(payload: str) -> None:
        payloads.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel._session_key_to_relay_id["web:stable-chat"] = "relay-1"
    state = GoalState(session_key="web:stable-chat", goal="ship")

    await channel.send(
        OutboundMessage(
            channel="web",
            chat_id="relay-1",
            content="goal accepted",
            metadata={"goal": state.to_public_dict()},
        )
    )

    assert [payload["event"] for payload in payloads] == ["chat", "goal.updated"]
    assert payloads[1]["data"]["sessionKey"] == "web:stable-chat"
    assert payloads[1]["data"]["goal"]["goalId"] == state.goal_id


@pytest.mark.asyncio
async def test_relay_autonomous_turn_streams_through_the_shared_callback() -> None:
    """The relay's goal turn uses chat.send's own streaming implementation."""
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    payloads: list[dict[str, Any]] = []
    local: list[tuple[str, dict[str, Any]]] = []

    async def capture(payload: str) -> None:
        payloads.append(json.loads(payload))

    async def capture_local(name: str, data: dict[str, Any]) -> None:
        local.append((name, data))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel._emit_local_event = capture_local  # type: ignore[method-assign]
    channel._session_key_to_relay_id["web:stable-chat"] = "relay-1"

    callback = channel._make_stream_callback("relay-1", "web:stable-chat", "run-1")
    await callback("working")
    await asyncio.sleep(0)

    assert payloads == [{
        "type": "event",
        "sessionId": "relay-1",
        "event": "chat",
        "data": {
            "state": "streaming",
            "runId": "run-1",
            "sessionKey": "web:stable-chat",
            "delta": "working",
        },
    }]
    # The embedded desktop bot listens on the local ``agent`` event; a goal
    # turn that skipped it showed no live text at all.
    assert [name for name, _ in local] == ["chat", "agent"]


@pytest.mark.asyncio
async def test_relay_autonomous_turn_announces_the_agent_authored_prompt() -> None:
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    payloads: list[dict[str, Any]] = []
    published: list[Any] = []

    async def capture(payload: str) -> None:
        payloads.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel._emit_local_event = AsyncMock()  # type: ignore[method-assign]
    channel.bus.publish_inbound = lambda msg: published.append(msg)  # type: ignore[assignment]
    channel._session_key_to_relay_id["web:stable-chat"] = "relay-1"

    await channel.run_autonomous_turn(
        "web:stable-chat", {"_goal_continuation_goal_id": "g1", "goal_run": True},
    )
    await asyncio.sleep(0)

    # The turn is published as an ordinary inbound with the goal markers…
    assert len(published) == 1
    inbound = published[0]
    assert inbound.session_key == "web:stable-chat"
    assert inbound.metadata["_goal_continuation_goal_id"] == "g1"
    # …and the announce hook publishes the user row when the agent resolves it.
    await inbound.metadata["on_user_message"]("keep going")
    user_events = [p for p in payloads if p["data"].get("state") == "user"]
    assert len(user_events) == 1
    assert user_events[0]["data"]["message"]["content"][0]["text"] == "keep going"
    assert user_events[0]["data"]["sessionKey"] == "web:stable-chat"
