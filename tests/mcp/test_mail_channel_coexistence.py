"""Real email channel and bus against a local provider during MCP key changes."""

import asyncio
import base64
import hashlib
import secrets
from email import message_from_bytes
from types import SimpleNamespace

import httpx
from aiohttp import web

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels import email as mail_module
from flowly.channels.email import EmailChannel
from flowly.gateway.server import GatewayServer
from flowly.mcp.server.external_service import ExternalMCPService
from tests.mcp import test_live_tool_bridge as support

runtime = support.runtime


async def test_email_poll_and_reply_continue_across_external_key_revocation(runtime, tmp_path, monkeypatch):
    inbox, sent = [], []
    delivered = asyncio.Event()

    async def provider(request):
        assert request.headers.get("Authorization") == "Bearer local-mail-fixture"
        if request.method == "GET" and request.path == "/messages":
            return web.json_response({"messages": [{"id": identifier} for identifier in inbox]})
        if request.method == "GET" and request.path.startswith("/messages/"):
            identifier = request.match_info["path"].split("/")[-1]
            return web.json_response({
                "id": identifier, "threadId": "test-thread", "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "Test Sender <sender@example.test>"},
                        {"name": "Subject", "value": "Isolated mail regression"},
                        {"name": "Message-ID", "value": f"<{identifier}@example.test>"},
                    ],
                    "body": {"data": base64.urlsafe_b64encode(identifier.encode()).decode()},
                },
            })
        if request.path.endswith("/modify"):
            identifier = request.path.split("/")[-2]
            inbox.remove(identifier)
            return web.json_response({"id": identifier})
        if request.path == "/messages/send":
            body = await request.json()
            sent.append(message_from_bytes(base64.urlsafe_b64decode(body["raw"])))
            delivered.set()
            return web.json_response({"id": "local-outbound"})
        raise AssertionError("Unexpected local mail request")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", provider)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    endpoint = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(mail_module, "_GMAIL_API", endpoint)
    monkeypatch.setattr(mail_module.gmail_auth, "get_valid_access_token", lambda: (
        "local-mail-fixture", "owner@example.test",
    ))
    bus = MessageBus()
    email = EmailChannel(SimpleNamespace(poll_interval_seconds=0.02, allow_from=["sender@example.test"]), bus)
    bus.subscribe_outbound("email", email.send)
    polling = asyncio.create_task(email.start())
    dispatch = asyncio.create_task(bus.dispatch_outbound())
    service = ExternalMCPService(tmp_path / "mcp-access.json", runtime.bridge)

    async def gateway_send(target, message):
        raise AssertionError("MCP did not receive authority to send email")

    gateway = GatewayServer(
        host="127.0.0.1", port=0, sessions=runtime.sessions, on_send=gateway_send,
        auth_token="isolated-admin-" + "x" * 32, require_loopback_auth=True,
    )
    gateway._tool_bridge = runtime.bridge
    gateway._external_mcp_service = service
    try:
        await gateway.start()

        async def roundtrip(identifier):
            delivered.clear()
            inbox.append(identifier)
            incoming = await asyncio.wait_for(bus.consume_inbound(), 5)
            assert incoming.channel == "email" and incoming.chat_id == "sender@example.test"
            assert identifier in incoming.content
            await bus.publish_outbound(OutboundMessage(channel="email", chat_id=incoming.chat_id, content="Local reply"))
            await asyncio.wait_for(delivered.wait(), 5)
            assert sent[-1]["To"] == "sender@example.test"
            assert sent[-1]["From"] == "owner@example.test"
            assert sent[-1]["Subject"] == "Re: Isolated mail regression"
            assert sent[-1]["In-Reply-To"] == f"<{identifier}@example.test>"
            assert email.is_running and not polling.done() and not dispatch.done()

        await roundtrip("before-mcp-change")
        token = "flm_" + secrets.token_hex(32)
        row = await service.owner("create", {
            "id": secrets.token_hex(16), "label": "Mail coexistence", "sessionKey": "cli:first",
            "tools": ["board_list"], "tokenDigest": hashlib.sha256(token.encode()).hexdigest(), "ttlSeconds": 3600,
        })
        async with httpx.AsyncClient() as http:
            response = await http.post(service.local_endpoint, headers={"Authorization": f"Bearer {token}"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "board_list", "arguments": {}},
            })
            assert response.status_code == 200 and not response.json()["result"].get("isError")
            await service.owner("revoke", {"id": row["id"]})
            assert (await http.post(service.local_endpoint, headers={"Authorization": f"Bearer {token}"}, json={})).status_code == 401
        await roundtrip("after-mcp-change")
        assert len(sent) == 2 and inbox == []
    finally:
        await gateway.stop()
        await email.stop()
        bus.stop()
        polling.cancel()
        dispatch.cancel()
        await asyncio.gather(polling, dispatch, return_exceptions=True)
        await runner.cleanup()
