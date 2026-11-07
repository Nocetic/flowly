"""Every piece of a run goes on the wire with its ``seq`` (flowly-internal/core/docs/chat-wire-protocol.md §8).

A client shows the ``chat.inflight`` snapshot, then adds only pieces with a
higher ``seq``: none twice (the Android "every chunk twice" class of bug),
none skipped. The registry assigns the numbers (agent/inflight.py); these
tests pin that each transport sends them.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.agent import inflight
from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer


class Socket:
    closed = False

    def __init__(self):
        self.sent = []

    async def send_json(self, frame):
        self.sent.append(frame)

    async def send(self, frame):
        self.sent.append(json.loads(frame))


@pytest.fixture(autouse=True)
def _clear_registry():
    inflight._runs.clear()
    yield
    inflight._runs.clear()


@pytest.mark.asyncio
async def test_direct_gateway_sends_each_delta_and_step_with_its_seq():
    async def run(*args):
        stream, on_iteration = args[3], args[6]
        await stream("Hel")
        await on_iteration({"role": "assistant", "iterationIdx": 0, "content": "", "tool_calls": []})
        await stream("lo")
        snapshot = inflight.get("desktop:seq")
        assert snapshot["seq"] == 3, "the snapshot names the last piece it holds"
        return "Hello", {}

    server = GatewayServer(on_chat_message=run)
    ws = Socket()
    await server._ws_rpc_chat_send(ws, "client", "rpc-1", {"sessionKey": "desktop:seq", "idempotencyKey": "r1", "message": "hi"})
    await asyncio.gather(*list(server._active_tasks.values()))

    pieces = [
        (frame["event"], frame["data"].get("data", {}).get("text") or frame["data"].get("state"), frame["data"].get("seq"))
        for frame in ws.sent
        if frame.get("type") == "event" and (frame["event"] == "agent" or frame["data"].get("state") == "iteration_step")
    ]
    assert pieces == [("agent", "Hel", 1), ("chat", "iteration_step", 2), ("agent", "lo", 3)]


@pytest.mark.asyncio
async def test_web_channel_sends_a_steps_seq(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    sent: list[dict] = []

    async def capture(payload: str) -> None:
        sent.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel._session_key_to_relay_id["web:c"] = "relay-1"
    await channel.send(OutboundMessage(channel="web", chat_id="relay-1", content="", metadata={
        "session_key": "web:c",
        "iteration_event": {"runId": "r1", "iterationIdx": 0, "role": "assistant", "content": "", "tool_calls": [], "seq": 7},
    }))
    assert sent[0]["data"]["state"] == "iteration_step"
    assert sent[0]["data"]["seq"] == 7


@pytest.mark.asyncio
async def test_a_piece_that_was_not_recorded_goes_without_seq(tmp_path, monkeypatch):
    # §6.3: an omitted field reproduces the old wire shape exactly.
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    sent: list[dict] = []

    async def capture(payload: str) -> None:
        sent.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    await channel.send(OutboundMessage(channel="web", chat_id="relay-1", content="", metadata={
        "session_key": "web:c",
        "iteration_event": {"runId": "r1", "iterationIdx": 0, "role": "assistant", "content": "", "tool_calls": []},
    }))
    assert "seq" not in sent[0]["data"]


@pytest.mark.asyncio
async def test_a_message_the_run_took_in_reaches_the_server_as_joined(tmp_path, monkeypatch):
    # run_steering: every client binds the message to the running reply.
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    sent: list[dict] = []

    async def capture(payload: str) -> None:
        sent.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    await channel.send(OutboundMessage(channel="web", chat_id="relay-1", content="", metadata={
        "session_key": "web:c",
        "iteration_event": {"runId": "r1", "iterationIdx": 2, "role": "user", "content": "stop",
                            "turnSteer": True, "messageId": "m2"},
    }))
    assert sent[0]["data"] | {"source": None} == {
        "state": "iteration_step", "runId": "r1", "sessionKey": "web:c", "source": None, "iterationIdx": 2,
        "role": "user", "content": "stop", "turnSteer": True, "messageId": "m2",
    }
