"""Relay display commands traverse the actual authenticated gateway."""

import asyncio
from types import SimpleNamespace

import pytest

from flowly.desktop_runtime.relay_rpc import RelayScreenRpc
from flowly.gateway.display import _resolve
from flowly.gateway.display_watch import DisplaySubscriptions
from tests.test_screen_display_gateway import display_gateway as _display_gateway

display_gateway = _display_gateway


@pytest.fixture
async def proxy(display_gateway):
    fixture = display_gateway
    fixture.gateway.port = fixture.server.port
    replies = asyncio.Queue()
    state = SimpleNamespace(current=True)
    manager = RelayScreenRpc(gateway=fixture.gateway, current=lambda: state.current,
                             send=replies.put)
    manager.enabled = True
    manager.connected("one")
    manager.connected("two")
    count = 0

    async def call(method, params=None, session="one", rpc_id=None):
        nonlocal count
        count += 1
        await manager.submit(session, {"id": rpc_id or f"request-{count}", "method": method,
                                       "params": params or {}})
        return await asyncio.wait_for(replies.get(), 3)

    try:
        yield SimpleNamespace(manager=manager, replies=replies, state=state, call=call, fixture=fixture)
    finally:
        await manager.close()


async def test_status_uses_authenticated_local_gateway_and_private_recipient(proxy):
    reply = await proxy.call("display.status", {"profileId": "writer"})
    assert reply["type"] == "screen.rpc" and reply["sessionId"] == "one"
    status = reply["message"]["result"]
    assert status["profileId"] == "writer" and status["generation"] == "generation-writer"
    assert proxy.manager.clients["one"].task is not None
    assert proxy.manager.clients["two"].task is None


async def test_profile_identity_rejection_is_preserved(proxy):
    reply = await proxy.call("display.observe", {"profileId": "writer", "expectedBotId": "wrong",
                                                "expectedHostId": "wrong", "generation": "generation-writer"})
    assert reply["message"]["error"]["code"] == "PROFILE_IDENTITY_CHANGED"


async def test_subscriptions_are_owned_by_each_browser_and_disconnect_preserves_control(proxy, monkeypatch):
    monkeypatch.setattr(DisplaySubscriptions, "INTERVAL", 0.01)
    status = (await proxy.call("display.status"))["message"]["result"]
    target = {"profileId": "default", "expectedHostId": status["hostId"], "expectedBotId": status["botId"]}
    watch = (await proxy.call("display.subscribe", target))["message"]["result"]
    assert watch["subscriptionId"] in proxy.fixture.gateway._display_subscriptions.entries
    rejected = await proxy.call("display.unsubscribe", {"subscriptionId": watch["subscriptionId"]}, session="two")
    assert rejected["sessionId"] == "two" and rejected["message"]["error"]["code"] == "UNAUTHORIZED"
    runtime = _resolve(proxy.fixture.gateway, {}).runtime
    human = runtime.authority.acquire("human", "generation-default", 1)
    events = [await asyncio.wait_for(proxy.replies.get(), 3) for _ in range(2)]
    assert all(event["sessionId"] == "one" for event in events)
    assert [event["message"]["event"] for event in events] == ["display.status", "display.control"]
    await proxy.manager.disconnected("one")
    async with asyncio.timeout(3):
        while proxy.fixture.gateway._display_subscriptions.entries:
            await asyncio.sleep(0.01)
    assert runtime.authority.snapshot() == human
    await proxy.manager.submit("one", {"id": "after-disconnect", "method": "display.status"})
    assert proxy.replies.empty()


async def test_non_display_methods_duplicate_ids_and_unannounced_browsers_rejected(proxy):
    rejected = await proxy.call("config.set", {"secret": "do-not-store"})
    assert rejected["message"]["error"]["code"] == "INVALID_PARAMS"
    assert proxy.manager.clients["one"].task is None
    await proxy.call("display.status", rpc_id="duplicate")
    duplicate = await proxy.call("display.status", rpc_id="duplicate")
    assert duplicate["message"]["error"]["code"] == "OPERATION_CONFLICT"
    await proxy.manager.submit("unannounced", {"id": "new", "method": "display.status"})
    assert "unannounced" not in proxy.manager.clients and proxy.replies.empty()


async def test_local_disconnect_fails_pending_without_replay(proxy, monkeypatch):
    monkeypatch.setattr(proxy.fixture.gateway, "_ws_credential_ok", lambda request: False)
    result = await proxy.call("display.status", rpc_id="uncertain")
    assert result["message"]["error"]["code"] == "CONTROL_UNAVAILABLE"
    # An admission failure cannot silently reconnect and repeat the request.
    await asyncio.sleep(0)
    repeated = await proxy.call("display.status", rpc_id="uncertain")
    assert repeated["message"]["error"]["code"] == "OPERATION_CONFLICT"


async def test_closed_or_stale_control_never_emits(proxy):
    proxy.state.current = False
    await proxy.manager.submit("one", {"id": "stale", "method": "display.status"})
    assert proxy.replies.empty()
    await proxy.manager.close()
    proxy.manager.connected("replacement")
    assert not proxy.manager.clients


async def test_deadline_bounds_unanswered_requests_and_duplicate_mutation_is_not_replayed(proxy, monkeypatch):
    import flowly.desktop_runtime.relay_rpc as module

    monkeypatch.setattr(module, "REQUEST_TIMEOUT", 0.05)
    arrived = asyncio.Event()
    calls = []

    async def ignore(ws, client_id, message):
        calls.append(message)
        arrived.set()

    monkeypatch.setattr(proxy.fixture.gateway, "_handle_ws_rpc", ignore)
    result = await proxy.call("display.start", {"operationId": "uncertain-op"}, rpc_id="uncertain-start")
    assert arrived.is_set()
    assert result["message"]["error"]["code"] == "CONTROL_UNAVAILABLE"
    repeated = await proxy.call("display.start", rpc_id="uncertain-start")
    assert repeated["message"]["error"]["code"] == "OPERATION_CONFLICT"
    assert len(calls) == 1


async def test_pending_limit_keeps_ninth_request_out_of_gateway(proxy, monkeypatch):
    import flowly.desktop_runtime.relay_rpc as module

    monkeypatch.setattr(module, "REQUEST_TIMEOUT", 60)
    received = []

    async def ignore(ws, client_id, message):
        received.append(message)

    monkeypatch.setattr(proxy.fixture.gateway, "_handle_ws_rpc", ignore)
    for number in range(9):
        await proxy.manager.submit("one", {"id": f"limit-{number}", "method": "display.status"})
    rejected = await asyncio.wait_for(proxy.replies.get(), 3)
    assert rejected["message"]["id"] == "limit-8"
    assert rejected["message"]["error"]["code"] == "SCREEN_BUSY"
    async with asyncio.timeout(3):
        while len(received) < 8:
            await asyncio.sleep(0.01)
    assert len(received) == 8


async def test_web_channel_routes_only_current_announced_browser(proxy):
    from unittest.mock import AsyncMock

    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig

    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    ws = SimpleNamespace(send=AsyncMock())
    channel._ws = ws
    channel._screen_rpc = proxy.manager
    await channel._handle_relay_message(ws, {"type": "browser-connected", "sessionId": "web-browser"})
    await channel._handle_relay_message(ws, {"type": "rpc", "id": "web-request", "method": "display.status",
                                           "sessionId": "web-browser", "params": {"profileId": "writer"}})
    response = await asyncio.wait_for(proxy.replies.get(), 3)
    assert response["sessionId"] == "web-browser"
    assert response["message"]["result"]["profileId"] == "writer"
    await channel._handle_relay_message(ws, {"type": "browser-disconnected", "sessionId": "web-browser"})
    assert "web-browser" not in proxy.manager.clients


async def test_session_and_socket_capacity_is_bounded(proxy, monkeypatch):
    release = asyncio.Event()

    async def hold(session, client):
        await release.wait()

    monkeypatch.setattr(proxy.manager, "_run", hold)
    for number in range(80):
        proxy.manager.connected(f"browser-{number}")
    assert len(proxy.manager.clients) == 64
    for number in range(17):
        await proxy.manager.submit(f"browser-{number}", {"id": "request", "method": "display.status"})
    reply = await asyncio.wait_for(proxy.replies.get(), 3)
    assert reply["sessionId"] == "browser-16" and reply["message"]["error"]["code"] == "SCREEN_BUSY"
    assert sum(client.task is not None for client in proxy.manager.clients.values()) == 16


async def test_old_local_reader_cannot_deliver_to_replacement_browser(proxy):
    old = proxy.manager.clients["one"]
    await proxy.manager.disconnected("one")
    proxy.manager.connected("one")
    await proxy.manager.emit("one", {"type": "rpc", "id": "old", "result": {"ticket": "old-secret"}}, old)
    assert proxy.replies.empty()


async def test_relay_capability_is_added_only_on_the_relay_delivery_path(proxy):
    direct = await proxy.fixture.rpc("display.status", {"profileId": "writer"})
    relayed = await proxy.call("display.status", {"profileId": "writer"})
    assert direct["result"]["transports"] == ["direct"]
    assert relayed["message"]["result"]["transports"] == ["direct", "relay"]
    status = relayed["message"]["result"]
    subscribed = await proxy.call("display.subscribe", {"profileId": "writer",
        "expectedHostId": status["hostId"], "expectedBotId": status["botId"]})
    assert subscribed["message"]["result"]["status"]["transports"] == ["direct", "relay"]
