"""Installed clients must reconcile task IDs without requiring an app update."""
from __future__ import annotations

import json
import gc
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

import pytest
from aiohttp import web

from flowly.agent.subagent_observation import TaskEvents, client_event
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    registry = SubagentRegistry(tmp_path / "runs.json")
    registry.register(SubagentRunRecord(
        run_id="12345678-1234-4321-8888-123456789abc", child_session_key="subagent:child",
        parent_session_key="desktop:parent", parent_channel="web", parent_chat_id="parent",
        task="Review the result", label="researcher", display_name="Review the result",
        model="fixture/model", cleanup="keep", created_at=100,
    ))
    monkeypatch.setattr(feature_rpc, "_registry_provider", lambda: registry)
    return registry


def gateway():
    server = object.__new__(GatewayServer)
    server._subagent_event_versions = WeakKeyDictionary()
    server._profile_subagent_event_versions = WeakKeyDictionary()
    server._ws_clients = {}
    server._ws_rpc_reply = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    server._ws_send = AsyncMock()
    return server


def desktop_event(rows, data):
    # MenuBarResponse's installed reducer: snapshot IDs are sliced to 8 chars,
    # while lifecycle updates compare runId verbatim. Do not modernize this.
    if not data.get("outcome") and data.get("runId") and data.get("label"):
        if not any(row["runId"] == data["runId"] for row in rows):
            rows.append({"runId": data["runId"]})
    elif data.get("outcome") and data.get("runId"):
        rows[:] = [row for row in rows if row["runId"] != data["runId"]]


@pytest.mark.parametrize("transport", ["gateway", "relay"])
@pytest.mark.parametrize("snapshot_first", [True, False])
async def test_old_and_new_clients_share_server_without_duplicate_or_stuck_tasks(history, transport, snapshot_first):
    snapshot = feature_rpc.subagents_list({})
    full_id = snapshot["tasks"][0]["runId"]
    rows = [{"runId": full_id[:8]}] if snapshot_first else []
    server = gateway()
    legacy, modern = web.WebSocketResponse(), web.WebSocketResponse()
    server._ws_clients = {"old": legacy, "new": modern}
    channel = WebChannel(WebChannelConfig(), MessageBus())
    channel._ws = AsyncMock()
    for key, ws, params in [("old", legacy, {}), ("new", modern, {"eventVersion": 2})]:
        if transport == "gateway":
            await server._handle_feature_rpc(ws, key, "subagents.list", params)
        else:
            await channel._handle_feature_rpc(channel._ws, key, key, "subagents.list", params)

    async def send(event, data):
        if transport == "gateway":
            server._ws_send.reset_mock()
            await server._broadcast_subagent_event(event, data)
            frames = {"old" if call.args[0] is legacy else "new": call.args[1]
                      for call in server._ws_send.await_args_list}
        else:
            channel._ws.reset_mock()
            await channel.send_subagent_event(event, data)
            frames = {frame["sessionId"]: frame for call in channel._ws.send.await_args_list
                      for frame in [json.loads(call.args[0])]}
        assert set(frames) == {"old", "new"}
        assert frames["new"]["data"] == data
        assert frames["new"]["event"] == event
        assert frames["old"]["data"]["runId"] == full_id[:8]
        assert "schemaVersion" not in frames["old"]["data"]
        desktop_event(rows, frames["old"]["data"])

    # Deliberately coalesce away the original start. Old clients still see a
    # usable start, and repeated progress must not insert duplicate rows.
    publisher = TaskEvents(send)
    for revision in range(1, 10):
        data = {**snapshot["tasks"][0], "revision": revision, "running": 1, "outcome": None}
        publisher.publish("subagent.started" if revision == 1 else "subagent.progress", data)
    await publisher.flush()
    assert rows == [{"runId": full_id[:8]}]
    await send("subagent.progress", data)
    assert len(rows) == 1
    await send("subagent.completed", {**data, "revision": 10, "status": "ok", "outcome": "ok", "running": 0})
    assert rows == []


@pytest.mark.parametrize("outcome", ["ok", "error", "timeout", "cancelled", "interrupted"])
@pytest.mark.parametrize("kind", ["subagent", "delegate"])
def test_legacy_terminal_shapes_and_nonmutating_projection(history, kind, outcome):
    data = {**feature_rpc.subagents_list({})["tasks"][0], "kind": kind, "agentId": "writer",
            "outcome": outcome, "running": 0, "toolTrace": [{"tool": "read_file"}], "error": "fixture"}
    original = deepcopy(data)
    name, projected = client_event("subagent.completed", data)
    expected = {"runId": data["runId"][:8], "outcome": outcome, "running": 0,
                "label": "@writer" if kind == "delegate" else data["label"]}
    if kind != "delegate":
        expected.update(error="fixture", toolTrace=[{"tool": "read_file"}])
    assert name == "subagent.completed"
    assert projected == expected
    assert data == original
    assert client_event(name, projected) == (name, projected)  # bridged legacy frame


async def test_gateway_negotiation_is_socket_local_and_success_only(history):
    server = gateway()
    ws = web.WebSocketResponse()
    await server._handle_feature_rpc(ws, "1", "subagents.get", {"runId": "missing", "eventVersion": 2})
    assert server._subagent_event_versions.get(ws, 1) == 1
    server._ws_rpc_error.assert_awaited_once()
    await server._handle_feature_rpc(ws, "2", "subagents.list", {"eventVersion": 2})
    assert server._subagent_event_versions[ws] == 2
    await server._handle_feature_rpc(ws, "3", "subagents.list", {})
    assert server._subagent_event_versions[ws] == 2  # ordinary refresh cannot downgrade
    await server._handle_feature_rpc(ws, "4", "subagents.list", {"eventVersion": 3})
    assert server._subagent_event_versions[ws] == 2  # failed negotiation cannot change state
    # The same clientId on a replacement socket must default to legacy.
    replacement = web.WebSocketResponse()
    server._ws_clients["stable-id"] = replacement
    data = feature_rpc.subagents_list({})["tasks"][0]
    await server._broadcast_subagent_event("subagent.started", data)
    assert server._ws_send.call_args.args[0] is replacement
    assert server._ws_send.call_args.args[1]["data"]["runId"] == data["runId"][:8]
    await server._handle_feature_rpc(ws, "5", "subagents.list", {"eventVersion": 1})
    assert server._subagent_event_versions[ws] == 1


async def test_relay_negotiation_expires_and_failed_reads_cannot_subscribe(history):
    channel = WebChannel(WebChannelConfig(), MessageBus())
    ws = AsyncMock()
    await channel._handle_feature_rpc(ws, "1", "missing", "subagents.get", {"runId": "missing", "eventVersion": 2})
    assert not channel._subagent_observers
    assert not channel._subagent_event_versions
    await channel._handle_feature_rpc(ws, "2", "reader", "subagents.list", {"eventVersion": 2})
    assert channel._subagent_event_versions == {"reader": 2}
    await channel._handle_feature_rpc(ws, "3", "reader", "subagents.list", {})
    assert channel._subagent_event_versions == {"reader": 2}
    channel._subagent_observers["reader"] = 0
    await channel._handle_feature_rpc(ws, "4", "reader", "subagents.list", {})
    assert not channel._subagent_event_versions
    assert "reader" in channel._subagent_observers


@pytest.mark.parametrize("version", [None, True, False, "2", 2.0, 0, 3, [], {}])
@pytest.mark.parametrize("method", ["subagents.list", "subagents.get"])
async def test_invalid_version_is_rejected_before_transport_subscription(history, version, method):
    with pytest.raises(feature_rpc.FeatureRpcError) as exc:
        await feature_rpc.dispatch(method, {"eventVersion": version, "runId": history.all()[0].run_id})
    assert exc.value.code == "INVALID"


async def test_relay_observer_capacity_does_not_leak_version_entries(history):
    channel = WebChannel(WebChannelConfig(), MessageBus())
    ws = AsyncMock()
    for index in range(66):
        await channel._handle_feature_rpc(ws, str(index), str(index), "subagents.list", {"eventVersion": 2})
    assert len(channel._subagent_observers) == len(channel._subagent_event_versions) == 64
    assert "65" not in channel._subagent_event_versions


async def test_relay_browser_disconnect_resets_negotiation(history):
    channel = WebChannel(WebChannelConfig(), MessageBus())
    ws = AsyncMock()
    await channel._handle_feature_rpc(ws, "1", "reader", "subagents.list", {"eventVersion": 2})
    await channel._handle_relay_message(ws, {"type": "browser-disconnected", "sessionId": "reader"})
    assert not channel._subagent_observers
    assert not channel._subagent_event_versions
    await channel._handle_feature_rpc(ws, "2", "reader", "subagents.list", {})
    channel._ws = ws
    ws.reset_mock()
    await channel.send_subagent_event("subagent.started", feature_rpc.subagents_list({})["tasks"][0])
    frame = json.loads(ws.send.call_args.args[0])
    assert "schemaVersion" not in frame["data"]


async def test_relay_reconnect_clears_all_browser_negotiations(history, monkeypatch):
    monkeypatch.delenv("MOLTBOT_PROXY_JWT_SECRET", raising=False)
    channel = WebChannel(WebChannelConfig(auth_token="test-only-" * 5), MessageBus())
    ws = AsyncMock()
    for reader in ("first", "second"):
        await channel._handle_feature_rpc(ws, reader, reader, "subagents.list", {"eventVersion": 2})
    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield ws  # empty receive iterator; no real network or credentials
    monkeypatch.setattr("flowly.channels.web.websockets.connect", connect)
    await channel._connect_and_run()
    assert not channel._subagent_observers
    assert not channel._subagent_event_versions


async def test_gateway_discarded_socket_does_not_leave_negotiation_state(history):
    server = gateway()
    ws = web.WebSocketResponse()
    await server._handle_feature_rpc(ws, "1", "subagents.list", {"eventVersion": 2})
    assert len(server._subagent_event_versions) == 1
    server._ws_rpc_reply.reset_mock()  # release mock call history's socket reference
    del ws
    gc.collect()
    assert not server._subagent_event_versions


@pytest.mark.parametrize("transport", ["gateway", "relay"])
async def test_primary_opt_in_does_not_change_profile_protocol_or_routing(history, transport):
    data = {**feature_rpc.subagents_list({})["tasks"][0], "sessionKey": "desktop:parent"}
    envelope = {"profile": "writer", "type": "subagent.progress", "data": data}
    if transport == "gateway":
        server = gateway()
        ws = web.WebSocketResponse()
        server._ws_clients = {"reader": ws, "unrelated": web.WebSocketResponse()}
        server._subagent_event_versions[ws] = 2
        server._profile_run_subscriptions = {}
        server._profile_client_subscriptions = {
            "reader": SimpleNamespace(directory=False, profiles={"writer"}, conversations={("writer", "desktop:parent")}),
            "unrelated": SimpleNamespace(directory=False, profiles={"other"}, conversations={("other", "desktop:parent")}),
        }
        await server._broadcast_profile_host_event(envelope)
        server._ws_send.assert_awaited_once()
        assert server._ws_send.call_args.args[0] is ws
        frame = server._ws_send.call_args.args[1]
    else:
        channel = WebChannel(WebChannelConfig(), MessageBus())
        channel._subagent_event_versions["reader"] = 2
        channel._bind_profile_conversation("writer", "desktop:parent", "reader")
        channel._bind_profile_conversation("other", "desktop:parent", "unrelated")
        channel._observe_profile_subagents("reader", "writer", {})
        channel._ws = AsyncMock()
        await channel._forward_profile_event(envelope)
        channel._ws.send.assert_awaited_once()
        frame = json.loads(channel._ws.send.call_args.args[0])
        assert frame["sessionId"] == "reader"
    assert frame["data"]["profile"] == "writer"
    assert frame["data"]["type"] == "subagent.started"
    assert frame["data"]["data"]["runId"] == data["runId"][:8]
    assert "schemaVersion" not in frame["data"]["data"]
    assert envelope["data"]["runId"] == data["runId"]  # shared envelope unchanged
