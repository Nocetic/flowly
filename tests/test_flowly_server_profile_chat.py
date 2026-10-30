"""A named agent's one chat through the Flowly server (server PROTOCOL A7, B7).

The server delivers a named agent's message with ``params.profile`` on its
``srv-p:<profile>:<conversationId>`` session. Core runs it in that agent and
sends the agent's chat back on that session, once, under its profile, in the
shape the main agent's chat has. Devices that still follow the agent through
``profiles.rpc`` keep their ``profile.event`` copies.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
from flowly.agent_home import HOME_SESSION
from flowly.bus.queue import MessageBus
from flowly.channels.flowly_server import FlowlyServerChannel
from flowly.channels.web import WebChannel
from flowly.config.schema import FlowlyServerChannelConfig, WebChannelConfig
from flowly.profile_host import ProfileHost

SESSION = "srv-p:writer:conv-1"


@pytest.fixture
def home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / ".flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    profiles.create_profile("writer", local_runtime=True)
    return default


class Socket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def make_channel(result=None) -> tuple[FlowlyServerChannel, ProfileHost, list[dict]]:
    channel = FlowlyServerChannel(FlowlyServerChannelConfig(
        enabled=True, server_url="wss://cloud.example.test/v1/agent", agent_secret="s", agent_host_id="host-1",
    ), MessageBus())
    host = ProfileHost()
    host.dispatch = AsyncMock(return_value=result if result is not None else {"runId": "run-1"})  # type: ignore[method-assign]
    forwarded: list[dict] = []

    async def capture(payload: str) -> None:
        forwarded.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel.set_profile_host(host)
    return channel, host, forwarded


async def deliver(channel: FlowlyServerChannel, socket: Socket, rpc_id: str = "d1", **params) -> None:
    await channel._handle_rpc(socket, {
        "type": "rpc", "id": rpc_id, "sessionId": SESSION, "method": "chat.send",
        "params": {"profile": "writer", "sessionKey": HOME_SESSION, "message": "Draft the post",
                   "idempotencyKey": "run-1", **params},
    })


def test_core_says_it_speaks_one_chat_per_agent_on_the_server_only():
    channel, _host, _ = make_channel()
    assert channel._server_capabilities() == {"conversations": {"version": 2}}
    assert WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())._server_capabilities() == {}


@pytest.mark.asyncio
async def test_system_capabilities_carries_the_flag(home):
    channel, _host, _ = make_channel()
    socket = Socket()
    await channel._handle_feature_rpc(socket, "c1", "srv-1", "system.capabilities", {})
    assert socket.sent[-1]["result"]["conversations"] == {"version": 2}


@pytest.mark.asyncio
async def test_a_delivery_runs_in_the_agent_and_binds_no_profile_event_copies(home):
    channel, host, _ = make_channel()
    socket = Socket()
    await deliver(channel, socket)
    host.dispatch.assert_awaited_once_with("profiles.rpc", {
        "name": "writer", "method": "chat.send",
        # The profile host's own validation fills in what it always carries (attachments).
        "params": {"sessionKey": HOME_SESSION, "message": "Draft the post", "idempotencyKey": "run-1", "attachments": []},
    })
    assert socket.sent == [{"type": "rpc", "id": "d1", "sessionId": SESSION, "result": {"runId": "run-1"}}]
    assert SESSION not in channel._profile_bindings_by_relay, "the server fans the chat out itself"
    saved = json.loads((home / "flowly_server" / "profile_sessions.json").read_text())
    assert saved == {"writer": SESSION}


@pytest.mark.asyncio
async def test_the_agents_chat_reaches_the_server_once_in_the_main_agents_shape(home):
    channel, host, forwarded = make_channel()
    await deliver(channel, Socket())
    await host._emit("writer", "agent", {"runId": "run-1", "sessionKey": HOME_SESSION, "stream": "assistant",
                                         "data": {"text": "Hel"}, "seq": 1})
    await host._emit("writer", "chat", {"state": "iteration_step", "runId": "run-1", "sessionKey": HOME_SESSION,
                                        "role": "assistant", "tool_calls": [], "seq": 2})
    await host._emit("writer", "chat", {"state": "final", "runId": "m-1", "streamRunId": "run-1",
                                        "sessionKey": HOME_SESSION, "message": {"content": [{"type": "text", "text": "Hello"}]}})
    assert [(f["sessionId"], f["event"], f["profile"], f["data"].get("state"), f["data"].get("seq")) for f in forwarded] == [
        (SESSION, "chat", "writer", "streaming", 1),
        (SESSION, "chat", "writer", "iteration_step", 2),
        (SESSION, "chat", "writer", "final", None),
    ]
    assert forwarded[0]["data"] == {"state": "streaming", "runId": "run-1", "sessionKey": HOME_SESSION, "delta": "Hel", "seq": 1}


@pytest.mark.asyncio
async def test_only_its_one_chat_and_only_chat_events_go_to_the_server(home):
    channel, host, forwarded = make_channel()
    await deliver(channel, Socket())
    await host._emit("writer", "chat", {"state": "final", "runId": "x", "sessionKey": "ios:other-thread", "message": {}})
    await host._emit("writer", "connection", {"state": "connected"})
    # A roster event about this very chat is still not the chat.
    await host._emit("writer", "tool.activity", {"phase": "turn", "sessionKey": HOME_SESSION, "runId": "run-1"})
    await host._emit("writer", "agent", {"runId": "run-1", "sessionKey": HOME_SESSION, "stream": "thinking", "data": {"text": "…"}})
    await host._emit("default", "chat", {"state": "final", "runId": "y", "sessionKey": HOME_SESSION, "message": {}})
    assert forwarded == []


@pytest.mark.asyncio
async def test_a_device_on_the_old_path_keeps_its_copy_and_the_server_gets_one(home):
    channel, host, forwarded = make_channel()
    await deliver(channel, Socket())
    channel._bind_profile_conversation("writer", HOME_SESSION, "c-phone")
    await host._emit("writer", "chat", {"state": "final", "runId": "m", "streamRunId": "run-1", "sessionKey": HOME_SESSION, "message": {}})
    assert sorted((f["sessionId"], f["event"]) for f in forwarded) == [("c-phone", "profile.event"), (SESSION, "chat")]


@pytest.mark.asyncio
async def test_after_a_restart_the_agents_own_turn_still_reaches_the_server(home):
    channel, _host, _ = make_channel()
    await deliver(channel, Socket())
    restarted, host, forwarded = make_channel()
    # Its introduction or a goal's next step: no chat.send, the first event names the chat.
    await host._emit("writer", "chat", {"state": "user", "runId": "own-1", "sessionKey": HOME_SESSION,
                                        "message": {"role": "user", "content": []}})
    await host._emit("writer", "agent", {"runId": "own-1", "stream": "assistant", "data": {"text": "Hi"}})
    assert [(f["sessionId"], f["data"]["state"]) for f in forwarded] == [(SESSION, "user"), (SESSION, "streaming")]
    assert forwarded[1]["data"]["sessionKey"] == HOME_SESSION, "the run's chat is known from its first event"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.inflight", "chat.abort", "chat.steer", "chat.command"])
async def test_the_agents_other_chat_calls_run_in_it(home, method):
    channel, host, _ = make_channel({"ok": True})
    socket = Socket()
    params = {"profile": "writer", "sessionKey": HOME_SESSION, "runId": "run-1"}
    if method == "chat.steer":
        params["message"] = "shorter"
    await channel._handle_rpc(socket, {"type": "rpc", "id": "q1", "sessionId": "c-app", "method": method, "params": params})
    called = host.dispatch.await_args.args
    assert called[0] == "profiles.rpc"
    assert called[1]["name"] == "writer" and called[1]["method"] == method
    assert "profile" not in called[1]["params"]
    assert socket.sent[-1]["result"] == {"ok": True}
    assert not (home / "flowly_server" / "profile_sessions.json").exists(), "only a delivery names the server session"


@pytest.mark.asyncio
async def test_the_servers_inflight_names_the_session_before_any_message(home):
    """A new agent introduces itself before anyone writes to it: the server binds
    its session with a read-only chat.inflight, so those first words reach it."""
    channel, host, forwarded = make_channel({"inflight": None})
    socket = Socket()
    await channel._handle_rpc(socket, {"type": "rpc", "id": "i1", "sessionId": SESSION, "method": "chat.inflight",
                                       "params": {"profile": "writer", "sessionKey": HOME_SESSION}})
    saved = json.loads((home / "flowly_server" / "profile_sessions.json").read_text())
    assert saved == {"writer": SESSION}
    await host._emit("writer", "agent", {"runId": "intro-1", "sessionKey": HOME_SESSION, "stream": "assistant", "data": {"text": "Hi"}})
    assert [(f["sessionId"], f["data"]["state"]) for f in forwarded] == [(SESSION, "streaming")]


@pytest.mark.asyncio
async def test_a_bad_agent_name_or_no_profile_host_is_refused(home):
    channel, _host, _ = make_channel()
    socket = Socket()
    await channel._handle_rpc(socket, {"type": "rpc", "id": "b1", "sessionId": SESSION, "method": "chat.send",
                                       "params": {"profile": "../evil", "sessionKey": HOME_SESSION, "message": "x"}})
    assert socket.sent[-1]["error"]["code"] == "INVALID_PARAMS"
    channel.set_profile_host(None)
    await deliver(channel, socket, rpc_id="b2")
    assert socket.sent[-1]["error"]["code"] == "PROFILE_HOST_UNAVAILABLE"


@pytest.mark.asyncio
async def test_a_saved_session_that_names_another_agent_is_ignored(home):
    path = home / "flowly_server" / "profile_sessions.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"writer": "srv-p:other:conv-9", "bad": "c-123"}))
    channel, host, forwarded = make_channel()
    await host._emit("writer", "chat", {"state": "final", "runId": "m", "sessionKey": HOME_SESSION, "message": {}})
    assert forwarded == []


@pytest.mark.asyncio
async def test_a_scheduled_tasks_result_in_the_agents_chat_reaches_the_server(home):
    # A job made in the agent's chat delivers to its home session as a
    # proactive final (cli/gateway_cmd.py); it is the agent's chat like any reply.
    channel, host, forwarded = make_channel()
    await deliver(channel, Socket())
    await host._emit("writer", "chat", {"state": "final", "proactive": True, "runId": "proactive-1a2b",
                                        "sessionKey": HOME_SESSION,
                                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Daily report"}]}})
    assert [(f["sessionId"], f["event"], f["data"]["state"], f["data"]["proactive"]) for f in forwarded] == [
        (SESSION, "chat", "final", True)]


@pytest.mark.asyncio
async def test_the_server_reads_the_agents_chat_with_exact_times(home):
    # Sessions store local time with no zone; the server (UTC) gets epoch milliseconds.
    from datetime import datetime

    local = datetime(2026, 10, 1, 9, 30, 0)
    channel, host, _ = make_channel({"messages": [
        {"id": "e1", "role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": local.isoformat()},
        {"id": "e2", "role": "assistant", "content": [], "timestamp": "not a time"},
    ], "hasOlder": False, "before": None})
    socket = Socket()
    await channel._handle_rpc(socket, {"type": "rpc", "id": "h1", "method": "chat.history",
                                       "params": {"profile": "writer", "sessionKey": HOME_SESSION, "limit": 100}})
    assert host.dispatch.await_args.args[1]["method"] == "chat.history"
    rows = socket.sent[-1]["result"]["messages"]
    assert rows[0]["timestampMs"] == int(local.astimezone().timestamp() * 1000)
    assert "timestampMs" not in rows[1]


@pytest.mark.asyncio
async def test_a_final_names_its_reply_apart_from_the_persons_message(home):
    # The server stores the reply under the final's runId: the direct gateway's
    # (the turn, = the person's message id) would overwrite the person's row.
    channel, host, forwarded = make_channel()
    await deliver(channel, Socket())
    await host._emit("writer", "chat", {"state": "final", "runId": "run-1", "sessionKey": HOME_SESSION,
                                        "message": {"role": "assistant", "content": []}})
    assert forwarded[0]["data"]["runId"] == "run-1:reply"
    assert forwarded[0]["data"]["streamRunId"] == "run-1"
    # A final already in the main agent's shape passes as it is.
    await host._emit("writer", "chat", {"state": "final", "runId": "m-9", "streamRunId": "run-2", "sessionKey": HOME_SESSION,
                                        "message": {"role": "assistant", "content": []}})
    assert (forwarded[1]["data"]["runId"], forwarded[1]["data"]["streamRunId"]) == ("m-9", "run-2")


@pytest.mark.asyncio
async def test_a_message_between_agents_reaches_the_server_once_whoever_watches(home) -> None:
    channel, host, forwarded = make_channel()
    # A device that watches the writer's chat must not get its own copy.
    channel._profile_conversation_sessions[("writer", HOME_SESSION)] = {"device-1": 0.0}
    data = {"phase": "started", "exchangeId": "x1", "source": "default", "target": "writer",
            "sourceSessionKey": "web:main-chat", "message": "Draft the post", "at": 1}

    await host._emit("writer", "agent.exchange", data)

    assert forwarded == [{"type": "event", "event": "agent.exchange", "data": data}]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["secrets.submit", "secrets.list", "secrets.decline"])
async def test_a_named_agents_secret_calls_run_in_it(home, method):
    """Secrets are the agent's own (flowly.secrets): the server names it with
    ``params.profile`` as for its chat, never the main agent's vault."""
    channel, host, _ = make_channel({"saved": ["FAL_KEY"]})
    socket = Socket()
    await channel._handle_rpc(socket, {"type": "rpc", "id": "s1", "method": method,
                                       "params": {"profile": "writer", "names": ["FAL_KEY"]}})
    called = host.dispatch.await_args.args
    assert called[0] == "profiles.rpc"
    assert called[1] == {"name": "writer", "method": method, "params": {"names": ["FAL_KEY"]}}
    assert socket.sent[-1]["result"] == {"saved": ["FAL_KEY"]}
