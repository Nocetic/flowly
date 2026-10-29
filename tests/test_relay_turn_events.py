"""What a user's turn over the relay / Flowly server tells the apps while it runs."""

from __future__ import annotations

import asyncio

import pytest

from unittest.mock import AsyncMock

from flowly.bus.queue import MessageBus
from tests.test_subagent_report_streaming import CHAT, _FakeWS, _Provider, _agent, _gateway, _relay, _until


@pytest.mark.asyncio
async def test_a_tool_running_in_a_user_turn_reaches_the_apps_live(tmp_path):
    provider = _Provider(session_key="web:chat")
    bus = MessageBus()
    agent = _agent(tmp_path, provider, bus)
    server = _gateway(agent)
    channel, payloads = _relay(bus, server)
    pump = asyncio.create_task(_forward_outbound(bus, channel))
    try:
        await channel._process_message("relay-2", "web:chat", "when was it founded?", "run-1")
        msg = await asyncio.wait_for(bus.consume_inbound(), 5)
        await agent._process_scoped_turn(msg)
        await _until(lambda: any(p.get("data", {}).get("state") == "final" for p in payloads))

        progress = [p["data"] for p in payloads if p.get("data", {}).get("state") == "tool_progress"]
        assert progress, "no tool_progress reached the relay"
        assert all(e["runId"] == "run-1" for e in progress)
        calls = [e["call"] for e in progress]
        assert {c.get("name") for c in calls} == {"list_dir"}
        assert len({c.get("state") for c in calls}) > 1  # its lifecycle, not one snapshot
        steps = [p["data"] for p in payloads if p.get("data", {}).get("state") == "iteration_step"]
        assert steps  # the steps still go as before
    finally:
        pump.cancel()
        agent.stop()


@pytest.mark.asyncio
async def test_a_user_turn_on_the_relay_says_it_started_before_anything_else(tmp_path):
    provider = _Provider(session_key="web:chat")
    bus = MessageBus()
    agent = _agent(tmp_path, provider, bus)
    server = _gateway(agent)
    channel, payloads = _relay(bus, server)
    pump = asyncio.create_task(_forward_outbound(bus, channel))
    try:
        await channel._process_message("relay-2", "web:chat", "when was it founded?", "run-1")
        msg = await asyncio.wait_for(bus.consume_inbound(), 5)
        await agent._process_scoped_turn(msg)
        await _until(lambda: any(p.get("data", {}).get("state") == "final" for p in payloads))

        events = [p["data"] for p in payloads if p.get("event") == "chat"]
        # The apps show the agent working from here, not from its first word or tool.
        assert events[0] == {"state": "run_started", "runId": "run-1", "sessionKey": "web:chat"}
        assert sum(e["state"] == "run_started" for e in events) == 1
    finally:
        pump.cancel()
        agent.stop()


@pytest.mark.asyncio
async def test_a_user_turn_on_the_direct_gateway_says_it_started_before_anything_else(tmp_path):
    provider = _Provider()
    agent = _agent(tmp_path, provider)
    server = _gateway(agent)
    ws = _FakeWS()
    server._session_ws[CHAT] = ws
    server.push_session_message = AsyncMock()  # type: ignore[method-assign]
    try:
        await server._run_chat(ws, "client", CHAT, "when was it founded?", "run-u", stream_callback=AsyncMock())

        chat = [e["data"] for e in ws.sent if e.get("event") == "chat"]
        assert chat[0] == {"state": "run_started", "runId": "run-u", "sessionKey": CHAT}
        assert chat[-1]["state"] == "final" and chat[-1]["runId"] == "run-u"
    finally:
        agent.stop()


async def _forward_outbound(bus: MessageBus, channel) -> None:
    while True:
        await channel.send(await bus.consume_outbound())
