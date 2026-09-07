import asyncio
import hashlib
import json
import secrets
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from aiohttp import web
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

from flowly.mcp.external_access import ExternalAccessError
from flowly.mcp.server.external_http import register_external_mcp_route
from flowly.mcp.server.external_service import ExternalMCPService
from flowly.session.manager import SessionManager


@pytest.fixture
async def peer(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    sessions = SessionManager(tmp_path)
    sessions.save(sessions.get_or_create("web:test"))
    base = MCPServer("temporary-test")
    calls = []
    started, cancelled = asyncio.Event(), asyncio.Event()

    @base.tool(structured_output=True)
    async def echo(value: str) -> dict[str, str]:
        calls.append(value)
        return {"echo": value}

    @base.tool()
    async def wait_forever() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    @base.tool()
    async def denied_write() -> str:
        calls.append("DENIED_WRITE_EXECUTED")
        return "bad"

    bridge = SimpleNamespace(
        owner=SimpleNamespace(sessions=sessions),
        available_tools=lambda _: [],
    )
    service = ExternalMCPService(tmp_path / "mcp-access.json", bridge, conversation_server=base)
    token = "flm_" + secrets.token_hex(32)
    credential = await service.owner("create", {
        "id": secrets.token_hex(16), "label": "Test peer", "sessionKey": "web:test", "tools": ["echo", "wait_forever"],
        "tokenDigest": hashlib.sha256(token.encode()).hexdigest(), "ttlSeconds": 3600,
    })
    app = web.Application()
    register_external_mcp_route(app, service)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/mcp"
    yield service, token, credential, url, calls, started, cancelled
    await runner.cleanup()


@pytest.mark.asyncio
async def test_native_sdk_scoped_http_round_trip_and_revocation(peer):
    service, token, credential, url, calls, _, _ = peer
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listed = await client.list_tools()
                assert {tool.name for tool in listed.tools} == {"echo", "wait_forever"}
                result = await client.call_tool("echo", {"value": "hello"})
                assert not result.is_error and result.structured_content == {"echo": "hello"}
                denied = await client.call_tool("denied_write", {})
                assert denied.is_error and calls == ["hello"]
                await service.owner("revoke", {"id": credential["id"]})
        assert (await http.post(url, json={})).status_code == 401


@pytest.mark.asyncio
async def test_revocation_cancels_running_call(peer):
    service, token, credential, _, _, started, cancelled = peer
    task = asyncio.create_task(service.call(token, "wait_forever", {}))
    await asyncio.wait_for(started.wait(), 2)
    await service.owner("revoke", {"id": credential["id"]})
    with pytest.raises(ExternalAccessError, match="withdrawn"):
        await task
    await asyncio.wait_for(cancelled.wait(), 2)
    assert not service._calls


@pytest.mark.asyncio
async def test_owner_deletion_cancels_running_call_even_after_recreation(peer):
    service, token, _, _, _, started, cancelled = peer
    task = asyncio.create_task(service.call(token, "wait_forever", {}))
    await asyncio.wait_for(started.wait(), 2)
    manager = service.bridge.owner.sessions
    manager.delete("web:test")
    manager.save(manager.get_or_create("web:test"))
    with pytest.raises(ExternalAccessError):
        await asyncio.wait_for(task, 2)
    await asyncio.wait_for(cancelled.wait(), 2)
    assert not service._calls


@pytest.mark.asyncio
async def test_owner_revocation_returns_native_mcp_error_without_hanging(peer):
    service, token, credential, url, _, started, cancelled = peer
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                task = asyncio.create_task(client.call_tool("wait_forever", {}))
                await asyncio.wait_for(started.wait(), 2)
                await service.owner("revoke", {"id": credential["id"]})
                result = await asyncio.wait_for(task, 2)
                assert result.is_error
                await asyncio.wait_for(cancelled.wait(), 2)
    assert not service._calls


@pytest.mark.asyncio
async def test_boundary_rejects_admin_token_origin_and_query_secrets(peer):
    _, token, _, url, _, _, _ = peer
    async with httpx.AsyncClient() as http:
        assert (await http.post(url, json={})).status_code == 401
        assert (await http.post(url, headers={"Authorization": "Bearer " + "a" * 64}, json={})).status_code == 401
        assert (await http.post(url, headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.test"}, json={})).status_code == 403
        assert (await http.post(url + "?token=x", headers={"Authorization": f"Bearer {token}"}, json={})).status_code == 403
        assert (await http.post(url, headers={"Authorization": f"Bearer {token}", "Host": "evil.test"}, json={})).status_code == 403
        assert (await http.post(url, headers={"Authorization": f"Bearer {token}", "Origin": ""}, json={})).status_code == 403
        assert (await http.post(url, headers={"Authorization": f"Bearer {token}", "Host": "remote.test", "X-Forwarded-Proto": "https"}, json={})).status_code == 403


async def test_http_disconnect_cancels_unfinished_invocation(peer):
    service, token, _, url, _, started, cancelled = peer
    address = urlsplit(url)
    _, writer = await asyncio.open_connection(address.hostname, address.port)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "wait_forever", "arguments": {}}}).encode()
    try:
        writer.write((f"POST /mcp HTTP/1.1\r\nHost: {address.netloc}\r\nAuthorization: Bearer {token}\r\n"
                      "Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n"
                      f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        await writer.drain()
        await asyncio.wait_for(started.wait(), 2)
    finally:
        writer.close()
        await writer.wait_closed()
    await asyncio.wait_for(cancelled.wait(), 2)
    for _ in range(10):
        if not service._calls:
            break
        await asyncio.sleep(0.01)
    assert not service._calls


async def test_gateway_shutdown_drains_external_calls_and_denies_new_ones(peer):
    service, token, _, _, _, started, cancelled = peer
    task = asyncio.create_task(service.call(token, "wait_forever", {}))
    await asyncio.wait_for(started.wait(), 2)
    await service.close()
    with pytest.raises(ExternalAccessError):
        await task
    await asyncio.wait_for(cancelled.wait(), 2)
    assert not service._calls
    with pytest.raises(ExternalAccessError, match="stopped"):
        service.authorize(token)


@pytest.mark.parametrize("protocol", ["auto", "legacy"])
async def test_public_stdio_adapter_preserves_remote_scope(peer, tmp_path, protocol):
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    _, token, _, url, calls, _, _ = peer
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "flowly", "mcp", "connect"],
        env={"FLOWLY_HOME": str(tmp_path / "isolated-client"), "FLOWLY_QUIET": "1",
             "FLOWLY_MCP_ENDPOINT": url, "FLOWLY_MCP_ACCESS_KEY": token},
    )
    async with Client(stdio_client(parameters), mode=protocol) as connected:
        assert {tool.name for tool in (await connected.session.list_tools()).tools} == {"echo", "wait_forever"}
        result = await connected.session.call_tool("echo", {"value": "stdio"})
        assert result.structured_content == {"echo": "stdio"}
        denied = await connected.session.call_tool("denied_write", {})
        assert denied.is_error
    assert calls == ["stdio"]


@pytest.mark.parametrize("endpoint", [
    "http://remote.test/mcp", "https://user:password@remote.test/mcp", "https://remote.test/mcp?key=x",
    "https://remote.test/mcp#x", "file:///mcp", "https://remote.test\\mcp", "http://localhost.evil.test/mcp",
])
def test_stdio_rejects_unsafe_endpoints(endpoint):
    from flowly.mcp.server.external_stdio import validate_connection

    with pytest.raises(ExternalAccessError):
        validate_connection(endpoint, "flm_" + "a" * 64)
