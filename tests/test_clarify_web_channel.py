"""Relay delivery of clarify: send_clarify_event emits an
``agent.clarify.requested`` frame to the mapped relay session, and
``agent.clarify.resolve`` over the relay completes the manager's Future.

Mirrors the exec-approval relay path so iOS/browser/cloud-connected
desktop clients receive clarify questions the same way they receive
approval requests.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.clarify.manager import ClarifyManager
from flowly.config.schema import WebChannelConfig


@pytest.fixture(autouse=True)
def _fresh_manager(monkeypatch):
    import flowly.clarify.manager as mod
    monkeypatch.setattr(mod, "_manager", ClarifyManager())


@pytest.fixture
def channel():
    ch = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    capture: list[dict] = []

    async def fake_send_or_queue(payload: str) -> None:
        capture.append(json.loads(payload))

    ch._send_or_queue = fake_send_or_queue  # type: ignore[method-assign]
    ch._capture = capture
    # Pretend a relay WS is connected and the session is mapped.
    ch._ws = object()  # truthy sentinel; _send_or_queue is stubbed
    ch._session_key_to_relay_id["web:abc"] = "relay-uuid-1"
    return ch


def _request(cid: str, question: str, choices, expires_at: float):
    from flowly.clarify.types import ClarifyRequest

    return ClarifyRequest(
        id=cid, question=question, choices=choices,
        session_key="web:abc", created_at=0.0, expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_send_clarify_event_shape(channel):
    await channel.send_clarify_event(
        "web:abc", _request("cid1", "Which environment?", ["staging", "prod"], 1234.0),
    )
    assert len(channel._capture) == 1
    frame = channel._capture[0]
    assert frame["event"] == "agent.clarify.requested"
    assert frame["sessionId"] == "relay-uuid-1"
    assert frame["data"]["id"] == "cid1"
    assert frame["data"]["choices"] == ["staging", "prod"]
    assert frame["data"]["question"] == "Which environment?"
    # Additive: the relay path now carries the same sessionKey the gateway
    # broadcast always did, so a surface can tell whether a question belongs to
    # the conversation it is showing.
    assert frame["data"]["sessionKey"] == "web:abc"


@pytest.mark.asyncio
async def test_send_clarify_event_open_ended(channel):
    await channel.send_clarify_event("web:abc", _request("cid2", "Say what?", None, 99.0))
    assert channel._capture[0]["data"]["choices"] is None


@pytest.mark.asyncio
async def test_send_clarify_event_unmapped_session_is_noop(channel):
    await channel.send_clarify_event("web:ghost", _request("cid3", "Q?", None, 1.0))
    assert channel._capture == []


@pytest.mark.asyncio
async def test_send_clarify_closed_reaches_relay(channel):
    """The gateway has broadcast ``closed`` for a while; the relay path never
    did, so a question answered on the phone left a dead prompt on every other
    relay-connected surface."""
    await channel.send_clarify_closed("web:abc", "cid5", "answered")
    assert channel._capture == [
        {
            "type": "event",
            "sessionId": "relay-uuid-1",
            "event": "agent.clarify.closed",
            "data": {"id": "cid5", "reason": "answered", "sessionKey": "web:abc"},
        }
    ]


@pytest.mark.asyncio
async def test_send_clarify_closed_unmapped_session_is_noop(channel):
    await channel.send_clarify_closed("web:ghost", "cid6", "timeout")
    assert channel._capture == []


@pytest.mark.asyncio
async def test_relay_resolve_completes_future(channel):
    """An agent.clarify.resolve RPC arriving over the relay resolves the
    pending Future the agent is awaiting."""
    from flowly.clarify.manager import get_clarify_manager
    from flowly.clarify.types import ClarifyRequest
    import asyncio
    import time

    mgr = get_clarify_manager()
    now = time.time()
    pending = ClarifyRequest(
        id="cid4", question="Q?", choices=None,
        session_key="web:abc", created_at=now, expires_at=now + 5,
    )

    # Capture the ack the channel sends back.
    sent: list[dict] = []
    ws = AsyncMock()

    async def send(payload):
        sent.append(json.loads(payload))

    ws.send = send

    async def resolve_via_relay():
        await asyncio.sleep(0.01)
        await channel._handle_rpc(ws, {
            "method": "agent.clarify.resolve",
            "id": "rpc-1",
            "params": {"id": "cid4", "answer": "do it"},
            "sessionId": "relay-uuid-1",
        })

    asyncio.create_task(resolve_via_relay())
    answer = await mgr.request_and_wait(pending)
    assert answer == "do it"
    assert sent and sent[-1]["result"]["ok"] is True
