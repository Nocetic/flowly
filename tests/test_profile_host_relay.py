from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.profile_host import ProfileHost


@pytest.fixture
def profile_roots(tmp_path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / ".flowly"
    root = default / "profiles"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", root)
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    return default, root


class _Socket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, payload: str) -> None:
        self.messages.append(json.loads(payload))


@pytest.mark.asyncio
async def test_relay_profile_rpc_binds_and_routes_only_matching_conversation(
    profile_roots,
) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    host.dispatch = AsyncMock(return_value={"runId": "run-1"})  # type: ignore[method-assign]
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    forwarded: list[dict] = []

    async def capture(payload: str) -> None:
        forwarded.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel.set_profile_host(host)
    socket = _Socket()

    await channel._handle_rpc(socket, {
        "type": "rpc",
        "id": "rpc-1",
        "method": "profiles.rpc",
        "sessionId": "relay-a",
        "params": {
            "name": "writer",
            "method": "chat.send",
            "params": {
                "sessionKey": "ios:thread-a",
                "message": "Hello",
            },
        },
    })
    channel._bind_profile_conversation("writer", "ios:thread-b", "relay-b")

    assert socket.messages == [{
        "type": "rpc",
        "id": "rpc-1",
        "sessionId": "relay-a",
        "result": {"runId": "run-1"},
    }]

    await host._emit("writer", "chat", {
        "state": "streaming",
        "runId": "run-1",
        "sessionKey": "ios:thread-a",
        "delta": "Hi",
    })

    assert [frame["sessionId"] for frame in forwarded] == ["relay-a"]
    assert forwarded[0]["event"] == "profile.event"
    assert forwarded[0]["data"]["profile"] == "writer"
    assert forwarded[0]["data"]["data"]["delta"] == "Hi"

    await host._emit("writer", "chat", {
        "state": "final",
        "runId": "run-1",
        "sessionKey": "ios:thread-a",
        "message": {"role": "assistant", "content": []},
    })
    assert ("writer", "run-1") not in channel._profile_run_bindings


@pytest.mark.asyncio
async def test_relay_profile_lifecycle_events_do_not_reach_unbound_sessions(
    profile_roots,
) -> None:
    profiles.create_profile("writer", local_runtime=True)
    host = ProfileHost()
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    forwarded: list[dict] = []

    async def capture(payload: str) -> None:
        forwarded.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel.set_profile_host(host)
    channel._bind_profile_directory("directory")
    channel._bind_profile_conversation("writer", "ios:thread", "writer-chat")
    channel._bind_profile_conversation("default", "ios:other", "other-chat")

    await host._emit("writer", "error", {"message": "Could not start"})

    assert {frame["sessionId"] for frame in forwarded} == {
        "directory",
        "writer-chat",
    }


@pytest.mark.asyncio
async def test_relay_disconnect_releases_default_event_lease(profile_roots) -> None:
    leases: list[bool] = []
    host = ProfileHost(primary_event_lease=leases.append)
    host.dispatch = AsyncMock(return_value={"messages": []})  # type: ignore[method-assign]
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    channel.set_profile_host(host)
    socket = _Socket()

    await channel._handle_rpc(socket, {
        "type": "rpc",
        "id": "rpc-default",
        "method": "profiles.rpc",
        "sessionId": "relay-default",
        "params": {
            "name": "default",
            "method": "chat.history",
            "params": {"sessionKey": "ios:default-thread"},
        },
    })
    assert leases == [True]

    await channel._handle_relay_message(object(), {
        "type": "browser-disconnected",
        "sessionId": "relay-default",
    })
    assert leases == [True, False]
    assert "relay-default" not in channel._profile_bindings_by_relay


@pytest.mark.asyncio
async def test_relay_profile_error_preserves_code_and_retryability(profile_roots) -> None:
    from flowly.profile_host_contract import ProfileHostError

    host = ProfileHost()
    host.dispatch = AsyncMock(side_effect=ProfileHostError(
        "PROFILE_CAPACITY", "Stop another agent and try again.", retryable=True
    ))  # type: ignore[method-assign]
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    channel.set_profile_host(host)
    socket = _Socket()

    await channel._handle_rpc(socket, {
        "type": "rpc",
        "id": "rpc-error",
        "method": "profiles.connect",
        "sessionId": "relay-a",
        "params": {"name": "writer"},
    })

    assert socket.messages[0]["error"] == {
        "code": "PROFILE_CAPACITY",
        "message": "Stop another agent and try again.",
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_rejected_internal_session_is_not_bound_to_relay(profile_roots) -> None:
    host = ProfileHost()
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    channel.set_profile_host(host)
    socket = _Socket()

    await channel._handle_rpc(socket, {
        "type": "rpc",
        "id": "rpc-internal",
        "method": "profiles.rpc",
        "sessionId": "relay-a",
        "params": {
            "name": "default",
            "method": "chat.history",
            "params": {
                "sessionKey": "desktop:profile-inbox:writer:source",
            },
        },
    })

    assert socket.messages[0]["error"]["code"] == "REMOTE_SESSION_DENIED"
    assert channel._profile_bindings_by_relay == {}


@pytest.mark.asyncio
async def test_relay_walks_group_history_to_the_first_message(
    profile_roots, tmp_path
) -> None:
    """Replay iOS's exact relay frames against a real host and room store.

    The observed field failure — a transcript stuck at "couldn't load
    earlier messages" over the relay while the same pages served fine over a
    direct socket — kept pointing suspicion at this envelope. Pin the whole
    walk: a 45-message room paged at the client's limit of 10 must answer
    every request, chain cursors to the very first message, and never leave
    a frame unanswered (a silent drop is a 20-second client timeout).
    """
    host = ProfileHost()
    rooms = host._rooms
    profiles.create_profile("writer", local_runtime=True)
    created = await rooms.create("Buddies", ["default", "writer"], "panel")
    room_id = created["id"]
    room = rooms._rooms[room_id]
    for index in range(45):
        rooms._append_message(room, {
            "id": str(__import__("uuid").uuid4()),
            "role": "user" if index % 3 == 0 else "assistant",
            **({} if index % 3 == 0 else {"profile": "writer"}),
            "content": f"message {index}",
            "createdAt": "2026-08-26T01:00:00.000Z",
        })
    await rooms._persist()

    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    channel.set_profile_host(host)
    socket = _Socket()

    await channel._handle_rpc(socket, {
        "type": "rpc",
        "id": "rpc-1",
        "sessionId": "phone-room",
        "method": "profiles.rooms.list",
        "params": {"includeMessages": False, "eventMode": "delta-v1"},
    })
    listing = socket.messages[-1]
    assert listing["id"] == "rpc-1"
    listed = next(r for r in listing["result"]["rooms"] if r["id"] == room_id)
    assert listed["messageCount"] == 45

    cursor: str | None = None
    seen: list[int] = []
    for page_number in range(2, 12):
        params = {"roomId": room_id, "limit": 10, "eventMode": "delta-v1"}
        if cursor:
            params["cursor"] = cursor
        await channel._handle_rpc(socket, {
            "type": "rpc",
            "id": f"rpc-{page_number}",
            "sessionId": "phone-room",
            "method": "profiles.rooms.history",
            "params": params,
        })
        reply = socket.messages[-1]
        # Every request is answered on the socket it arrived on — the relay
        # cannot deliver a reply the channel never sent.
        assert reply["id"] == f"rpc-{page_number}", reply
        assert reply["sessionId"] == "phone-room"
        assert "error" not in reply, reply
        page = reply["result"]
        assert page["totalCount"] == 45
        assert page["hasMore"] == (page["nextCursor"] is not None)
        seen = [m["seq"] for m in page["messages"]] + seen
        cursor = page["nextCursor"]
        if cursor is None:
            break

    # The last page closes the walk at the room's first message.
    assert cursor is None
    assert seen == list(range(45))
