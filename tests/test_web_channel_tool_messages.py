"""Tests for the web channel emitting tool_messages on the final event.

The relay reads ``data.toolMessages`` to decide whether to write each
assistant_with_tool_calls / tool_result entry to a separate
``tool_turns/`` Firestore subcollection. The OutboundMessage carries
the list in its metadata; this file pins:

  * Non-empty tool_messages → field appears in the WS payload.
  * Empty tool_messages OR missing key → field is OMITTED so old
    relays don't see a wire change. Critical for backward compat:
    a relay that doesn't know about toolMessages must see identical
    payload bytes to today.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig


@pytest.fixture
def channel():
    """Build a WebChannel with a stubbed _send_or_queue so we can
    capture exactly what would be put on the wire without touching
    a real WebSocket."""
    ch = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    # Stub the transport — every call now lands in capture[] as the
    # JSON string the relay would have received.
    capture: list[dict] = []

    async def fake_send_or_queue(payload: str) -> None:
        capture.append(json.loads(payload))

    ch._send_or_queue = fake_send_or_queue  # type: ignore[method-assign]
    ch._capture = capture  # for test access
    return ch


@pytest.mark.asyncio
async def test_tool_messages_included_when_non_empty(channel) -> None:
    """Tool-using turn: relay must receive the structured tool turn
    entries so it can persist them to tool_turns/."""
    msg = OutboundMessage(
        channel="web",
        chat_id="sess-1",
        content="Done.",
        metadata={
            "run_id": "run-abc",
            "tool_messages": [
                {
                    "role": "assistant",
                    "content": "Searching.",
                    "tool_calls": [{
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"q":"x"}'},
                    }],
                },
                {
                    "role": "tool",
                    "content": "results",
                    "tool_call_id": "c1",
                    "name": "web_search",
                },
            ],
        },
    )
    await channel.send(msg)

    assert len(channel._capture) == 1
    event = channel._capture[0]
    data = event["data"]

    # Existing fields still where they were.
    assert data["state"] == "final"
    assert data["runId"] == "run-abc"

    # NEW: toolMessages array surfaced verbatim for the relay.
    assert "toolMessages" in data
    tool_msgs = data["toolMessages"]
    assert len(tool_msgs) == 2

    # First entry: assistant with tool_calls.
    assert tool_msgs[0]["role"] == "assistant"
    assert tool_msgs[0]["tool_calls"][0]["id"] == "c1"
    assert tool_msgs[0]["content"] == "Searching."

    # Second entry: tool result with linkage fields.
    assert tool_msgs[1]["role"] == "tool"
    assert tool_msgs[1]["tool_call_id"] == "c1"
    assert tool_msgs[1]["name"] == "web_search"
    assert tool_msgs[1]["content"] == "results"


@pytest.mark.asyncio
async def test_tool_messages_absent_when_empty(channel) -> None:
    """No tool work this turn → no toolMessages field on the wire.

    This is the backward-compat guarantee: a relay that doesn't yet
    know about the field sees a payload byte-identical to what it
    received before this change. Without this, old relays would log
    "unknown field" warnings or — worse — crash on a strict parser."""
    msg = OutboundMessage(
        channel="web",
        chat_id="sess-1",
        content="Just text.",
        metadata={
            "run_id": "run-xyz",
            "tool_messages": [],  # empty — turn was tool-free
        },
    )
    await channel.send(msg)

    event = channel._capture[0]
    data = event["data"]
    assert "toolMessages" not in data, (
        "Empty tool_messages must be dropped from the wire payload — "
        "old relays / old desktops require unchanged bytes."
    )


@pytest.mark.asyncio
async def test_tool_messages_absent_when_metadata_key_missing(channel) -> None:
    """Pre-Phase-3 agents won't set tool_messages at all. Channel must
    handle the missing key gracefully — no KeyError, no field on wire."""
    msg = OutboundMessage(
        channel="web",
        chat_id="sess-1",
        content="Old-style turn.",
        metadata={"run_id": "run-old"},
    )
    await channel.send(msg)

    event = channel._capture[0]
    data = event["data"]
    assert "toolMessages" not in data


@pytest.mark.asyncio
async def test_approval_event_payload_shape_unchanged(channel) -> None:
    """iOS/browser approval events keep their existing wire contract."""
    channel._ws = object()
    channel._session_key_to_relay_id["web:session-1"] = "relay-1"

    await channel.send_approval_event(
        "session-1",
        "approval-1",
        "echo hello",
        123.0,
    )

    # Additive field `supportsAlways` (default True) — old relays ignore
    # unknown keys; clients use it to hide a no-op "Always allow".
    assert channel._capture == [
        {
            "type": "event",
            "sessionId": "relay-1",
            "event": "exec.approval.requested",
            "data": {
                "id": "approval-1",
                "command": "echo hello",
                "expiresAt": 123.0,
                "supportsAlways": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_approval_event_marks_non_persistable(channel) -> None:
    channel._ws = object()
    channel._session_key_to_relay_id["web:session-1"] = "relay-1"

    await channel.send_approval_event(
        "session-1", "approval-1", "📧 Send email", 123.0, supports_always=False
    )

    assert channel._capture[0]["data"]["supportsAlways"] is False
