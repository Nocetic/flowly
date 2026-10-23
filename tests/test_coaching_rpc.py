"""Meeting Coach over the relay and the Flowly server channel (flowly/coaching/rpc.py).

The new apps reach every agent, this Mac's included, through the Flowly server,
which hands each RPC to ``WebChannel`` with the asking device's client session.
Before this, ``coaching.*`` existed only on the direct gateway and the channel
answered ``METHOD_NOT_FOUND``. These tests pin that the channel serves them with
the gateway's params and replies, that events reach the device that started the
session, and that a session outlives a short drop but not a device that is gone.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from flowly.bus.queue import MessageBus
from flowly.channels import web as web_module
from flowly.channels.web import WebChannel
from flowly.coaching import CoachingManager
from flowly.coaching import rpc as coaching_rpc
from flowly.config.schema import WebChannelConfig


def _channel(manager: CoachingManager | None) -> tuple[WebChannel, AsyncMock]:
    channel = WebChannel(WebChannelConfig(), MessageBus())
    relay = AsyncMock()
    channel._ws = relay
    channel.set_coaching_manager(manager)
    return channel, relay


def _manager() -> CoachingManager:
    # start/stop of a session without segments never reach the model.
    return CoachingManager(llm_provider=object())


async def _rpc(channel: WebChannel, relay: AsyncMock, session: str, method: str, params: dict) -> dict:
    relay.send.reset_mock()
    await channel._dispatch_rpc(relay, {"type": "rpc", "id": "r", "sessionId": session, "method": method, "params": params})
    replies = [json.loads(c.args[0]) for c in relay.send.await_args_list]
    return next(r for r in replies if r.get("type") == "rpc" and r.get("id") == "r")


def _events(relay: AsyncMock) -> list[dict]:
    return [f for f in (json.loads(c.args[0]) for c in relay.send.await_args_list) if f.get("type") == "event"]


@pytest.mark.asyncio
async def test_the_channel_serves_coaching_with_the_gateway_replies():
    manager = _manager()
    channel, relay = _channel(manager)

    started = await _rpc(channel, relay, "s1", "coaching.start", {"sessionId": "coach-1", "context": "standup"})
    assert "error" not in started
    assert started["sessionId"] == "s1"
    assert started["result"]["status"] == "started" and started["result"]["sessionId"] == "coach-1"
    assert manager.is_active("coach-1")

    state = await _rpc(channel, relay, "s1", "coaching.state", {"sessionId": "coach-1"})
    assert state["result"]["active"] is True

    missing = await _rpc(channel, relay, "s1", "coaching.segment", {"sessionId": "coach-1", "text": ""})
    assert missing["error"]["code"] == "INVALID_REQUEST"

    stopped = await _rpc(channel, relay, "s1", "coaching.stop", {"sessionId": "coach-1"})
    assert stopped["result"]["status"] == "stopped" and stopped["result"]["sessionId"] == "coach-1"
    assert not manager.is_active("coach-1")
    assert channel._coaching_rpc.owned == {}


@pytest.mark.asyncio
async def test_events_reach_the_device_that_started_the_session():
    manager = _manager()
    channel, relay = _channel(manager)
    await _rpc(channel, relay, "s1", "coaching.start", {"sessionId": "coach-1"})
    relay.send.reset_mock()

    session = manager._sessions["coach-1"]
    for callback in session.tip_callbacks:
        await callback("coach-1", "Ask about the deadline.", 0.9, seq=3, timestamp=12.5)
    for callback in session.gate_decision_callbacks:
        await callback("coach-1", stage="gate1", passed=True)

    events = _events(relay)
    assert [e["event"] for e in events] == ["coaching.tip", "coaching.gate_decision"]
    assert all(e["sessionId"] == "s1" for e in events)
    assert events[0]["data"] == {"sessionId": "coach-1", "text": "Ask about the deadline.",
                                 "confidence": 0.9, "timestamp": 12.5, "seq": 3}


@pytest.mark.asyncio
async def test_a_device_that_comes_back_takes_its_session_and_its_events():
    manager = _manager()
    channel, relay = _channel(manager)
    await _rpc(channel, relay, "s1", "coaching.start", {"sessionId": "coach-1"})
    # The desktop reconnects as a new client session and starts the same session again.
    again = await _rpc(channel, relay, "s2", "coaching.start", {"sessionId": "coach-1"})
    assert again["result"]["status"] == "reconfigured"
    assert len(manager._sessions["coach-1"].tip_callbacks) == 1  # not one per connection

    # The old connection's end does not stop it.
    await channel._handle_relay_message(relay, {"type": "browser-disconnected", "sessionId": "s1"})
    await asyncio.sleep(0)
    assert manager.is_active("coach-1")

    relay.send.reset_mock()
    for callback in manager._sessions["coach-1"].tip_callbacks:
        await callback("coach-1", "tip", 0.8)
    assert [e["sessionId"] for e in _events(relay)] == ["s2"]


@pytest.mark.asyncio
async def test_a_device_gone_for_good_has_its_session_stopped_after_the_grace(monkeypatch):
    monkeypatch.setattr(web_module, "_COACHING_RELEASE_GRACE_SECONDS", 0.05)
    manager = _manager()
    channel, relay = _channel(manager)
    await _rpc(channel, relay, "s1", "coaching.start", {"sessionId": "coach-1"})
    await _rpc(channel, relay, "s3", "coaching.start", {"sessionId": "coach-3"})

    await channel._handle_relay_message(relay, {"type": "browser-disconnected", "sessionId": "s1"})
    assert manager.is_active("coach-1"), "a short drop does not end the meeting"
    await asyncio.sleep(0.2)
    assert not manager.is_active("coach-1")
    assert manager.is_active("coach-3"), "another device's session is untouched"


@pytest.mark.asyncio
async def test_without_a_coach_the_channel_says_so_instead_of_method_not_found():
    channel, relay = _channel(None)
    started = await _rpc(channel, relay, "s1", "coaching.start", {"sessionId": "coach-1"})
    assert started["error"]["code"] == "NOT_AVAILABLE"
    state = await _rpc(channel, relay, "s1", "coaching.state", {"sessionId": "coach-1"})
    assert state["result"] == {"active": False}


@pytest.mark.asyncio
async def test_an_oversized_screenshot_is_dropped_and_the_text_still_goes():
    manager = AsyncMock()
    manager.add_transcript.return_value = {"status": "ok"}
    calls = coaching_rpc.CoachingRpc(lambda: manager, AsyncMock())
    big = "a" * (coaching_rpc.MAX_SCREENSHOT_B64 + 1)
    await calls.call("s1", "coaching.segment", {"sessionId": "c", "text": "hi", "screenshot": big})
    assert manager.add_transcript.await_args.kwargs["screenshot_b64"] is None
    await calls.call("s1", "coaching.segment", {"sessionId": "c", "text": "hi", "screenshot": "png"})
    assert manager.add_transcript.await_args.kwargs["screenshot_b64"] == "png"
