"""Combined external-client, live-runtime and remote-server acceptance paths."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from flowly.agent.tool_context import tool_execution_scope
from flowly.exec.approval_manager import ApprovalManager
from flowly.mcp import client
from flowly.mcp.server.managed_tools import managed_tool_launch
from tests.mcp import test_live_tool_bridge as support

runtime = support.runtime
QUERY = "mcp_acceptance_query"
UPDATE = "mcp_acceptance_update"
LATE = "mcp_acceptance_late"
SECRET = "isolated-acceptance-credential"

# The server is a separate process. Its append-only receipt contains operation
# identities, not secrets, and proves rejected/retried calls never reached it.
SERVER = r'''
import asyncio, json, os, sys
from pathlib import Path
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

state_file, log_file, release_file = map(Path, sys.argv[1:])
def state():
    return json.loads(state_file.read_text())
def record(kind, **fields):
    with log_file.open("a") as out:
        out.write(json.dumps(dict(kind=kind, pid=os.getpid(), **fields)) + "\n")
record("spawn")
async def listing(ctx, params):
    current = state()
    return types.ListToolsResult(tools=[types.Tool(
        name=name, description="Acceptance " + name,
        inputSchema={"type": "object", "properties": {
            "value": {"type": "integer", "minimum": 1, "maximum": 3},
            "wait": {"type": "boolean"},
            "session_key": {"type": "string"},
        }, "required": ["value"], "additionalProperties": False},
        annotations=types.ToolAnnotations(readOnlyHint=(
            name != "update" and (name != "query" or current.get("readonly", True))
        )),
    ) for name in (["query", "update", "late"] if current.get("late") else ["query", "update"])])
async def call(ctx, params):
    args = params.arguments or {}
    value = args["value"]
    record("start", tool=params.name, value=value)
    try:
        if args.get("wait"):
            while not release_file.exists():
                await asyncio.sleep(0.01)
        os.write(2, ("Acceptance diagnostic: " + os.environ["VENDOR_AUTH"] + "\n").encode())
        result = {"tool": params.name, "value": value, "pid": os.getpid()}
        record("finish", tool=params.name, value=value)
        return types.CallToolResult(content=[types.TextContent(text=json.dumps(result))],
                                    structuredContent=result)
    except asyncio.CancelledError:
        record("cancel", tool=params.name, value=value)
        raise
async def main():
    server = Server("acceptance", on_list_tools=listing, on_call_tool=call)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
asyncio.run(main())
'''


async def until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def remote(runtime, tmp_path, request):
    state, receipt, release = (tmp_path / name for name in ("remote.json", "receipt.jsonl", "released"))
    state.write_text("{}")
    config = {
        "command": sys.executable, "args": ["-c", SERVER, str(state), str(receipt), str(release)],
        "env": {"VENDOR_AUTH": SECRET}, "protocol": getattr(request, "param", "auto"),
        "trust": "untrusted", "timeout": 10, "connect_timeout": 5, "osv_check": False,
        # This peer does not request forms. Legacy form-enabled peers deliberately
        # serialize calls to preserve ownership; write consent is independent.
        "elicitation": {"enabled": False},
        "supports_parallel_tool_calls": True, "max_parallel_tool_calls": 2,
        "lifecycle": {"lazy_start": True, "manifest_ttl": 60, "idle_timeout": 0.2},
    }

    async def discover():
        names = await asyncio.to_thread(
            client.discover_mcp_tools, servers={"acceptance": config}, tool_registry=runtime.tools,
        )
        assert {QUERY, UPDATE} <= set(names)

    def events(kind):
        if not receipt.exists():
            return []
        return [row for line in receipt.read_text().splitlines()
                if (row := json.loads(line))["kind"] == kind]

    try:
        await discover()
        await asyncio.to_thread(client.shutdown_mcp_servers)
        await discover()
        assert client.get_mcp_server_health()["acceptance"]["catalogSource"] == "manifest"
        assert len(events("spawn")) == 1
        yield SimpleNamespace(state=state, events=events, release=release, home=tmp_path)
    finally:
        release.touch()
        await asyncio.to_thread(client.shutdown_mcp_servers)
        client._reset_server_error("acceptance")


@asynccontextmanager
async def external(runtime, key, names, *, writes=False, mode="auto"):
    with tool_execution_scope(key, allowed_tools=frozenset(names)):
        async with managed_tool_launch(runtime, key, allow_writes=writes) as launch:
            params = StdioServerParameters(
                command=launch.command, args=launch.args, env={**os.environ, **launch.env},
            )
            async with Client(stdio_client(params), mode=mode) as connected:
                yield connected.session, launch


def payload(result):
    assert not result.is_error, result
    return result.structured_content


@pytest.mark.parametrize("remote", ["auto", "legacy", "stateless"], indirect=True)
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_delegated_warm_cache_recycle_preserves_schema_authority_and_private_logs(runtime, remote, mode):
    async with external(runtime, "cli:first", {QUERY, "board_list"}, mode=mode) as (session, _):
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {QUERY, "board_list"}
        assert tools[QUERY].input_schema == runtime.tools.get(QUERY).parameters
        assert (await session.call_tool(QUERY, {"value": 4})).is_error
        assert (await session.call_tool(UPDATE, {"value": 1})).is_error
        assert (await session.call_tool("board_add", {"title": "Never"})).is_error
        assert not remote.events("start")
        assert len(remote.events("spawn")) == 1  # Rejection does not start the remote process.

        results = await asyncio.gather(*(session.call_tool(QUERY, {"value": value}) for value in (1, 2, 3)))
        values = [payload(result) for result in results]
        assert [value["value"] for value in values] == [1, 2, 3]
        assert len({value["pid"] for value in values}) == 1
        assert len(remote.events("spawn")) == 2
        await until(lambda: client.get_mcp_server_health()["acceptance"]["state"] == "idle")
        with pytest.raises(ProcessLookupError):
            os.kill(values[0]["pid"], 0)

        # A reconnect can change a read into a write and add a new tool. Neither
        # can acquire the already-issued grant's authority from a cached schema.
        remote.state.write_text('{"readonly": false, "late": true}')
        changed = await session.call_tool(QUERY, {"value": 1})
        assert changed.is_error and "changed" in changed.content[0].text.lower()
        assert len(remote.events("start")) == 3
        assert len(remote.events("spawn")) == 3
        assert {tool.name for tool in (await session.list_tools()).tools} == {"board_list"}
        assert (await session.call_tool(QUERY, {"value": 1})).is_error
        assert (await session.call_tool(LATE, {"value": 1})).is_error
        assert not runtime.store.list_cards()

    assert not runtime._external_tool_bridge._grants
    await asyncio.to_thread(client.shutdown_mcp_servers)
    diagnostic = remote.home / "logs" / "mcp" / "diagnostics.jsonl"
    text = diagnostic.read_text()
    assert "Acceptance diagnostic" in text and SECRET not in text
    assert diagnostic.stat().st_mode & 0o077 == 0
    assert all(json.loads(line) for line in text.splitlines())


@pytest.mark.parametrize("remote", ["auto", "legacy"], indirect=True)
async def test_delegated_consent_and_revocation_do_not_replay_or_cancel_another_owner(runtime, remote, monkeypatch):
    approvals = ApprovalManager()
    monkeypatch.setattr("flowly.exec.approval_manager._manager", approvals)
    owners = []

    async def answer(pending):
        owners.append(pending.session_key)
        approvals.resolve(pending.id, "deny" if len(owners) == 1 else "allow-once")

    approvals.add_notify_callback(answer)
    async with external(runtime, "cli:first", {UPDATE}, writes=True) as (session, _):
        assert (await session.call_tool(UPDATE, {"value": 1, "session_key": "cli:spoof"})).is_error
        assert not remote.events("start")
        assert len(remote.events("spawn")) == 1
        assert payload(await session.call_tool(UPDATE, {"value": 2}))["value"] == 2
    assert owners == ["cli:first", "cli:first"]
    assert [(row["tool"], row["value"]) for row in remote.events("finish")] == [("update", 2)]

    ready = {key: asyncio.Event() for key in ("cli:first", "cli:second")}
    withdraw = {}

    async def waiting(key, value):
        async with external(runtime, key, {QUERY}) as (session, launch):
            withdraw[key] = launch.withdraw
            ready[key].set()
            return await session.call_tool(QUERY, {"value": value, "wait": True})

    tasks = [asyncio.create_task(waiting(key, value)) for key, value in (("cli:first", 1), ("cli:second", 3))]
    try:
        await asyncio.gather(*(event.wait() for event in ready.values()))
        await until(lambda: len(remote.events("start")) == 3)
        withdraw["cli:first"]()
        first = await asyncio.wait_for(tasks[0], 10)
        assert first.is_error
        await until(lambda: len(remote.events("cancel")) == 1)
        assert remote.events("cancel")[0]["value"] == 1
        assert not tasks[1].done()
        remote.release.touch()
        second = payload(await asyncio.wait_for(tasks[1], 10))
        assert second["value"] == 3
        assert [(row["tool"], row["value"]) for row in remote.events("finish")] == [("update", 2), ("query", 3)]
        assert not runtime._external_tool_bridge._grants
    finally:
        remote.release.touch()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_public_conversation_write_event_attachment_and_approval_round_trip(runtime, monkeypatch, mode):
    import aiohttp

    from flowly.exec.types import ExecRequest, PendingApproval
    from flowly.gateway.server import GatewayServer

    key = "web:acceptance_room:topic"
    conversation = runtime.sessions.get_or_create(key)
    conversation.add_message("user", "Before the external client connected")
    runtime.sessions.save(conversation)
    sent = []

    async def send(target, message):
        sent.append((target, message))
        conversation = runtime.sessions.get_or_create(target)
        conversation.add_message("assistant", message, media=["/private/acceptance/reply.png"])
        runtime.sessions.save(conversation)
        return True

    approvals = ApprovalManager()
    monkeypatch.setattr("flowly.exec.approval_manager._manager", approvals)
    gateway = GatewayServer(
        host="127.0.0.1", port=0, sessions=runtime.sessions, on_send=send,
        control_token="acceptance-control-" + "x" * 32,
        auth_token="separate-gateway-" + "y" * 32, require_loopback_auth=True,
    )

    def conversation_client(writes=False):
        args = ["-m", "flowly", "mcp", "serve"] + (["--allow-writes"] if writes else [])
        return Client(stdio_client(StdioServerParameters(
            command=sys.executable, args=args, env={**os.environ, "FLOWLY_QUIET": "1"},
        )), mode=mode)

    pending = None
    await gateway.start()
    try:
        origin = f"http://127.0.0.1:{gateway.port}"
        async with aiohttp.ClientSession() as http:
            for token in (None, "wrong-control-token", gateway._auth_token):
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                for method, path, body in (
                    ("GET", "/control/approvals", None),
                    ("POST", "/control/messages/send", {"target": key, "message": "Never"}),
                    ("POST", "/control/approvals/resolve", {"id": "unowned", "decision": "deny"}),
                ):
                    async with http.request(method, origin + path, json=body, headers=headers) as response:
                        assert response.status == 401
            # A scoped control credential cannot mint full gateway authority.
            async with http.post(origin + "/api/auth/ws-ticket", headers={
                "Authorization": f"Bearer {gateway._control_token}",
            }) as response:
                assert response.status == 401
            for path in ("/api/mcp/admin", "/ws-admin", "/api/media/stream/private", "/control/admin"):
                async with http.get(origin + path) as response:
                    assert response.status == 401
        async with conversation_client() as connected:
            session = connected.session
            assert "messages_send" not in {tool.name for tool in (await session.list_tools()).tools}
            assert (await session.call_tool("messages_send", {"target": key, "message": "Never"})).is_error
            targets = payload(await session.call_tool("channels_list", {"platform": "web"}))["targets"]
            assert key in {target["target"] for target in targets}
            head = payload(await session.call_tool("events_poll", {"session_key": key}))["next_cursor"]
        assert sent == []

        # Restart only the public MCP process. The live gateway and its scoped
        # idempotency ledger remain the same authority throughout.
        async with conversation_client(writes=True) as connected:
            session = connected.session
            arguments = {"target": key, "message": "External reply", "idempotency_key": "acceptance-send"}
            first = payload(await session.call_tool("messages_send", arguments))
            second = payload(await session.call_tool("messages_send", arguments))
            assert first["sent"] and second["sent"]
            assert not first["duplicate"] and second["duplicate"]
            assert sent == [(key, "External reply")]
            event_page = payload(await session.call_tool("events_wait", {
                "after_cursor": head, "session_key": key, "timeout_ms": 3000,
            }))
            assert [event["content"] for event in event_page["events"]] == ["External reply"]
            message_id = event_page["events"][0]["message_id"]
            attachment = payload(await session.call_tool("attachments_fetch", {
                "session_key": key, "message_id": message_id,
            }))
            assert attachment["attachments"][0]["fileName"] == "reply.png"
            assert "/private/acceptance" not in json.dumps(attachment)
            assert (await session.call_tool("attachments_fetch", {
                "session_key": "cli:second", "message_id": message_id,
            })).is_error

            now = time.time()
            pending = asyncio.create_task(approvals.request_and_wait(PendingApproval(
                id="acceptance-permission", request=ExecRequest(command="Local acceptance action", session_key=key),
                session_key=key, created_at=now, expires_at=now + 30, risk_reasons=["Test-only consent"],
            )))
            await until(lambda: bool(approvals.list_pending()))
            listing = payload(await session.call_tool("approvals_list", {}))
            assert listing["approvals"][0]["session_key"] == key
            resolved = payload(await session.call_tool("approvals_resolve", {
                "id": "acceptance-permission", "decision": "deny",
            }))
            assert resolved["resolved"]
            assert await asyncio.wait_for(pending, 5) == "deny"

        async with conversation_client() as connected:
            resumed = payload(await connected.session.call_tool("events_poll", {
                "session_key": key, "after_cursor": event_page["next_cursor"],
            }))
            assert not resumed["events"]
            history = payload(await connected.session.call_tool("messages_read", {"session_key": key}))
            assert [message["content"] for message in history["messages"]] == [
                "Before the external client connected", "External reply",
            ]
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await gateway.stop()
