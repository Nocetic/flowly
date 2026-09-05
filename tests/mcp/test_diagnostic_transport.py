"""Error/success separation through real SDK stdio, HTTP and SSE peers."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import sys
import threading
import warnings

import pytest
from mcp import types
from mcp.server.mcpserver import Context, MCPServer

from flowly.agent.tools.registry import ToolRegistry
from flowly.mcp import client

OPAQUE_CREDENTIAL = "opaque-test-credential"


def make_server(*, wire: bool = False):
    server = MCPServer("diagnostic-fixture")
    # The SDK diverts ordinary fd 1 writes to stderr. Keep a child-only wire
    # duplicate to emulate a genuinely non-compliant server, not that diversion.
    wire_fd = os.dup(1) if wire else None

    @server.tool(annotations=types.ToolAnnotations(readOnlyHint=True))
    async def inspect(kind: str, ctx: Context) -> types.CallToolResult:
        """Return deliberately sensitive errors, or legitimate success data."""
        if kind == "error":
            return types.CallToolResult(
                isError=True,
                content=[
                    types.TextContent(text='Provider failed: {"refresh_token":"hidden-value"} ' + OPAQUE_CREDENTIAL),
                    types.ImageContent(data="cHJpdmF0ZS1iaW5hcnk=", mimeType="image/png"),
                ],
                structuredContent={"api_key": "hidden-structured"},
                _meta={"token": "hidden-meta"},
            )
        if kind == "oversize":
            return types.CallToolResult(isError=True, content=[types.TextContent(text="private-fragment" * 10_000)])
        if kind == "logs":
            # Deliberately exercise the retained wire logging capability. Its
            # SDK deprecation warning is not itself a remote log notification.
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="The logging capability is deprecated.*")
                await ctx.request_context.session.send_log_message("debug", "below-default-level")
                await ctx.request_context.session.send_log_message(
                    "warning", {"access_token": "hidden-log-key", "detail": OPAQUE_CREDENTIAL}, logger="remote\nforged",
                )
                for index in range(300):
                    await ctx.request_context.session.send_log_message("info", {"index": index})
        if kind == "stderr":
            for fragment in (b'Error: {"api_', b'key":"hidden-log-key"} ', OPAQUE_CREDENTIAL.encode(), b'\n'):
                os.write(2, fragment)
        if kind == "malformed-stdout":
            assert wire_fd is not None
            os.write(wire_fd, ('invalid JSON token=hidden-log-key ' + OPAQUE_CREDENTIAL + '\n').encode())
        return types.CallToolResult(
            content=[types.TextContent(text='{"api_key":"legitimate-success-data"}')],
            structuredContent={"token": "legitimate-success-data"},
            _meta={"vendor": "retained"},
        )

    return server


@pytest.fixture(params=["stdio-auto", "stdio-legacy", "stdio-stateless", "http-auto", "http-legacy", "sse-legacy"])
async def connected_peer(tmp_path, monkeypatch, request):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    transport, protocol = request.param.split("-")
    config = {"protocol": protocol, "connect_timeout": 5, "timeout": 3, "osv_check": False}
    server = thread = listener = None
    try:
        if transport == "stdio":
            config.update({
                "command": sys.executable,
                "args": ["-c", "from tests.mcp.test_diagnostic_transport import make_server; make_server(wire=True).run()"],
                "env": {"VENDOR_AUTH": OPAQUE_CREDENTIAL},
            })
        else:
            import uvicorn

            mcp = make_server()
            app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app(stateless_http=False, json_response=transport == "json")
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            thread.start()
            async with asyncio.timeout(5):
                while not server.started:
                    assert thread.is_alive()
                    await asyncio.sleep(0.01)
            port = listener.getsockname()[1]
            config.update({
                "url": f"http://127.0.0.1:{port}/{'sse' if transport == 'sse' else 'mcp'}",
                "transport": "sse" if transport == "sse" else "http",
                "headers": {"X-Test-Credential": OPAQUE_CREDENTIAL},
            })
        registry = ToolRegistry()
        assert await asyncio.to_thread(
            client.discover_mcp_tools, servers={"diagnostic": config}, tool_registry=registry,
        ) == ["mcp_diagnostic_inspect"]
        yield registry.get("mcp_diagnostic_inspect"), tmp_path / "home"
    finally:
        await asyncio.to_thread(client.shutdown_mcp_servers)
        client._reset_server_error("diagnostic")
        if server is not None:
            server.should_exit = True
        if thread is not None:
            await asyncio.to_thread(thread.join, 5)
            assert not thread.is_alive()
        if listener is not None:
            listener.close()


@pytest.mark.parametrize("native", [False, True])
async def test_real_error_results_are_bounded_redacted_and_do_not_cache_attachments(connected_peer, native):
    original, home = connected_peer
    tool = copy.copy(original)
    tool._bridge_native_result = native
    for kind in ("error", "oversize"):
        result = await tool.execute(kind=kind)
        payload = json.loads(result)
        assert payload["isError"] is True
        assert len(result) < 8192
        for secret in (OPAQUE_CREDENTIAL, "hidden-value", "hidden-structured", "hidden-meta", "cHJpdmF0ZS1iaW5hcnk=", "private-fragment"):
            assert secret not in result
    assert not list(home.glob("media/mcp/*"))
    # No overbroad scrubbing of successful application data or vendor metadata.
    success = json.loads(await tool.execute(kind="success"))
    assert "legitimate-success-data" in json.dumps(success)
    assert success["_meta"]["vendor"] == "retained"
    if native:
        assert success["content"][0]["type"] == "text"
        assert not success.get("isError")
    else:
        assert success["structuredContent"] == {"token": "legitimate-success-data"}


async def test_real_log_notifications_are_opted_in_filtered_redacted_and_bounded(connected_peer, caplog):
    tool, home = connected_peer
    result = json.loads(await tool.execute(kind="logs"))
    assert not result.get("isError")
    # Join the actual retained writer, not a sleep guessing when disk is ready.
    await asyncio.to_thread(client.shutdown_mcp_servers)
    state = tool._server_task.health_snapshot()["diagnostics"]
    assert state["received"] >= 301
    assert state["dropped"] >= 200
    entries = [json.loads(line) for path in (home / "logs/mcp").glob("diagnostics.jsonl*")
               for line in path.read_text().splitlines()]
    assert len(entries) <= 101
    serialized = json.dumps(entries)
    assert "below-default-level" not in serialized
    assert OPAQUE_CREDENTIAL not in serialized and "hidden-log-key" not in serialized
    assert "[REDACTED]" in serialized
    assert any(row["source"] == "notification" and row["level"] == "warning" for row in entries)
    assert not tool._server_task._diagnostics._thread.is_alive()
    assert "message handler error" not in caplog.text


@pytest.mark.parametrize("connected_peer", ["json-auto"], indirect=True)
async def test_json_only_http_peer_still_works_without_a_notification_stream(connected_peer):
    tool, _ = connected_peer
    assert not json.loads(await tool.execute(kind="logs")).get("isError")
    assert tool._server_task.health_snapshot()["diagnostics"]["received"] == 0


@pytest.mark.parametrize("connected_peer", ["stdio-auto", "stdio-legacy", "stdio-stateless"], indirect=True)
async def test_real_subprocess_stderr_has_no_raw_disk_or_terminal_fallback(connected_peer):
    tool, home = connected_peer
    result = json.loads(await tool.execute(kind="stderr"))
    assert not result.get("isError")
    await asyncio.to_thread(client.shutdown_mcp_servers)
    output = "\n".join(path.read_text() for path in (home / "logs/mcp").glob("diagnostics.jsonl*"))
    assert "[REDACTED]" in output
    assert "hidden-log-key" not in output and OPAQUE_CREDENTIAL not in output
    assert not (home / "logs/mcp-stderr.log").exists()


@pytest.mark.parametrize("connected_peer", ["stdio-auto", "stdio-legacy", "stdio-stateless"], indirect=True)
async def test_sdk_parse_errors_cannot_bypass_private_log_redaction(connected_peer, caplog):
    tool, home = connected_peer
    result = await tool.execute(kind="malformed-stdout")
    await asyncio.to_thread(client.shutdown_mcp_servers)
    assert OPAQUE_CREDENTIAL not in result and "hidden-log-key" not in result
    assert OPAQUE_CREDENTIAL not in caplog.text and "hidden-log-key" not in caplog.text
    entries = [json.loads(line) for path in (home / "logs/mcp").glob("diagnostics.jsonl*")
               for line in path.read_text().splitlines()]
    assert any(row["source"] == "sdk" for row in entries)
    output = json.dumps(entries)
    assert OPAQUE_CREDENTIAL not in output and "hidden-log-key" not in output
    assert "Failed to parse" in output and "[REDACTED]" in output
