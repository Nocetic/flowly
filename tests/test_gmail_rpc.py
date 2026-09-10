import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from typer.testing import CliRunner

from flowly.channels import feature_rpc, gmail_auth
from flowly.cli.gmail_cmd import gmail_app
from flowly.gateway import mcp_management
from flowly.integrations.gmail_connection import GmailConnection, atomic_private_json


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))


async def test_shared_rpc_capabilities_no_restart():
    result, restart = await feature_rpc.dispatch("gmail.capabilities", {})
    assert result["version"] == 1
    assert "gmail.setup.begin" in result["methods"]
    assert restart is False


@pytest.mark.parametrize("method,params", [
    ("gmail.status", {"home": "/other"}),
    ("gmail.setup.begin", {"issuer": "https://attacker.test"}),
    ("gmail.setup.status", {}),
    ("gmail.disconnect", {}),
    ("gmail.setup.begin", {"label": []}),
])
async def test_rpc_rejects_unsupported_fields(method, params):
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch(method, params)
    assert error.value.code == "INVALID_PARAMS"


async def test_rpc_runs_shared_service_and_redacts_failures(monkeypatch):
    monkeypatch.setattr(GmailConnection, "begin", lambda self, **kwargs: {"status": "pending"})
    assert await feature_rpc.dispatch("gmail.setup.begin", {"locale": "tr"}) == ({"status": "pending"}, False)
    def fail(self):
        raise RuntimeError("Google refresh-token secret")
    monkeypatch.setattr(GmailConnection, "status", fail)
    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        await feature_rpc.dispatch("gmail.status", {})
    assert "secret" not in error.value.message


async def test_gmail_http_uses_auth_and_its_own_allowlist():
    app = web.Application()
    mcp_management.register_gmail_management(app, SimpleNamespace(_auth_token="owner-token", _profile_host=None))
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/gmail/manage", json={"method": "gmail.capabilities"})).status == 401
        headers = {"Authorization": "Bearer owner-token"}
        response = await client.post("/api/gmail/manage", json={"method": "gmail.capabilities"}, headers=headers)
        assert response.status == 200
        assert (await response.json())["result"]["version"] == 1
        assert response.headers["Cache-Control"] == "no-store"
        for method in ["chat.send", "gmail.set_credentials", "mcp.setup.begin", "profiles.rpc"]:
            assert (await client.post("/api/gmail/manage", json={"method": method}, headers=headers)).status == 400
        headers["Origin"] = "https://attacker.test"
        assert (await client.post("/api/gmail/manage", json={"method": "gmail.capabilities"}, headers=headers)).status == 403


@pytest.mark.parametrize("method,params", [
    ("gmail.setup.begin", {}), ("profiles.rpc", {"name": "work", "method": "gmail.setup.begin"}),
])
async def test_plaintext_remote_ws_cannot_bypass_gmail_guard(method, params):
    from flowly.gateway.server import GatewayServer
    server = GatewayServer(auth_token="owner", advertise_control=False)
    server._ws_rpc_error = AsyncMock()
    server._handle_feature_rpc = AsyncMock()
    request = SimpleNamespace(secure=False, transport=Mock())
    request.transport.get_extra_info.return_value = ("192.0.2.1", 1234)
    ws = SimpleNamespace(_req=request)
    await server._handle_ws_rpc(ws, "client", {"method": method, "id": "1", "params": params})
    assert server._ws_rpc_error.await_args.args[2] == "SECURE_TRANSPORT_REQUIRED"
    server._handle_feature_rpc.assert_not_awaited()


def test_cli_prints_link_and_code_without_opening_browser(monkeypatch):
    monkeypatch.setattr(GmailConnection, "begin", lambda self, **kwargs: {
        "label": "Agent", "profile": "work", "verificationCode": "AABBCCDD",
        "authorizationUrl": "https://useflowlyapp.com/tr/gmail/connect?request=public", "requestId": "public",
    })
    opener = Mock()
    monkeypatch.setattr("flowly.cli.gmail_cmd.webbrowser.open", opener)
    result = CliRunner().invoke(gmail_app, ["connect", "--no-browser", "--no-wait", "--locale", "tr"])
    assert result.exit_code == 0, result.output
    assert "AABBCCDD" in result.output
    assert "useflowlyapp.com" in result.output
    opener.assert_not_called()


def test_cli_does_not_open_a_browser_on_ssh_host(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "remote")
    monkeypatch.setattr(GmailConnection, "begin", lambda self, **kwargs: {
        "label": "Agent", "profile": "work", "verificationCode": "AABBCCDD", "authorizationUrl": "https://useflowlyapp.com", "requestId": "public",
    })
    opener = Mock()
    monkeypatch.setattr("flowly.cli.gmail_cmd.webbrowser.open", opener)
    assert CliRunner().invoke(gmail_app, ["connect", "--no-wait"]).exit_code == 0
    opener.assert_not_called()


def test_legacy_credentials_and_dynamic_managed_readiness(tmp_path):
    path = tmp_path / "credentials" / "gmail.json"
    atomic_private_json(path, {"refresh_token": "legacy", "email": "old@example.test"})
    assert gmail_auth.load_credentials()["refresh_token"] == "legacy"
    assert gmail_auth.email_tool_ready(legacy_enabled=True)
    assert not gmail_auth.email_tool_ready(legacy_enabled=False)
    managed = {"mode": "flowly_broker", "issuer": "https://useflowlyapp.com", "grant_id": "a" * 32, "grant_secret": "b" * 43}
    atomic_private_json(path, managed)
    assert not gmail_auth.email_tool_ready()
    (tmp_path / "config.json").write_text(json.dumps({"channels": {"email": {"enabled": True}}}))
    assert gmail_auth.email_tool_ready()
    atomic_private_json(path, {**managed, "disconnect_pending": True})
    assert not gmail_auth.email_tool_ready()


async def test_probe_uses_profile_credentials_and_does_not_claim_file_means_connected(tmp_path, monkeypatch):
    from flowly.integrations.probes import probe_email
    assert (await probe_email({"enabled": True})).status == "not_configured"
    atomic_private_json(tmp_path / "credentials" / "gmail.json", {"refresh_token": "legacy"})
    monkeypatch.setattr(GmailConnection, "status", lambda self: {"connected": False})
    assert (await probe_email({"enabled": True})).status == "down"
    monkeypatch.setattr(GmailConnection, "status", lambda self: {"connected": True})
    assert (await probe_email({"enabled": True})).status == "ok"
    assert (await probe_email({"enabled": False})).status == "disabled"
