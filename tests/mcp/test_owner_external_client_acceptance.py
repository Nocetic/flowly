"""Installed clients against the owner-key gateway, with isolated local data."""

import asyncio
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

import httpx
import pytest

from flowly.agent.tools.board import BoardListTool
from flowly.gateway.server import GatewayServer
from flowly.mcp.external_access import ExternalAccessError
from flowly.mcp.server.external_service import ExternalMCPService
from tests.mcp import test_external_client_acceptance as clients
from tests.mcp import test_live_tool_bridge as support

runtime = support.runtime
adapter = clients.adapter


@pytest.fixture
async def gateway(runtime, tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"channels":{"email":{"enabled":false}},"marker":"preserve"}')
    original = path.read_bytes()
    sent = []

    async def send(target, message):
        sent.append((target, message))
        return True

    service = ExternalMCPService(tmp_path / "mcp-access.json", runtime.bridge)
    server = GatewayServer(
        host="127.0.0.1", port=0, sessions=runtime.sessions, on_send=send,
        auth_token="isolated-gateway-admin-" + "x" * 32, require_loopback_auth=True,
    )
    server._tool_bridge = runtime.bridge
    server._external_mcp_service = service
    await server.start()
    try:
        yield service
    finally:
        await server.stop()
        assert path.read_bytes() == original
        assert sent == []


async def issue(service, names, *, session="cli:first", ttl=3600):
    token = "flm_" + secrets.token_hex(32)
    row = await service.owner("create", {
        "id": secrets.token_hex(16), "label": "Isolated acceptance client", "sessionKey": session,
        "tools": names, "tokenDigest": hashlib.sha256(token.encode()).hexdigest(), "ttlSeconds": ttl,
    })
    assert token not in service.store.path.read_text()
    return token, row


def connection(adapter, service, token, transport, tmp_path):
    if transport == "http":
        return clients.expand(adapter["http_mcp_options"], {
            "$MCP_URL": service.local_endpoint, "$MCP_TOKEN": token,
        })
    return {
        "command": sys.executable, "args": ["-B", "-m", "flowly", "mcp", "connect"],
        "env": {
            "FLOWLY_HOME": str(tmp_path / "stdio-profile"), "FLOWLY_QUIET": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "FLOWLY_MCP_ENDPOINT": service.local_endpoint, "FLOWLY_MCP_ACCESS_KEY": token,
        },
    } | adapter.get("mcp_options", {})


@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("writes", [False, True], ids=["readonly", "write-and-read"])
async def test_owner_key_installed_client_roundtrip(runtime, gateway, tmp_path, adapter, transport, writes):
    names = ["board_add", "board_list"] if writes else ["board_list"]
    token, _ = await issue(gateway, names)
    actions = ([("board_add", {"title": clients.TITLE})] if writes else []) + [("board_list", {})]
    peer = clients.ModelPeer(adapter["style"], actions)
    async with clients.model_api(peer) as api:
        _, output = await clients.run_adapter(adapter, tmp_path, api, {
            "flowly": connection(adapter, gateway, token, transport, tmp_path),
        })
    seen = clients.transcript(peer)
    assert token not in seen and token not in output
    cards = runtime.store.list_cards()
    if writes:
        assert len(cards) == 1 and cards[0].title == clients.TITLE
        assert cards[0].origin_chat_id == "first" and cards[0].id in seen
    else:
        assert not cards
        assert "board_add" not in seen
        with pytest.raises(ExternalAccessError, match="not permitted"):
            await gateway.call(token, "board_add", {"title": "must not execute"})
    assert not gateway._calls and not runtime.bridge._grants


@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("withdraw", ["revoke", "expire", "delete"])
async def test_owner_withdrawal_reaches_installed_client_and_spares_other_key(
    runtime, gateway, tmp_path, adapter, transport, withdraw,
):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class WaitingRead(BoardListTool):
        name = "board_list"
        description = "Read a test board after waiting for a controlled cancellation"
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}

        async def execute(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    runtime.tools.register(WaitingRead(runtime.store))
    # Accelerate only the isolated store's clock; never modify a real token.
    clock = [time.time()]
    gateway.store.clock = lambda: clock[0]
    token, row = await issue(gateway, ["board_list"], ttl=60)
    other, _ = await issue(gateway, ["messages_read"], session="cli:second")
    peer = clients.ModelPeer(adapter["style"], [("board_list", {})])
    async with clients.model_api(peer) as api:
        running = asyncio.create_task(clients.run_adapter(adapter, tmp_path, api, {
            "flowly": connection(adapter, gateway, token, transport, tmp_path),
        }))
        started = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait({running, started}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
            if running in done:
                await running  # Surface client failures rather than a misleading wait timeout.
            assert entered.is_set(), "Client did not start the controlled read"
            if withdraw == "revoke":
                assert (await gateway.owner("revoke", {"id": row["id"]}))["status"] == "revoked"
            elif withdraw == "delete":
                assert runtime.sessions.delete("cli:first")
                runtime.sessions.save(runtime.sessions.get_or_create("cli:first"))
            else:
                clock[0] = row["expiresAt"] + 1
            await asyncio.wait_for(cancelled.wait(), 3)
            _, output = await asyncio.wait_for(running, 20)
        finally:
            started.cancel()
            running.cancel()
            await asyncio.gather(started, running, return_exceptions=True)
    seen = clients.transcript(peer)
    assert "withdrawn" in seen or "expired" in seen
    assert token not in output and token not in seen
    with pytest.raises(ExternalAccessError):
        gateway.authorize(token)
    async with httpx.AsyncClient() as http:
        assert (await http.post(gateway.local_endpoint, json={}, headers={"Authorization": f"Bearer {token}"})).status_code == 401
    # The unaffected client's authority and actual read remain usable.
    result = await gateway.call(other, "messages_read", {"session_key": "cli:second"})
    assert not result.get("isError") and "Bridge test" in json.dumps(result)
    assert not runtime.store.list_cards()
    assert not gateway._calls and not runtime.bridge._grants
