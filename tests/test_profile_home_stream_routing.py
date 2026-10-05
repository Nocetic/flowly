"""A named agent's introduction streams to the app that asked for it.

``agent.home.introduce`` starts the agent's first message on the host. The
app learns of it only through the conversation's events, which the gateway
and the relay route to sockets bound to ``(profile, sessionKey)``. The
``agent.home.*`` calls carry no session key, so the asking app was not bound
yet when the first words streamed: it saw the whole introduction land at
once, seconds later, from a history read. Every ``agent.home.*`` call is
about the home conversation, and binds the caller to it before it runs.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

import flowly.profile as profiles
from flowly.agent_home import HOME_SESSION
from flowly.gateway.server import GatewayServer
from flowly.profile_host_contract import profile_rpc_session_key


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


BOT = "5f0c7c4e-0f4b-4d8e-9d59-2d6f1d0d3a11"
_DELTA = {"hostId": "host", "profile": "writer", "botId": BOT, "type": "agent",
          "data": {"runId": "agent-intro-1", "sessionKey": HOME_SESSION, "stream": "assistant",
                   "data": {"text": "Hello"}}}


def test_every_home_call_is_about_the_home_conversation() -> None:
    for method in ("agent.home.get", "agent.home.introduce", "agent.home.setup"):
        assert profile_rpc_session_key(method, {"expectedBotId": BOT}) == HOME_SESSION
    assert profile_rpc_session_key("chat.history", {"sessionKey": "ios:a"}) == "ios:a"
    assert profile_rpc_session_key("chat.history", {}) == ""
    assert profile_rpc_session_key("provider.list", {"sessionKey": 7}) == ""


def _socket():
    ws = SimpleNamespace(closed=False, messages=[])

    async def send_json(payload):
        ws.messages.append(payload)

    async def close():
        ws.closed = True

    ws.send_json = send_json
    ws.close = close
    return ws


@pytest.mark.asyncio
async def test_a_direct_gateway_streams_the_introduction_to_the_app_that_asked(profile_roots) -> None:
    profiles.create_profile("writer", local_runtime=True)
    server = GatewayServer(host="127.0.0.1", enable_profile_host=True)
    asker, other = _socket(), _socket()
    server._ws_clients.update({"asker": asker, "other": other})
    server._bind_profile_client_request("asker", "profiles.rpc", {
        "name": "writer", "method": "agent.home.introduce",
        "params": {"expectedBotId": BOT, "locale": "en"},
    })
    server._bind_profile_client_request("other", "profiles.list", {})

    await server._broadcast_profile_host_event(_DELTA)

    assert [m["data"]["data"]["data"]["text"] for m in asker.messages] == ["Hello"]
    assert other.messages == []
    await server.stop()


@pytest.mark.asyncio
async def test_the_relay_streams_the_introduction_to_the_session_that_asked(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig

    channel = WebChannel(WebChannelConfig(), MessageBus())
    sent: list[dict] = []
    channel._send_or_queue = AsyncMock(side_effect=lambda frame: sent.append(json.loads(frame)))
    host = SimpleNamespace(
        dispatch=AsyncMock(return_value={"version": 1}),
        retain_default_events=lambda owner: None,
        release_default_events=lambda owner: None,
    )
    channel._profile_host = host
    ws = SimpleNamespace(send=AsyncMock())
    await channel._handle_profile_rpc(ws, {
        "id": "1", "sessionId": "phone", "method": "profiles.rpc",
        "params": {"name": "writer", "method": "agent.home.introduce",
                   "params": {"expectedBotId": BOT, "locale": "en"}},
    })
    channel._profile_directory_sessions = {"elsewhere": time.monotonic()}

    await channel._forward_profile_event(_DELTA)

    assert [frame["sessionId"] for frame in sent] == ["phone"]
