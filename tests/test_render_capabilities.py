"""Per-turn rich-rendering capability negotiation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from flowly.agent import inflight
from flowly.agent.loop import AgentLoop
from flowly.agent.prompt_blocks import build_render_capability_hint
from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer
from flowly.render_capabilities import normalize_render_capabilities


class _FakeGatewayWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


def test_normalizer_allowlists_deduplicates_and_preserves_order() -> None:
    assert normalize_render_capabilities(
        [" MERMAID ", "unknown", "mermaid", 42, None]
    ) == ("mermaid",)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "mermaid",
        {"mermaid": True},
        [""],
        ["x" * 65],
        ["unknown"],
    ],
)
def test_normalizer_fails_closed_for_malformed_or_unknown_values(value: Any) -> None:
    assert normalize_render_capabilities(value) == ()


def test_mermaid_hint_is_opt_in_and_security_constrained() -> None:
    assert build_render_capability_hint(None) == ""
    assert build_render_capability_hint(["unknown"]) == ""

    hint = build_render_capability_hint(["mermaid"])
    assert "Client rendering capability — Mermaid" in hint
    assert "fenced ``mermaid`` code block" in hint
    assert "Do not emit Mermaid init" in hint
    assert "click" in hint
    assert "raw HTML" in hint
    assert "Do not force a diagram" in hint


def test_loop_injects_hint_after_cached_system_prompt() -> None:
    messages = [
        {"role": "system", "content": "stable cached prompt"},
        {"role": "user", "content": "draw the flow"},
    ]

    AgentLoop._inject_render_capability_hint(
        messages, ["mermaid"], voice_mode=False
    )

    assert messages[0]["content"] == "stable cached prompt"
    assert messages[1]["role"] == "system"
    assert "Mermaid" in messages[1]["content"]
    assert messages[2]["role"] == "user"


def test_loop_suppresses_hint_for_voice_and_unadvertised_clients() -> None:
    voice_messages = [{"role": "system", "content": "stable"}]
    AgentLoop._inject_render_capability_hint(
        voice_messages, ["mermaid"], voice_mode=True
    )
    assert voice_messages == [{"role": "system", "content": "stable"}]

    terminal_messages = [{"role": "system", "content": "stable"}]
    AgentLoop._inject_render_capability_hint(
        terminal_messages, None, voice_mode=False
    )
    assert terminal_messages == [{"role": "system", "content": "stable"}]


@pytest.mark.asyncio
async def test_process_direct_normalizes_capabilities_into_inbound_metadata() -> None:
    loop = object.__new__(AgentLoop)
    observed: dict[str, Any] = {}

    async def process_message(msg):
        observed["message"] = msg
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="done",
        )

    loop._process_message = process_message
    response = await loop.process_direct(
        "draw it",
        session_key="desktop:capabilities",
        render_capabilities=[" MERMAID ", "unknown", "mermaid"],
    )

    assert response == "done"
    assert observed["message"].metadata["render_capabilities"] == ("mermaid",)


@pytest.mark.asyncio
async def test_direct_gateway_propagates_normalized_capabilities() -> None:
    observed: dict[str, Any] = {}
    callback_called = asyncio.Event()

    async def on_chat_message(
        session_key: str,
        message: str,
        run_id: str,
        stream_callback,
        media: list[str],
        voice_mode: bool,
        iteration_callback,
        render_capabilities: tuple[str, ...],
    ) -> tuple[str, dict]:
        observed.update(
            session_key=session_key,
            message=message,
            run_id=run_id,
            media=media,
            voice_mode=voice_mode,
            render_capabilities=render_capabilities,
        )
        callback_called.set()
        return "done", {}

    server = GatewayServer(
        host="127.0.0.1",
        port=0,
        on_chat_message=on_chat_message,
    )
    ws = _FakeGatewayWebSocket()
    inflight._runs.clear()

    await server._ws_rpc_chat_send(
        ws,  # type: ignore[arg-type]
        "desktop-client",
        "rpc-1",
        {
            "sessionKey": "desktop:capabilities",
            "message": "draw it",
            "idempotencyKey": "run-1",
            "renderCapabilities": [" MERMAID ", "unknown", "mermaid"],
        },
    )
    active_tasks = list(server._active_tasks.values())
    await asyncio.wait_for(callback_called.wait(), timeout=1)
    await asyncio.gather(*active_tasks)

    assert observed["render_capabilities"] == ("mermaid",)
    assert ws.sent[0]["result"] == {"runId": "run-1", "status": "accepted"}


@pytest.mark.asyncio
async def test_relay_metadata_carries_capability_only_when_advertised() -> None:
    bus = MessageBus()
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=bus)

    await channel._process_message(
        "relay-session",
        "web:conversation",
        "draw it",
        "run-1",
        render_capabilities=("mermaid",),
    )
    advertised = await bus.consume_inbound()
    assert advertised.metadata["render_capabilities"] == ("mermaid",)

    await channel._process_message(
        "relay-session",
        "web:conversation",
        "plain text",
        "run-2",
    )
    unadvertised = await bus.consume_inbound()
    assert "render_capabilities" not in unadvertised.metadata
