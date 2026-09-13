"""Input and failure regression coverage without live accounts or providers."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp import types

from flowly.mcp import client
from flowly.mcp.tool import MCPTool


@pytest.fixture
def peer(monkeypatch):
    client._reset_server_error("recovery")
    monkeypatch.setattr(client, "get_mcp_loop", asyncio.get_running_loop)
    calls = []

    class Session:
        response = types.CallToolResult(content=[types.TextContent(text="ok")])

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

    task = SimpleNamespace(
        name="recovery",
        session=Session(),
        _config={},
        tool_timeout=1,
        rpc_lock=asyncio.Lock(),
        report_transport_failure=lambda *_: None,
    )

    def make(schema=None):
        return MCPTool(
            server_task=task,
            remote_tool=types.Tool(
                name="executeRead",
                inputSchema=schema or {"type": "object"},
            ),
        )

    yield task, calls, make
    client._reset_server_error("recovery")


async def test_schema_rejects_wrong_nesting_before_transport_or_consent(peer):
    task, calls, make = peer
    task._config = {"trust": "untrusted"}
    tool = make(
        {
            "type": "object",
            "properties": {
                "cloudId": {"type": "string", "minLength": 1},
                "inputs": {"type": "object"},
            },
            "required": ["cloudId"],
        }
    )
    for _ in range(6):
        result = json.loads(await tool.execute(inputs={"cloudId": "site-value"}))
        assert result["code"] == "INVALID_ARGUMENTS"
        assert "cloudId" in result["error"]
    assert not calls
    assert client.circuit_breaker_block_reason("recovery") is None


async def test_raw_schema_preserves_nested_refs_and_nullable_values(peer):
    _, calls, make = peer
    tool = make(
        {
            "type": "object",
            "$defs": {
                "Site": {
                    "type": "object",
                    "properties": {"id": {"type": ["string", "null"]}},
                    "required": ["id"],
                    "additionalProperties": False,
                }
            },
            "properties": {"site": {"$ref": "#/$defs/Site"}},
            "required": ["site"],
        }
    )
    invalid = json.loads(await tool.execute(site={"id": 3}))
    assert invalid["code"] == "INVALID_ARGUMENTS"
    assert not calls
    assert json.loads(await tool.execute(site={"id": None}))["result"] == "ok"
    assert calls == [("executeRead", {"site": {"id": None}})]


async def test_validation_errors_never_echo_values(peer):
    _, calls, make = peer
    tool = make(
        {
            "type": "object",
            "properties": {"credential": {"enum": ["allowed"]}},
            "additionalProperties": False,
        }
    )
    raw = await tool.execute(credential="private-user-value", **{"private-property-name": "hidden"})
    assert "private-user-value" not in raw
    assert "private-property-name" not in raw
    assert json.loads(raw)["code"] == "INVALID_ARGUMENTS"
    assert not calls


async def test_external_schema_ref_is_not_fetched(peer, monkeypatch):
    _, calls, make = peer

    def forbidden(*args, **kwargs):
        pytest.fail("Schema validation must not access the network")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    tool = make(
        {"type": "object", "properties": {"site": {"$ref": "https://example.invalid/schema"}}}
    )
    result = json.loads(await tool.execute(site={}))
    assert result["code"] == "INVALID_SCHEMA"
    assert not calls


async def test_application_errors_never_open_server_breaker(peer):
    task, calls, make = peer
    task.session.response = types.CallToolResult(
        isError=True,
        content=[
            types.TextContent(text="Failed to fetch cloud ID: Invalid URL"),
        ],
    )
    tool = make()
    for _ in range(7):
        assert json.loads(await tool.execute())["isError"] is True
    assert len(calls) == 7
    assert client.circuit_breaker_block_reason("recovery") is None
    task.session.response = types.CallToolResult(content=[types.TextContent(text="ok")])
    assert json.loads(await tool.execute())["result"] == "ok"


@pytest.mark.parametrize(
    "status,opens",
    [(400, False), (401, False), (403, False), (404, False), (429, True), (503, True)],
)
async def test_http_application_vs_availability_errors(peer, status, opens):
    task, calls, make = peer
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid/mcp"))
    task.session.response = httpx.HTTPStatusError(
        "request failed", request=response.request, response=response
    )
    tool = make()
    for _ in range(5):
        assert "error" in json.loads(await tool.execute())
    assert bool(client.circuit_breaker_block_reason("recovery")) is opens
    assert len(calls) == 5


async def test_transport_failures_still_open_breaker(peer):
    task, calls, make = peer
    task.session.response = ConnectionError("connection reset")
    tool = make()
    for _ in range(6):
        result = json.loads(await tool.execute())
    assert len(calls) == 5
    assert "temporarily paused" in result["error"]


async def test_application_response_closes_half_open_breaker(peer, monkeypatch):
    task, _, make = peer
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    for _ in range(5):
        client._bump_server_error("recovery")
    clock[0] += 61
    task.session.response = types.CallToolResult(
        isError=True, content=[types.TextContent(text="No permission")]
    )
    assert json.loads(await make().execute())["isError"] is True
    assert "recovery" not in client._server_breaker_probe_inflight
    assert client.circuit_breaker_block_reason("recovery") is None


@pytest.mark.parametrize("code", [-32601, -32602, -32603])
async def test_protocol_errors_do_not_reconnect_or_open_breaker(peer, code):
    from mcp.shared.exceptions import MCPError

    task, calls, make = peer
    reconnects = []
    task.report_transport_failure = lambda *_: reconnects.append(True)
    task.session.response = MCPError(code=code, message="Bad argument: stream closed")
    tool = make()
    for _ in range(6):
        assert "error" in json.loads(await tool.execute())
    await asyncio.sleep(0)
    assert len(calls) == 6
    assert not reconnects
    assert client.circuit_breaker_block_reason("recovery") is None


async def test_timeout_still_opens_breaker_without_replaying_calls(peer):
    task, calls, make = peer
    task.session.response = TimeoutError()
    tool = make()
    for _ in range(6):
        result = json.loads(await tool.execute())
    assert len(calls) == 5
    assert "temporarily paused" in result["error"]


async def test_valid_required_fields_are_forwarded_without_rewriting(peer):
    _, calls, make = peer
    tool = make(
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "definitions": {
                "Inputs": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                    "required": ["limit"],
                }
            },
            "properties": {
                "cloudId": {"type": "string"},
                "inputs": {"$ref": "#/definitions/Inputs"},
            },
            "required": ["cloudId", "inputs"],
        }
    )
    arguments = {"cloudId": "https://team.atlassian.net", "inputs": {"limit": 5}}
    assert json.loads(await tool.execute(**arguments))["result"] == "ok"
    assert calls == [("executeRead", arguments)]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "unknown-type"},
        {"$schema": "https://example.invalid/unknown-dialect", "type": "object"},
    ],
)
async def test_invalid_or_unsupported_schema_fails_closed(peer, schema):
    _, calls, make = peer
    result = json.loads(await make(schema).execute())
    assert result["code"] == "INVALID_SCHEMA"
    assert not calls


async def test_invalid_arguments_do_not_reserve_half_open_probe(peer, monkeypatch):
    _, calls, make = peer
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    for _ in range(5):
        client._bump_server_error("recovery")
    clock[0] += 61
    result = json.loads(await make({"type": "object", "required": ["site"]}).execute())
    assert result["code"] == "INVALID_ARGUMENTS"
    assert not calls
    assert "recovery" not in client._server_breaker_probe_inflight
