"""A loopback gateway exempts local clients, but never exempts the screen.

Relay hosts run the gateway on 127.0.0.1 without enforced auth. The relay screen
bridge must still reach the display routes, and only with the configured token.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

import flowly.profile as profiles
from flowly.desktop_runtime.launcher import ScreenRuntime
from flowly.desktop_runtime.relay_stream import gateway_endpoint
from flowly.gateway.display import STREAM_PATH
from flowly.gateway.server import GatewayServer

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux-host screen transport")
TOKEN = "loopback-display-token"


@pytest.fixture
async def loopback(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", home)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", home / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    monkeypatch.setattr(ScreenRuntime, "capabilities", staticmethod(lambda: {
        "supported": False, "reason": "DEPENDENCIES_MISSING", "missing": ["Xvnc"]}))
    servers = []

    async def start(token=TOKEN):
        gateway = GatewayServer(host="127.0.0.1", port=0, auth_token=token)
        gateway.on_chat_message = AsyncMock()
        server = TestServer(gateway._create_app())
        await server.start_server()
        servers.append(server)
        return gateway, server

    async with aiohttp.ClientSession() as client:
        async def rpc(server, query="", method="display.capabilities"):
            async with client.ws_connect(server.make_url("/ws" + query)) as socket:
                await socket.send_json({"type": "rpc", "id": "request", "method": method, "params": {}})
                return await socket.receive_json(timeout=5)

        yield SimpleNamespace(start=start, client=client, rpc=rpc)
    for server in servers:
        await server.close()


async def test_loopback_exemption_is_unchanged_but_the_screen_requires_the_token(loopback):
    gateway, server = await loopback.start()
    assert not gateway._require_auth, "other local clients keep the loopback exemption"
    denied = await loopback.rpc(server)
    assert denied["error"]["code"] == "UNAUTHORIZED"
    granted = await loopback.rpc(server, f"?token={TOKEN}")
    assert granted["result"]["installSupported"] is True
    assert (await loopback.rpc(server, "?token=wrong"))["error"]["code"] == "UNAUTHORIZED"


async def test_ticket_route_checks_the_token_itself_on_an_exempt_gateway(loopback):
    _, server = await loopback.start()
    url = server.make_url("/api/auth/ws-ticket")
    async with loopback.client.post(url) as response:
        assert response.status == 401
    async with loopback.client.post(url, headers={"Authorization": "Bearer wrong"}) as response:
        assert response.status == 401
    async with loopback.client.post(url, headers={"Authorization": f"Bearer {TOKEN}"}) as response:
        ticket = (await response.json())["ticket"]
    assert (await loopback.rpc(server, f"?ticket={ticket}"))["result"]["protocolVersion"] == 1
    # Single use: a replayed ticket connects as an ordinary local client only.
    assert (await loopback.rpc(server, f"?ticket={ticket}"))["error"]["code"] == "UNAUTHORIZED"


async def test_stream_route_demands_the_bearer_token_on_an_exempt_gateway(loopback):
    _, server = await loopback.start()
    async with loopback.client.get(server.make_url(STREAM_PATH)) as response:
        assert response.status == 401
    async with loopback.client.get(server.make_url(STREAM_PATH), headers={"Authorization": "Bearer wrong"}) as response:
        assert response.status == 401


async def test_without_a_token_the_screen_is_unreachable(loopback):
    gateway, server = await loopback.start(token="")
    assert (await loopback.rpc(server))["error"]["code"] == "UNAUTHORIZED"
    async with loopback.client.post(server.make_url("/api/auth/ws-ticket")) as response:
        assert response.status in {404, 405}
    assert gateway_endpoint(gateway) is None


async def test_relay_bridge_uses_the_exempt_loopback_gateway_with_its_token(loopback):
    gateway, server = await loopback.start()
    gateway.port = server.port
    url, token = gateway_endpoint(gateway)
    assert url.startswith("ws://127.0.0.1:") and url.endswith(STREAM_PATH) and token == TOKEN
    gateway.host = "0.0.0.0"
    assert gateway_endpoint(gateway)[0].startswith("ws://127.0.0.1:")
    gateway.host = "192.0.2.10"
    assert gateway_endpoint(gateway) is None
