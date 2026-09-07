"""External access uses the selected live runtime; no real accounts or mail."""

import hashlib
import json
import secrets
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from flowly.agent.tool_context import current_tool_origin
from flowly.agent.tools.base import Tool
from flowly.agent.tools.registry import ToolRegistry
from flowly.channels import feature_rpc
from flowly.gateway.server import GatewayServer
from flowly.mcp.connections import MCPConnectionService
from flowly.mcp.external_access import ExternalAccessError
from flowly.mcp.server.external_service import ExternalMCPService
from flowly.mcp.server.tool_runtime import RuntimeToolBridge
from flowly.session.manager import SessionManager


class SearchTool(Tool):
    name = "web_search"
    description = "Temporary profile-context echo"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self):
        return json.dumps({"session": current_tool_origin().session_key})


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("web:owner")
    session.add_message("user", "Private first profile message")
    sessions.save(session)
    (tmp_path / "config.json").write_text(json.dumps({"channels": {"email": {"enabled": False}}}))
    registry = ToolRegistry(availability_cache_ttl=0)
    registry.register(SearchTool())
    owner = SimpleNamespace(tools=registry, sessions=sessions, workspace=tmp_path, _resolve_toolset_route=lambda _: (None, None))
    bridge = RuntimeToolBridge(owner)
    service = ExternalMCPService(tmp_path / "mcp-access.json", bridge)
    yield service
    await service.close()
    await bridge.close()


async def issue(service, names):
    token = "flm_" + secrets.token_hex(32)
    row = await service.owner("create", {
        "id": secrets.token_hex(16), "label": "Temporary peer", "sessionKey": "web:owner", "tools": names,
        "tokenDigest": hashlib.sha256(token.encode()).hexdigest(), "ttlSeconds": 3600,
    })
    return token, row


async def test_read_and_event_scope_remain_bound_after_environment_changes(runtime, tmp_path, monkeypatch):
    token, _ = await issue(runtime, ["messages_read", "events_poll", "channels_list"])
    other = tmp_path / "other"
    monkeypatch.setenv("FLOWLY_HOME", str(other))
    sessions = SessionManager(other)
    second = sessions.get_or_create("web:owner")
    second.add_message("user", "Other profile secret")
    sessions.save(second)
    result = await runtime.call(token, "messages_read", {"session_key": "web:owner"})
    assert "Private first profile message" in json.dumps(result)
    assert "Other profile secret" not in json.dumps(result)
    await runtime.call(token, "events_poll", {})
    assert (tmp_path / "mcp" / "conversation-events.sqlite").exists()
    assert not (other / "mcp").exists()
    channels = await runtime.call(token, "channels_list", {})
    assert not channels["isError"]


async def test_live_tool_uses_owning_context_and_dynamic_availability(runtime):
    token, _ = await issue(runtime, ["web_search"])
    result = await runtime.call(token, "web_search", {})
    assert result["structuredContent"] == {"session": "web:owner"}
    assert runtime.bridge._grants == {}
    runtime.bridge.owner.tools.unregister("web_search")
    assert await runtime.list_tools(token) == []
    with pytest.raises(Exception, match="permitted"):
        await runtime.call(token, "web_search", {})


async def test_owner_rpc_exposes_only_scoped_access_and_never_restarts(runtime, monkeypatch, tmp_path):
    connection = MCPConnectionService(tmp_path / "config.json", lambda: runtime.bridge.owner.tools, external_provider=lambda: runtime)
    monkeypatch.setattr(feature_rpc, "_mcp_connection_service", connection)
    try:
        caps, restart = await feature_rpc.dispatch("mcp.capabilities", {})
        assert caps["externalAgentAccess"] is True and restart is False
        token, row = await issue(runtime, ["messages_read"])
        listing, restart = await feature_rpc.dispatch("mcp.access.list", {})
        assert token not in json.dumps(listing) and "digest" not in json.dumps(listing)
        assert restart is False
        result, restart = await feature_rpc.dispatch("mcp.access.revoke", {"id": row["id"]})
        assert result["status"] == "revoked" and restart is False
        with pytest.raises(ExternalAccessError):
            runtime.authorize(token)
    finally:
        await connection.close()


async def test_gateway_native_mcp_uses_access_keys_not_gateway_admin(runtime, tmp_path):
    sent = []

    async def send(target, message):
        sent.append((target, message))
        return True

    before = (tmp_path / "config.json").read_bytes()
    gateway = GatewayServer(
        host="127.0.0.1", port=0, sessions=runtime.bridge.owner.sessions, on_send=send,
        auth_token="gateway-admin-" + "x" * 32, require_loopback_auth=True,
    )
    gateway._tool_bridge = runtime.bridge
    gateway._external_mcp_service = runtime
    await gateway.start()
    try:
        token, _ = await issue(runtime, ["web_search", "messages_read"])
        url = runtime.local_endpoint
        assert url == f"http://127.0.0.1:{gateway.port}/mcp"
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    assert {tool.name for tool in (await client.list_tools()).tools} == {"web_search", "messages_read"}
                    assert (await client.call_tool("web_search", {})).structured_content == {"session": "web:owner"}
                    assert (await client.call_tool("messages_send", {"target": "email:test", "message": "must not send"})).is_error
            assert (await http.post(url, headers={"Authorization": "Bearer " + gateway._auth_token}, json={})).status_code == 401
            assert (await http.options(url, headers={"Origin": "https://evil.test"})).status_code == 403
        assert sent == []
        assert (tmp_path / "config.json").read_bytes() == before
    finally:
        await gateway.stop()
