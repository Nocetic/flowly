"""An agent's tool activity reaches the apps watching its list, and only that.

The apps draw each agent as a character that looks at a terminal while a
command runs and scans while it searches. The roster (strip, agents list,
groups) is watched by a directory socket, which never receives a
conversation's own events. `tool.activity` is the compact signal for it: a
tool's name, its call id, start or end, and the conversation's end — never
the call's arguments or result.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import flowly.profile as profiles
from flowly.gateway.server import GatewayServer
from flowly.profile_host import ProfileHost, _tool_activity


@pytest.fixture
def profile_roots(tmp_path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / ".flowly"
    root = default / "profiles"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    return default, root


_START = {"toolCallId": "call-1", "name": "exec", "args": {"command": "cat ~/.ssh/id_rsa"},
          "sessionKey": "desktop:profile-home"}
_COMPLETE = {"toolCallId": "call-1", "name": "exec", "success": True, "durationMs": 12,
             "preview": "-----BEGIN OPENSSH PRIVATE KEY-----", "sessionKey": "desktop:profile-home"}


def test_a_tool_start_and_end_carry_name_call_and_conversation_only() -> None:
    assert _tool_activity("tool.start", _START) == {
        "phase": "start", "scope": "desktop:profile-home", "callId": "call-1", "name": "exec",
    }
    assert _tool_activity("tool.complete", _COMPLETE) == {
        "phase": "end", "scope": "desktop:profile-home", "callId": "call-1", "name": "exec",
    }


def test_a_conversation_ending_ends_its_calls() -> None:
    for state in ("final", "aborted", "error"):
        assert _tool_activity("chat", {"sessionKey": "ios:a", "runId": "r", "state": state}) == {
            "phase": "idle", "scope": "ios:a",
        }
    assert _tool_activity("chat", {"sessionKey": "ios:a", "runId": "r", "state": "delta"}) is None


def test_anything_else_or_malformed_is_not_activity() -> None:
    assert _tool_activity("agent", {"stream": "assistant"}) is None
    assert _tool_activity("tool.start", {**_START, "toolCallId": ""}) is None
    assert _tool_activity("tool.start", {**_START, "name": ""}) is None
    assert _tool_activity("tool.start", {**_START, "toolCallId": "x" * 257}) is None
    assert _tool_activity("tool.start", {**_START, "name": "x" * 129}) is None
    assert _tool_activity("tool.start", {**_START, "toolCallId": 7}) is None


@pytest.mark.asyncio
async def test_the_host_publishes_activity_without_arguments_or_results(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    seen: list[dict] = []

    async def collect(envelope):
        seen.append(envelope)

    host.subscribe_events(collect)
    await host._handle_profile_event("writer", "tool.start", dict(_START))
    await host._handle_profile_event("writer", "tool.complete", dict(_COMPLETE))
    await host._handle_profile_event(
        "writer", "chat", {"sessionKey": "desktop:profile-home", "runId": "r", "state": "final"},
    )

    activity = [envelope for envelope in seen if envelope["type"] == "tool.activity"]
    assert [envelope["data"]["phase"] for envelope in activity] == ["start", "end", "idle"]
    assert all(envelope["profile"] == "writer" for envelope in activity)
    text = json.dumps(activity)
    assert "id_rsa" not in text and "PRIVATE KEY" not in text
    # The conversation's own events still go out as before.
    assert [envelope["type"] for envelope in seen if envelope["type"] != "tool.activity"] == [
        "tool.start", "tool.complete", "chat",
    ]


@pytest.mark.asyncio
async def test_an_internal_turn_publishes_no_activity(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    seen: list[dict] = []

    async def collect(envelope):
        seen.append(envelope)

    host.subscribe_events(collect)
    host._broker_sessions[("writer", "desktop:profile-inbox:other")] = object()
    await host._handle_profile_event(
        "writer", "tool.start", {**_START, "sessionKey": "desktop:profile-inbox:other"},
    )
    assert seen == []


def _socket():
    ws = SimpleNamespace(closed=False, messages=[])

    async def send_json(payload):
        ws.messages.append(payload)

    async def close():
        ws.closed = True

    ws.send_json = send_json
    ws.close = close
    return ws


_ACTIVITY = {"hostId": "host", "profile": "writer", "botId": "bot", "type": "tool.activity",
             "data": {"phase": "start", "scope": "ios:a", "callId": "call-1", "name": "web_search"}}


@pytest.mark.asyncio
async def test_a_direct_gateway_sends_activity_to_the_directory_only(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    server = GatewayServer(host="127.0.0.1", enable_profile_host=True)
    reader, directory, stranger = _socket(), _socket(), _socket()
    server._ws_clients.update({"reader": reader, "directory": directory, "stranger": stranger})
    server._bind_profile_client_request("reader", "profiles.rpc", {
        "name": "writer", "method": "chat.history", "params": {"sessionKey": "ios:a"},
    })
    server._bind_profile_client_request("directory", "profiles.list", {})

    await server._broadcast_profile_host_event(_ACTIVITY)

    assert [m["data"]["type"] for m in directory.messages] == ["tool.activity"]
    # The conversation's reader has the raw tool events already.
    assert reader.messages == []
    assert stranger.messages == []
    await server.stop()


@pytest.mark.asyncio
async def test_the_relay_channel_sends_activity_to_the_directory_only() -> None:
    from unittest.mock import AsyncMock

    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig

    channel = WebChannel(WebChannelConfig(), MessageBus())
    sent: list[dict] = []
    channel._send_or_queue = AsyncMock(side_effect=lambda frame: sent.append(json.loads(frame)))
    now = time.monotonic()
    channel._profile_directory_sessions = {"directory": now}
    channel._profile_conversation_sessions = {("writer", "ios:a"): {"reader": now}}

    await channel._forward_profile_event(_ACTIVITY)

    assert [frame["sessionId"] for frame in sent] == ["directory"]
    assert sent[0]["event"] == "profile.event"
    assert sent[0]["data"]["type"] == "tool.activity"
