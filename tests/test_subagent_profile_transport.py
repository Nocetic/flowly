"""Use the real profile RPC validator and host, not a permissive fake server."""
import json
import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
import aiohttp
from aiohttp.test_utils import TestServer

from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.bus.queue import MessageBus
from flowly.config.schema import WebChannelConfig
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc
from flowly.gateway.server import GatewayServer
from tests.test_subagent_event_compat import history, gateway


@pytest.mark.parametrize("transport", ["gateway", "relay"])
@pytest.mark.parametrize("profile", ["default", "writer"])
async def test_profile_history_and_live_events_negotiate_per_reader(history, transport, profile):
    host = ProfileHost()
    # Replace only the process/network boundary. Public dispatch, validation,
    # internal v2 negotiation, feature handlers and outgoing routing are real.
    requests = []
    async def target(name, method, params, timeout, **kwargs):
        requests.append((name, method, params.copy()))
        result, _ = await feature_rpc.dispatch(method, params)
        return result
    host._target_rpc = target
    server = gateway()
    server._profile_host = host
    server._profile_client_subscriptions = OrderedDict()
    server._profile_run_subscriptions = OrderedDict()
    channel = WebChannel(WebChannelConfig(), MessageBus())
    channel._profile_host = host
    channel._ws = AsyncMock()
    sockets = {name: web.WebSocketResponse() for name in ("old", "new", "other", "failed")}
    server._ws_clients = sockets

    async def read(reader, selected, method, params):
        envelope = {"name": selected, "method": method, "params": params}
        if transport == "gateway":
            await server._handle_profile_host_rpc(sockets[reader], reader, reader, "profiles.rpc", envelope)
        else:
            await channel._handle_profile_rpc(channel._ws, {"type": "rpc", "id": reader,
                "sessionId": reader, "method": "profiles.rpc", "params": envelope})

    await read("old", profile, "subagents.list", {})
    await read("new", profile, "subagents.list", {"eventVersion": 2})
    await read("other", "other-profile", "subagents.list", {"eventVersion": 2})
    await read("failed", profile, "subagents.get", {"eventVersion": 2, "runId": "missing"})
    assert all(params["eventVersion"] == 2 for _, _, params in requests)
    # A primary-runtime upgrade must not change the profile reader's version.
    server._subagent_event_versions[sockets["old"]] = 2
    channel._subagent_event_versions["old"] = 2
    row = feature_rpc.subagents_list({})["tasks"][0]
    event = {"profile": profile, "type": "subagent.progress", "data": {**row, "running": 1, "outcome": None}}
    if transport == "gateway":
        server._ws_send.reset_mock()
        await server._broadcast_profile_host_event(event)
        frames = {next(key for key, ws in sockets.items() if ws is call.args[0]): call.args[1]
                  for call in server._ws_send.await_args_list}
    else:
        channel._ws.reset_mock()
        await channel._forward_profile_event(event)
        frames = {data["sessionId"]: data for call in channel._ws.send.await_args_list
                  for data in [json.loads(call.args[0])]}
    assert set(frames) == {"old", "new"}  # not failed reads or another profile
    assert frames["old"]["data"]["type"] == "subagent.started"
    assert frames["old"]["data"]["data"]["runId"] == row["runId"][:8]
    assert frames["new"]["data"] == event
    assert "schemaVersion" not in frames["old"]["data"]["data"]
    # The result endpoint also traverses the real named-profile allowlist.
    history.finish(row["runId"], "ok", result="Saved report")
    response = await host.dispatch("profiles.rpc", {"name": profile, "method": "subagents.result",
                                                   "params": {"runId": row["runId"]}})
    assert response["content"] == "Saved report"
    if transport == "relay":
        if profile == "default":
            assert host._default_event_leases
        for reader in ("old", "new", "other", "failed"):
            await channel._handle_relay_message(channel._ws, {"type": "browser-disconnected", "sessionId": reader})
        assert not channel._profile_subagent_observers
        assert not host._default_event_leases


def test_profile_history_is_read_only_and_rejects_invalid_versions():
    for method in ("subagents.list", "subagents.get", "subagents.result"):
        assert validate_profile_rpc(method, {})[0] == method
    for method in ("subagents.cancel", "subagents.spawn", "subagents.set_model"):
        with pytest.raises(ProfileHostError):
            validate_profile_rpc(method, {})
    with pytest.raises(ProfileHostError):
        validate_profile_rpc("subagents.list", {"eventVersion": True})


def test_profile_relay_expiry_releases_default_bridge_and_cannot_leak_versions(history):
    channel = WebChannel(WebChannelConfig(), MessageBus())
    channel._profile_host = ProfileHost()
    channel._observe_profile_subagents("reader", "default", {"eventVersion": 2})
    assert channel._profile_host._default_event_leases
    channel._prune_profile_subagent_observers(time.monotonic() + 121)
    assert not channel._profile_subagent_observers
    assert not channel._profile_host._default_event_leases

    channel._observe_profile_subagents("reader", "default", {})
    assert channel._profile_subagent_observers[("reader", "default")][0] == 1
    channel._clear_profile_subagent_observers()
    assert not channel._profile_host._default_event_leases


async def test_default_profile_uses_actual_internal_gateway_bridge(history):
    class Socket:
        closed = False
        def __init__(self):
            self.messages = []
        async def send_json(self, data):
            self.messages.append(data)

    server = GatewayServer(host="127.0.0.1", auth_token="fixture-only", require_loopback_auth=True,
                           on_chat_message=AsyncMock(), enable_profile_host=True)
    old, modern = Socket(), Socket()
    server._ws_clients.update(old=old, modern=modern)
    for key, socket, params in (("old", old, {}), ("modern", modern, {"eventVersion": 2})):
        await server._handle_profile_host_rpc(socket, key, key, "profiles.rpc", {
            "name": "default", "method": "subagents.list", "params": params})
        assert socket.messages[-1]["result"]["tasks"][0]["runId"] == history.all()[0].run_id
    assert server._subagent_event_versions[server._profile_host_socket] == 2
    data = {**feature_rpc.subagents_list({})["tasks"][0], "running": 1, "outcome": None}
    old.messages.clear()
    modern.messages.clear()
    await server._broadcast_subagent_event("subagent.progress", data)
    # The same physical gateway also sends the primary runtime's legacy event.
    # Scoped profile subscribers must independently get their negotiated frame.
    old_profile = next(m for m in old.messages if m.get("event") == "profile.event")
    new_profile = next(m for m in modern.messages if m.get("event") == "profile.event")
    assert old_profile["data"]["data"]["runId"] == data["runId"][:8]
    assert new_profile["data"]["data"]["runId"] == data["runId"]
    assert new_profile["data"]["type"] == "subagent.progress"


async def test_relay_disconnect_during_profile_read_cannot_resubscribe(history):
    channel = WebChannel(WebChannelConfig(), MessageBus())
    channel._profile_host = ProfileHost()
    channel._ws = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()
    async def target(name, method, params, timeout, **kwargs):
        entered.set()
        await release.wait()
        return feature_rpc.subagents_list(params)
    channel._profile_host._target_rpc = target
    read = asyncio.create_task(channel._handle_profile_rpc(channel._ws, {
        "type": "rpc", "id": "pending", "sessionId": "reader", "method": "profiles.rpc",
        "params": {"name": "default", "method": "subagents.list", "params": {"eventVersion": 2}},
    }))
    await asyncio.wait_for(entered.wait(), 1)
    await channel._handle_relay_message(channel._ws, {"type": "browser-disconnected", "sessionId": "reader"})
    release.set()
    await asyncio.wait_for(read, 1)
    assert not channel._profile_subagent_observers
    assert not channel._profile_subagent_reads
    assert not channel._profile_host._default_event_leases


async def test_authenticated_websockets_negotiate_through_real_profile_rpc(history):
    server = GatewayServer(host="127.0.0.1", auth_token="fixture-only", require_loopback_auth=True,
                           on_chat_message=AsyncMock(), enable_profile_host=True)
    async with TestServer(server._create_app()) as http, aiohttp.ClientSession() as session:
        async def connect(client_id):
            async with session.post(http.make_url("/api/auth/ws-ticket"),
                                    headers={"Authorization": "Bearer fixture-only"}) as reply:
                assert reply.status == 200
                ticket = (await reply.json())["ticket"]
            return await session.ws_connect(http.make_url(f"/ws?ticket={ticket}&clientId={client_id}"))

        async def receive(ws, predicate):
            async with asyncio.timeout(3):
                while True:
                    frame = await ws.receive_json()
                    if predicate(frame):
                        return frame

        async def read(ws, key, params):
            await ws.send_json({"type": "rpc", "id": key, "method": "profiles.rpc", "params": {
                "name": "default", "method": "subagents.list", "params": params}})
            reply = await receive(ws, lambda frame: frame.get("id") == key)
            assert reply["result"]["tasks"][0]["runId"] == history.all()[0].run_id

        old, modern = await connect("old"), await connect("modern")
        try:
            await read(old, "old-list", {})
            await read(modern, "new-list", {"eventVersion": 2})
            row = feature_rpc.subagents_list({})["tasks"][0]
            data = {**row, "outcome": "ok", "status": "ok", "running": 0}
            await server._broadcast_subagent_event("subagent.completed", data)
            for ws, expected in ((old, row["runId"][:8]), (modern, row["runId"])):
                frame = await receive(ws, lambda item: item.get("event") == "profile.event")
                assert frame["data"]["data"]["runId"] == expected
            await modern.close()
            modern = await connect("modern")
            await read(modern, "reconnected", {})
            await server._broadcast_subagent_event("subagent.completed", data)
            frame = await receive(modern, lambda item: item.get("event") == "profile.event")
            assert frame["data"]["data"]["runId"] == row["runId"][:8]
        finally:
            await old.close()
            await modern.close()
            await server.profile_host.shutdown()


async def test_relay_receive_failure_cleans_up_active_profile_leases(history, monkeypatch):
    monkeypatch.delenv("MOLTBOT_PROXY_JWT_SECRET", raising=False)
    channel = WebChannel(WebChannelConfig(auth_token="test-only-" * 5), MessageBus())
    channel._profile_host = ProfileHost()
    class BrokenSocket:
        send = AsyncMock()
        def __aiter__(self):
            return self
        async def __anext__(self):
            channel._observe_profile_subagents("reader", "default", {"eventVersion": 2})
            channel._subagent_observers["reader"] = time.monotonic() + 120
            channel._subagent_event_versions["reader"] = 2
            raise ConnectionError("fixture connection closed")
    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield BrokenSocket()
    monkeypatch.setattr("flowly.channels.web.websockets.connect", connect)
    with pytest.raises(ConnectionError):
        await channel._connect_and_run()
    assert channel._ws is None
    assert not channel._subagent_observers
    assert not channel._subagent_event_versions
    assert not channel._profile_subagent_observers
    assert not channel._profile_host._default_event_leases
