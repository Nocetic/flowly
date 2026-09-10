"""The SSH management lane never inherits unauthenticated localhost trust."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from flowly.gateway import mcp_management as transport


@pytest.fixture
async def client(monkeypatch):
    gateway = SimpleNamespace(_auth_token="owner-token", _profile_host=None)
    app = web.Application()
    transport.register_mcp_management(app, gateway)
    async with TestClient(TestServer(app)) as instance:
        yield instance


@pytest.mark.parametrize("token", [None, "", "wrong"])
async def test_loopback_requires_token(client, token):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    response = await client.post("/api/mcp/manage", json={"method": "mcp.capabilities"}, headers=headers)
    assert response.status == 401


async def test_real_capability_dispatch(client):
    response = await client.post("/api/mcp/manage", json={"method": "mcp.capabilities"},
                                 headers={"Authorization": "Bearer owner-token"})
    assert response.status == 200
    assert (await response.json())["result"]["version"] == 1
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("payload", [
    {"method": "chat.send"}, {"method": "profiles.rpc", "params": {"method": "config.set"}},
    {"method": "mcp.capabilities", "params": []}, {"method": []}, [],
    {"method": "mcp.capabilities", "profile": ""},
    {"method": "mcp.capabilities", "url": "http://other"},
])
async def test_rejects_other_surfaces(client, payload):
    response = await client.post("/api/mcp/manage", json=payload, headers={"Authorization": "Bearer owner-token"})
    assert response.status == 400


async def test_limits_body(client):
    response = await client.post("/api/mcp/manage", data=b"x" * (transport.MAX_BODY + 1),
                                 headers={"Authorization": "Bearer owner-token"})
    assert response.status == 413


async def test_cross_origin_blocked(client):
    response = await client.post("/api/mcp/manage", json={"method": "mcp.capabilities"},
        headers={"Authorization": "Bearer owner-token", "Origin": "https://untrusted.example"})
    assert response.status == 403


@pytest.mark.parametrize("peer,tls,expected", [
    ("127.0.0.1", False, True), ("::1", False, True),
    ("::ffff:127.0.0.1", False, True), ("192.0.2.1", False, False),
    ("192.0.2.1", True, True),
])
def test_only_socket_proves_secure_transport(peer, tls, expected):
    request = SimpleNamespace(secure=tls, transport=Mock(), headers={"X-Forwarded-Proto": "https"})
    request.transport.get_extra_info.return_value = (peer, 1234)
    assert transport._protected_socket(request) is expected


async def test_profile_routing_preserves_mcp_allowlist():
    host = SimpleNamespace(dispatch=AsyncMock(return_value={"version": 1}))
    app = web.Application()
    transport.register_mcp_management(app, SimpleNamespace(_auth_token="token", _profile_host=host))
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/mcp/manage", json={"method": "mcp.capabilities", "profile": "work"},
                                     headers={"Authorization": "Bearer token"})
        assert response.status == 200
    host.dispatch.assert_awaited_once_with("profiles.rpc", {"name": "work", "method": "mcp.capabilities", "params": {}})


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
async def test_gateway_registration_keeps_health_public_but_management_authenticated(host):
    from flowly.gateway.server import GatewayServer

    gateway = GatewayServer(host=host, auth_token="owner-token", advertise_control=False)
    async with TestClient(TestServer(gateway._create_app())) as client:
        assert (await client.get("/health")).status == 200
        assert (await client.post("/api/mcp/manage", json={"method": "mcp.capabilities"})).status == 401
        response = await client.post("/api/mcp/manage", json={"method": "mcp.capabilities"},
                                     headers={"Authorization": "Bearer owner-token"})
        assert response.status == 200
        assert (await response.json())["result"]["version"] == 1


async def test_empty_configured_token_cannot_enable_management():
    app = web.Application()
    transport.register_mcp_management(app, SimpleNamespace(_auth_token="", _profile_host=None))
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/mcp/manage", json={"method": "mcp.capabilities"})
        assert response.status == 401


async def test_unexpected_dispatch_error_does_not_echo_credentials(client, monkeypatch):
    monkeypatch.setattr(transport.feature_rpc, "dispatch", AsyncMock(side_effect=RuntimeError("private-password")))
    response = await client.post("/api/mcp/manage", json={"method": "mcp.setup.begin"},
                                 headers={"Authorization": "Bearer owner-token"})
    assert response.status == 503
    assert "private-password" not in await response.text()
