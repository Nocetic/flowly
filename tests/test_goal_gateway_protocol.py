from __future__ import annotations

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
