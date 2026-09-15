"""The SSH management lane never inherits unauthenticated localhost trust."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from flowly.gateway import mcp_management as transport
from flowly.profile_host_contract import ProfileHostError


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
    {"method": "mcp.capabilities", "profile": "../work", "expectedHostId": "host-id", "expectedBotId": "bot-id"},
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
        response = await client.post("/api/mcp/manage", json={
            "method": "mcp.capabilities", "profile": "work",
            "expectedHostId": "host-id", "expectedBotId": "bot-id",
        },
                                     headers={"Authorization": "Bearer token"})
        assert response.status == 200
    host.dispatch.assert_awaited_once_with("profiles.rpc", {
        "name": "work", "method": "mcp.capabilities", "params": {},
        "expectedHostId": "host-id", "expectedBotId": "bot-id",
    })


@pytest.mark.parametrize("payload", [
    {"method": "mcp.capabilities", "profile": "work"},
    {"method": "mcp.capabilities", "profile": "work", "expectedHostId": "host-id"},
    {"method": "mcp.capabilities", "expectedHostId": "host-id", "expectedBotId": "bot-id"},
])
async def test_profile_routing_requires_complete_identity(payload):
    host = SimpleNamespace(dispatch=AsyncMock())
    app = web.Application()
    transport.register_mcp_management(app, SimpleNamespace(_auth_token="token", _profile_host=host))
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/mcp/manage", json=payload,
                                     headers={"Authorization": "Bearer token"})
        assert response.status == 400
    host.dispatch.assert_not_awaited()


async def test_profile_identity_error_is_preserved_without_private_diagnostics():
    host = SimpleNamespace(dispatch=AsyncMock(side_effect=ProfileHostError(
        "PROFILE_IDENTITY_CHANGED", "The selected profile identity changed.",
    )))
    app = web.Application()
    transport.register_mcp_management(app, SimpleNamespace(_auth_token="token", _profile_host=host))
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/mcp/manage", json={
            "method": "mcp.setup.begin", "profile": "work",
            "expectedHostId": "host-id", "expectedBotId": "retired-bot-id",
        }, headers={"Authorization": "Bearer token"})
        assert response.status == 409
        assert (await response.json())["error"] == {
            "code": "PROFILE_IDENTITY_CHANGED",
            "message": "The selected profile identity changed.",
            "retryable": False,
        }


@pytest.mark.parametrize(
    "error,expected_status,expected_retryable",
    [
        (ProfileHostError("PROFILE_NOT_FOUND", "The selected profile no longer exists."), 404, False),
        (ProfileHostError("PROFILE_OFFLINE", "The profile runtime is offline.", retryable=True), 503, True),
    ],
)
async def test_profile_error_http_status_preserves_machine_contract(
    error, expected_status, expected_retryable,
):
    host = SimpleNamespace(dispatch=AsyncMock(side_effect=error))
    app = web.Application()
    transport.register_mcp_management(app, SimpleNamespace(_auth_token="token", _profile_host=host))
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/mcp/manage", json={
            "method": "mcp.connections.list", "profile": "work",
            "expectedHostId": "host-id", "expectedBotId": "bot-id",
        }, headers={"Authorization": "Bearer token"})
        payload = await response.json()
    assert response.status == expected_status
    assert payload["error"]["code"] == error.code
    assert payload["error"]["retryable"] is expected_retryable
    assert response.headers["Cache-Control"] == "no-store"


async def test_profile_management_reaches_identity_guarded_runtime(monkeypatch):
    import flowly.profile_host as profile_module
    from flowly.profile_host import ProfileHost

    monkeypatch.setattr(profile_module, "_public_profile", lambda name: {"name": name, "botId": "bot-id"})
    host = ProfileHost()
    host._target_rpc = AsyncMock(return_value={"servers": []})
    app = web.Application()
    transport.register_mcp_management(app, SimpleNamespace(_auth_token="token", _profile_host=host))
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/mcp/manage", json={
            "method": "mcp.connections.list", "profile": "work",
            "expectedHostId": host.host_id, "expectedBotId": "bot-id",
        }, headers={"Authorization": "Bearer token"})
        assert response.status == 200
        assert (await response.json())["result"] == {"servers": []}
    host._target_rpc.assert_awaited_once_with(
        "work", "mcp.connections.list", {}, 30.0, expected_bot_id="bot-id",
    )


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
    payload = await response.json()
    assert payload["error"]["retryable"] is True
    assert "private-password" not in str(payload)
