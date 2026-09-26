"""Real loopback WebSocket coverage for the outbound screen bridge."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType, web

from flowly.desktop_runtime.relay_stream import (
    MAX_FRAME,
    PROTOCOL,
    RelayScreenStreams,
    gateway_endpoint,
    relay_endpoint,
)


def request(**overrides):
    return {"type": "screen.stream.open", "protocol": PROTOCOL,
            "channelId": "a" * 43, "serverId": "server", "userId": "user",
            "profileId": "main", "hostId": "host", "botId": "bot",
            "generation": "generation", "ticket": "one-use-ticket",
            "inputProtocol": "epoch-u64-v1", **overrides}


@asynccontextmanager
async def server(path, handler):
    app = web.Application()
    app.router.add_get(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        await runner.cleanup()


@asynccontextmanager
async def bridge_fixture(*, reject_local=False):
    local_frames, relay_frames = asyncio.Queue(), asyncio.Queue()
    local_open, relay_open = asyncio.Queue(), asyncio.Queue()
    headers = {}

    async def local_handler(req):
        headers["local"] = dict(req.headers)
        if reject_local:
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        await local_open.put(ws)
        async for frame in ws:
            await local_frames.put(frame)
        await local_frames.put(None)
        return ws

    async def relay_handler(req):
        headers["relay"] = dict(req.headers)
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        await relay_open.put(ws)
        async for frame in ws:
            await relay_frames.put(frame)
        await relay_frames.put(None)
        return ws

    async with server("/api/display/stream", local_handler) as local_port:
        async with server("/screen/agent", relay_handler) as relay_port:
            state = SimpleNamespace(current=True)
            gateway = SimpleNamespace(host="0.0.0.0", port=local_port,
                                      _require_auth=True, _auth_token="local-secret")
            bridge = RelayScreenStreams(
                relay_url=f"ws://127.0.0.1:{relay_port}/relay", server_id="server",
                token=lambda: "fresh-relay-secret", gateway=gateway,
                current=lambda: state.current,
            )
            try:
                yield SimpleNamespace(bridge=bridge, state=state, headers=headers,
                                      local_open=local_open, relay_open=relay_open,
                                      local_frames=local_frames, relay_frames=relay_frames)
            finally:
                await bridge.close()


async def take(queue):
    return await asyncio.wait_for(queue.get(), 2)


@pytest.mark.parametrize("url", ["http://localhost/relay", "ws://example.com/relay",
    "wss://example.com/other", "wss://user:pass@example.com/relay",
    "wss://example.com/relay?token=secret", "wss://example.com/relay#fragment"])
def test_relay_origin_restrictions(url):
    with pytest.raises(ValueError):
        relay_endpoint(url)


def test_fixed_endpoints_and_authenticated_gateway():
    assert relay_endpoint("wss://example.com/relay") == "wss://example.com/screen/agent"
    gateway = SimpleNamespace(host="::", port=1234, _require_auth=True, _auth_token="secret")
    assert gateway_endpoint(gateway) == ("ws://[::1]:1234/api/display/stream", "secret")
    gateway._require_auth = False
    assert gateway_endpoint(gateway) is None
    gateway._require_auth = True
    gateway.host = "remote.example.com"
    assert gateway_endpoint(gateway) is None


async def test_binary_roundtrip_and_separate_credentials():
    async with bridge_fixture() as f:
        assert f.bridge.open(request())
        local, remote = await take(f.local_open), await take(f.relay_open)
        await local.send_bytes(b"RFB\x00screen")
        frame = await take(f.relay_frames)
        assert frame.type == WSMsgType.BINARY and frame.data == b"RFB\x00screen"
        await remote.send_bytes(b"\x00\x01input")
        assert (await take(f.local_frames)).data == b"\x00\x01input"
        assert f.headers["local"]["Authorization"] == "Bearer local-secret"
        assert f.headers["local"]["X-Flowly-Screen-Ticket"] == "one-use-ticket"
        assert f.headers["local"]["X-Flowly-Screen-Input"] == "epoch-u64-v1"
        assert f.headers["relay"]["Authorization"] == "Bearer fresh-relay-secret"
        assert f.headers["relay"]["X-Flowly-Screen-Channel"] == "a" * 43
        assert "X-Flowly-Screen-Ticket" not in f.headers["relay"]
        await f.bridge.close()
        assert await take(f.local_frames) is None
        assert await take(f.relay_frames) is None
        assert not f.bridge.open(request(channelId="b" * 43))


@pytest.mark.parametrize("overrides", [{"serverId": "other"}, {"channelId": "short"},
    {"ticket": "bad\r\nheader"}, {"url": "ws://attacker/"}, {"inputProtocol": "unknown"},
    {"profileId": ""}, {"generation": "wrong whitespace"}, {"userId": None}])
async def test_invalid_admission_opens_no_sockets(overrides):
    async with bridge_fixture() as f:
        assert not f.bridge.open(request(**overrides))
        assert not f.bridge._tasks
        assert not f.headers


async def test_local_ticket_rejection_never_attaches_relay_or_retries():
    async with bridge_fixture(reject_local=True) as f:
        assert f.bridge.open(request())
        await asyncio.gather(*f.bridge._tasks.values(), return_exceptions=True)
        assert "local" in f.headers and "relay" not in f.headers
        assert not f.bridge.open(request())


@pytest.mark.parametrize("reason", ["text", "oversize", "stale", "remote-close"])
async def test_invalid_or_stale_stream_closes_both_sides_without_input(reason):
    async with bridge_fixture() as f:
        assert f.bridge.open(request())
        await take(f.local_open)
        remote = await take(f.relay_open)
        if reason == "text":
            await remote.send_str("not RFB")
        elif reason == "oversize":
            await remote.send_bytes(b"x" * (MAX_FRAME + 1))
        elif reason == "stale":
            f.state.current = False
            await remote.send_bytes(b"stale input")
        else:
            await remote.close()
        assert await take(f.local_frames) is None
        assert await take(f.relay_frames) is None
        assert not f.bridge.open(request())


async def test_four_stream_limit_and_no_duplicate_channel():
    async with bridge_fixture() as f:
        for char in "abcd":
            assert f.bridge.open(request(channelId=char * 43))
        assert not f.bridge.open(request(channelId="e" * 43))
        assert not f.bridge.open(request(channelId="a" * 43))
        await f.bridge.close()
        assert not f.bridge._tasks


@pytest.mark.parametrize("stalled_side", ["local", "relay"])
async def test_stalled_upgrade_times_out_and_releases_capacity(monkeypatch, stalled_side):
    import flowly.desktop_runtime.relay_stream as module

    monkeypatch.setattr(module, "CONNECT_TIMEOUT", 0.05)
    release = asyncio.Event()
    entered = asyncio.Event()
    local_closed = asyncio.Event()

    async def stalled(req):
        entered.set()
        await release.wait()
        return web.Response()

    async def admitted(req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        async for _ in ws:
            pass
        local_closed.set()
        return ws

    async with server("/api/display/stream", stalled if stalled_side == "local" else admitted) as port:
        async with server("/screen/agent", stalled) as relay_port:
            bridge = RelayScreenStreams(
                relay_url=f"ws://127.0.0.1:{relay_port}/relay", server_id="server",
                token=lambda: "secret", current=lambda: True,
                gateway=SimpleNamespace(host="127.0.0.1", port=port,
                                        _require_auth=True, _auth_token="secret"))
            try:
                assert bridge.open(request())
                tasks = tuple(bridge._tasks.values())
                await asyncio.wait_for(entered.wait(), 1)
                results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
                assert isinstance(results[0], TimeoutError)
                assert not bridge._tasks
                if stalled_side == "relay":
                    await asyncio.wait_for(local_closed.wait(), 1)
            finally:
                release.set()
                await bridge.close()


async def test_web_channel_negotiates_only_current_available_transport():
    import json
    from unittest.mock import AsyncMock, Mock

    from flowly.bus.queue import MessageBus
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig

    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    ws, stale = SimpleNamespace(send=AsyncMock()), SimpleNamespace(send=AsyncMock())
    channel._ws = ws
    transport = SimpleNamespace(available=True, open=Mock())
    channel._screen_streams = transport
    ready = {"type": "ready", "capabilities": [PROTOCOL]}
    await channel._handle_relay_message(stale, ready)
    stale.send.assert_not_called()
    await channel._handle_relay_message(ws, {"type": "ready"})
    ws.send.assert_not_called()
    transport.available = False
    await channel._handle_relay_message(ws, ready)
    ws.send.assert_not_called()
    transport.available = True
    await channel._handle_relay_message(ws, ready)
    assert json.loads(ws.send.call_args.args[0]) == {"type": "screen.transport.ready", "protocol": PROTOCOL}
    await channel._handle_relay_message(stale, request())
    transport.open.assert_not_called()
    await channel._handle_relay_message(ws, request())
    transport.open.assert_called_once_with(request())


@pytest.mark.parametrize("flush_fails", [False, True])
async def test_control_lifecycle_closes_streams_even_when_queue_flush_fails(monkeypatch, flush_fails):
    from unittest.mock import AsyncMock

    import jwt

    import flowly.channels.web as module
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import WebChannelConfig

    channel = module.WebChannel(config=WebChannelConfig(
        enabled=True, relay_url="ws://127.0.0.1:1234/relay", server_id="server",
        jwt_secret="test-signing-secret-with-at-least-32-bytes"), bus=MessageBus())
    channel.set_screen_gateway(SimpleNamespace(host="127.0.0.1", port=4321,
                                              _require_auth=True, _auth_token="local-secret"))
    seen = []

    class Control:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    monkeypatch.setattr(module.websockets, "connect", lambda *args, **kwargs: Control())

    async def flush():
        stream = channel._screen_streams
        assert stream.available
        seen.append(stream)
        decoded = jwt.decode(stream._token(), channel.config.jwt_secret,
                             algorithms=["HS256"], audience="moltbot-proxy", issuer="flowly")
        assert decoded["serverId"] == "server" and decoded["type"] == "agent"
        assert decoded["exp"] - decoded["iat"] == 60
        if flush_fails:
            raise RuntimeError("fixture queue failure")

    channel._flush_outbound_queue = AsyncMock(side_effect=flush)
    if flush_fails:
        with pytest.raises(RuntimeError, match="fixture queue failure"):
            await channel._connect_and_run()
    else:
        await channel._connect_and_run()
    assert len(seen) == 1 and not seen[0].available
    assert channel._screen_streams is None
    assert channel._ws is None
