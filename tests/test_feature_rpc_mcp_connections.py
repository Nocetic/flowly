"""Authenticated owner RPC contract; no real accounts or external calls."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from flowly.channels import feature_rpc
from flowly.mcp.connections import MCPConnectionService


@pytest.fixture
async def service(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    instance = MCPConnectionService(tmp_path / "config.json", lambda: object(),
        probe=AsyncMock(return_value=(True, ["read", "write"], "")),
        apply=AsyncMock(return_value={"ok": True, "connected": True, "state": "connected"}))
    monkeypatch.setattr(feature_rpc, "_mcp_connection_service", instance)
    yield instance
    await instance.close()


async def call(method, params=None):
    result, restart = await feature_rpc.dispatch(method, params or {})
    assert restart is False
    return result


async def test_methods_and_capabilities_negotiate_without_runtime(monkeypatch):
    monkeypatch.setattr(feature_rpc, "_mcp_connection_service", None)
    capabilities = await call("mcp.capabilities")
    assert capabilities["version"] == 1
    assert capabilities["connectionSetup"] is False
    assert capabilities["oauthCallbackModes"] == []
    assert capabilities["oauthRedirectUris"] == {}
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await call("mcp.setup.begin", {})
    assert error.value.code == "UNAVAILABLE"


@pytest.mark.parametrize("available", [True, False])
async def test_mobile_callback_capabilities_match_native_oauth_availability(service, monkeypatch, available):
    from flowly.mcp import oauth
    from flowly.mcp.oauth_handoff import ANDROID_OAUTH_REDIRECT_URI, IOS_OAUTH_REDIRECT_URI

    monkeypatch.setattr(oauth, "oauth_available", lambda: available)
    capabilities = await call("mcp.capabilities")
    assert capabilities["nativeOAuth"] is available
    assert capabilities["oauthCallbackModes"] == (
        ["desktop_loopback", "ios_https", "android_https"] if available else []
    )
    assert capabilities["oauthRedirectUris"] == (
        {"ios": IOS_OAUTH_REDIRECT_URI, "android": ANDROID_OAUTH_REDIRECT_URI} if available else {}
    )


async def test_setup_surface_is_short_lived_and_never_requests_gateway_restart(service):
    capabilities = await call("mcp.capabilities")
    assert capabilities["connectionSetup"] is True
    assert capabilities["liveReconfigure"] is True
    params = {"name": "demo", "requestId": "unique-request-id-0001", "config": {"command": "demo"}}
    started = await call("mcp.setup.begin", params)
    await asyncio.sleep(0)
    status = await call("mcp.setup.status", {"id": started["id"]})
    assert status["phase"] == "review"
    assert not service.manager.store.path.exists()
    confirmed = await call("mcp.setup.confirm", {"id": started["id"], "permissions": {"mode": "selected", "include": ["read"]}})
    assert confirmed["phase"] == "committing"
    await service.manager.get(started["id"]).task
    status = await call("mcp.setup.status", {"id": started["id"]})
    assert status["phase"] == "complete"
    assert status["runtime"]["connected"] is True
    assert (await call("mcp.setup.pending"))["operations"][0]["id"] == started["id"]


async def test_listing_has_no_connection_secrets(service):
    secret = "sensitive-value-123456"
    service.manager.store.path.write_text(json.dumps({"mcpServers": {
        "remote": {"url": "https://example.com/mcp?token=" + secret, "headers": {"Authorization": secret}},
        "local": {"command": "demo", "args": ["--password", secret], "env": {"API_KEY": secret}},
    }}))
    result = await call("mcp.connections.list")
    assert secret not in json.dumps(result)
    remote = next(row for row in result["servers"] if row["name"] == "remote")
    assert remote["connected"] is False
    assert remote["runtimeState"] == "not_started"
    assert remote["permissions"]["mode"] == "legacy"


async def test_invalid_callback_is_structured_and_does_not_cancel_signin(service):
    # Block the probe explicitly; it must remain cancellable by the owner.
    async def pending(*args, **kwargs):
        await asyncio.Event().wait()
    service.manager.probe = pending
    started = await call("mcp.setup.begin", {"name": "demo", "requestId": "unique-request-id-0001", "config": {"url": "https://example.com/mcp", "auth": "oauth"}, "redirectUri": "http://127.0.0.1:54321/mcp/oauth/callback/" + "a" * 32})
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await call("mcp.setup.callback", {"id": started["id"], "callback": {"code": "private", "state": "bad"}})
    assert error.value.code == "OAUTH_INVALID"
    assert "private" not in error.value.message
    assert (await call("mcp.setup.cancel", {"id": started["id"]}))["phase"] == "cancelled"
    assert not service.manager.store.path.exists()


async def test_close_cancels_unsigned_setup_and_rejects_new_requests(service):
    params = {"name": "demo", "requestId": "unique-request-id-0001", "config": {"command": "demo"}}
    started = await call("mcp.setup.begin", params)
    await asyncio.sleep(0)
    await service.close()
    assert service.manager.get(started["id"]).phase == "cancelled"
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await call("mcp.setup.begin", params)
    assert error.value.code == "UNAVAILABLE"


async def test_instances_do_not_share_operations_or_configuration(service, tmp_path):
    other = MCPConnectionService(tmp_path / "other" / "config.json", lambda: object(), probe=AsyncMock(), apply=AsyncMock())
    started = await call("mcp.setup.begin", {"name": "demo", "requestId": "unique-request-id-0001", "config": {"command": "demo"}})
    from flowly.mcp.setup import MCPSetupError
    with pytest.raises(MCPSetupError) as error:
        await other.invoke("status", {"id": started["id"]})
    assert error.value.code == "NOT_FOUND"
    await other.close()
