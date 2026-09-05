"""Delegated turns inherit exact runtime authority without persisting grants."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from flowly.agent.tool_context import current_tool_origin, tool_execution_scope
from flowly.agent.tools.base import Tool
from flowly.agent.tools.codex_session import CodexSessionTool
from flowly.codex.app_server import CodexAppServerClient, CodexProtocolError
from flowly.codex.live_tools import (
    callback_overrides,
    callback_server_config,
    thread_tool_overrides,
    verify_thread_tools,
)
from flowly.codex.session import CodexSessionConfig, TurnResult
from flowly.mcp.server.managed_tools import managed_tool_launch
from flowly.mcp.server.tool_bridge import GRANT_ENV, ToolBridgeClient
from flowly.mcp.server.tool_runtime import ToolBridgeError
from tests.mcp import test_live_tool_bridge as support

runtime = support.runtime
WaitingTool = support.WaitingTool


def _client(launch):
    return Client(stdio_client(StdioServerParameters(command=launch.command, args=launch.args, env={**os.environ, **launch.env})))


async def test_managed_launch_requires_explicit_owner_and_permissions(runtime):
    for key, permissions in ((None, None), ("cli:first", None), ("cli:second", frozenset({"board_list"}))):
        with tool_execution_scope(key, allowed_tools=permissions):
            with pytest.raises(ToolBridgeError, match="explicit owning"):
                async with managed_tool_launch(runtime, "cli:first", allow_writes=True):
                    pytest.fail("unowned delegation must not start")


async def test_generic_managed_client_gets_only_parent_grant_and_no_admin_authority(runtime):
    with tool_execution_scope("cli:first", allowed_tools=frozenset({"board_list", "board_add"})):
        async with managed_tool_launch(runtime, "cli:first", allow_writes=True) as launch:
            assert launch.tools == ("board_add", "board_list")
            secret = json.loads(launch.env[GRANT_ENV])
            assert secret["token"] not in repr(launch)
            assert secret["token"] not in " ".join(callback_overrides(launch))
            async with ToolBridgeClient(secret["endpoint"], secret["token"]) as http:
                with pytest.raises(ToolBridgeError, match="unauthorized"):
                    await http.request("POST", "/grants", {"session_key": "cli:second"})
            async with _client(launch) as mcp:
                assert {t.name for t in (await mcp.session.list_tools()).tools} == {"board_list", "board_add"}
                result = await mcp.session.call_tool("board_add", {"title": "Delegated", "session_key": "cli:spoof"})
                assert result.structured_content["card"]["originChatId"] == "first"
                assert (await mcp.session.call_tool("board_update", {"card_id": "x", "status": "done"})).is_error
    assert not runtime._external_tool_bridge._grants


async def test_readonly_sandbox_and_empty_parent_grants_do_not_fall_back_to_stateless_tools(runtime):
    for names, expected in (({"board_list", "board_add"}, {"board_list"}), ({"codex_session"}, set())):
        with tool_execution_scope("cli:first", allowed_tools=frozenset(names)):
            async with managed_tool_launch(runtime, "cli:first", allow_writes=False) as launch:
                async with _client(launch) as mcp:
                    assert {t.name for t in (await mcp.session.list_tools()).tools} == expected
                    assert (await mcp.session.call_tool("board_add", {"title": "Never"})).is_error
    assert not runtime.store.list_cards()


async def test_nested_registry_dispatch_cannot_widen_parent_permissions(runtime):
    class Delegate(Tool):
        name = "delegate_test"
        description = "Exercise nested dispatch"
        parameters = {"type": "object", "properties": {}}

        async def execute(self):
            assert current_tool_origin().allowed_tools == frozenset({self.name})
            return await runtime.tools.execute("board_add", {"title": "Never"})

    runtime.tools.register(Delegate())
    denied = frozenset(set(runtime.tools.tool_names) - {"delegate_test"})
    result = await runtime.tools.execute("delegate_test", {}, session_key="cli:first", disabled_tools=denied)
    assert "exceeds" in result
    assert not runtime.store.list_cards()


async def test_concurrent_leases_keep_owners_and_shutdown_independent(runtime):
    async def make(key, title):
        with tool_execution_scope(key, allowed_tools=frozenset({"board_add"})):
            async with managed_tool_launch(runtime, key, allow_writes=True) as launch:
                async with _client(launch) as mcp:
                    result = await mcp.session.call_tool("board_add", {"title": title})
                    return result.structured_content["card"]["originChatId"]

    assert await asyncio.gather(make("cli:first", "one"), make("cli:second", "two")) == ["first", "second"]
    assert not runtime._external_tool_bridge._closed
    assert not runtime._external_tool_bridge._grants


async def test_cancelled_parent_revokes_pending_callback_before_returning(runtime):
    tool = WaitingTool()
    runtime.tools.register(tool)
    launched = asyncio.Event()
    captured = []

    async def parent():
        with tool_execution_scope("cli:first", allowed_tools=frozenset({tool.name})):
            async with managed_tool_launch(runtime, "cli:first", allow_writes=False) as launch:
                captured.append(json.loads(launch.env[GRANT_ENV]))
                launched.set()
                await asyncio.Event().wait()

    pending = asyncio.create_task(parent())
    await launched.wait()
    entry = captured[0]
    async with ToolBridgeClient(entry["endpoint"], entry["token"]) as http:
        call = asyncio.create_task(http.call(tool.name, {"value": 1}))
        await tool.entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(ToolBridgeError):
            await call
    assert tool.cancelled.is_set()
    assert not runtime._external_tool_bridge._grants


def _coding_tool(runtime, monkeypatch, *, pause=False):
    sessions, metadata, instances = {}, {}, []
    started = asyncio.Event()

    class SessionStub:
        def __init__(self, *, config, approval_callback):
            self.config = config
            self.retired = False
            self.reasoning_items = []
            self.thread_id = None
            self.close = AsyncMock(side_effect=self._close)
            instances.append(self)

        async def _close(self):
            token = json.loads(self.config.extra_env[GRANT_ENV])["token"]
            with pytest.raises(ToolBridgeError, match="expired"):
                runtime._external_tool_bridge.list_tools(token)
            self.retired = True

        def set_thread_id(self, thread_id):
            self.thread_id = thread_id

        def set_initial_reasoning_items(self, items):
            self.reasoning_items = items

        async def run_turn(self, task, **kwargs):
            source = json.loads(self.config.extra_env[GRANT_ENV])
            async with ToolBridgeClient(source["endpoint"], source["token"]) as client:
                self.tools = [row["name"] for row in (await client.request("GET", "/list"))["tools"]]
                started.set()
                if pause:
                    await asyncio.Event().wait()
                result = await client.call("board_add", {"title": task}) if "board_add" in self.tools else {}
            self.reasoning_items = [{"encryptedContent": "continuity"}]
            return TurnResult(thread_id=self.thread_id or "persisted-thread", final_text=json.dumps(result), messages=[])

    monkeypatch.setattr("flowly.agent.tools.codex_session.CodexSession", SessionStub)
    tool = CodexSessionTool(
        config=CodexSessionConfig(cwd=str(runtime.workspace)),
        session_accessor=lambda key: metadata.setdefault(key, {}), stream_resolver=lambda key: None,
        session_store_get=sessions.get, session_store_set=lambda key, value: sessions.__setitem__(key, value),
        active_session_key_getter=lambda: "cli:unrelated-global-session",
        tool_bridge_factory=lambda key: managed_tool_launch(runtime, key, allow_writes=True),
    )
    runtime.tools.register(tool)
    return tool, instances, metadata, started


async def test_managed_coding_turn_rotates_authority_and_keeps_history(runtime, monkeypatch):
    tool, instances, metadata, _ = _coding_tool(runtime, monkeypatch)
    await runtime.tools.execute(tool.name, {"task": "first"}, session_key="cli:first")
    assert runtime.store.list_cards()[0].origin_chat_id == "first"
    result = await runtime.tools.execute(tool.name, {"task": "second"}, session_key="cli:first", disabled_tools=frozenset({"board_add"}))
    assert json.loads(result)["status"] == "ok"
    assert len(instances) == 2
    assert instances[1].thread_id == "persisted-thread"
    assert instances[1].reasoning_items == [{"encryptedContent": "continuity"}]
    assert "board_add" not in instances[1].tools
    assert instances[0].config.extra_env != instances[1].config.extra_env
    assert all(instance.close.await_count == 1 for instance in instances)
    assert all(not instance.config.auto_accept_callback_elicitation for instance in instances)
    assert set(metadata) == {"cli:first"}
    assert not runtime._external_tool_bridge._grants


async def test_managed_coding_cancellation_withdraws_grant_before_process_shutdown(runtime, monkeypatch):
    tool, instances, _, started = _coding_tool(runtime, monkeypatch, pause=True)
    pending = asyncio.create_task(runtime.tools.execute(tool.name, {"task": "wait"}, session_key="cli:first"))
    await started.wait()
    overlap = await runtime.tools.execute(tool.name, {"task": "overlap"}, session_key="cli:first")
    assert "already running" in overlap
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert instances[0].close.await_count == 1
    assert not runtime._external_tool_bridge._grants


@pytest.mark.skipif(shutil.which("codex") is None, reason="requires installed coding client")
@pytest.mark.parametrize("names", [{"board_list"}, set()])
async def test_real_app_server_uses_launch_only_mcp_settings_without_persisting_grant(runtime, tmp_path, names):
    private_home = tmp_path / "coding-client"
    private_home.mkdir()
    config_path = private_home / "config.toml"
    original = (
        'model = "unchanged-model"\nmodel_provider = "local-fixture"\n'
        '[model_providers.local-fixture]\nname = "Local fixture"\n'
        'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n'
        '[mcp_servers.unrelated]\ncommand = "/must-not-launch"\n'
        '[mcp_servers.flowly-tools]\ncommand = "/old-callback-must-not-launch"\n'
        'enabled_tools = ["old-tool"]\ndisabled_tools = ["board_list"]\n'
        '[mcp_servers.flowly-tools.env]\nFLOWLY_HOME = "/wrong-profile"\nPYTHONPATH = "/wrong-package"\n'
    )
    config_path.write_text(original)
    with tool_execution_scope("cli:first", allowed_tools=frozenset(names)):
        async with managed_tool_launch(runtime, "cli:first", allow_writes=False) as launch:
            client = await CodexAppServerClient.spawn(
                codex_home=str(private_home), cwd=str(runtime.workspace), env=launch.env,
                config_overrides=callback_overrides(launch),
            )
            try:
                effective = (await client.request("config/read", {"includeLayers": False}, timeout=15))["config"]
                assert effective["model"] == "unchanged-model"
                assert effective["features"]["apps"] is False
                assert effective["features"]["plugins"] is False
                assert effective["features"]["shell_snapshot"] is False
                assert "flowly-tools" in effective["mcp_servers"]
                entry = effective["mcp_servers"]["flowly-tools"]
                assert GRANT_ENV in entry["env_vars"]
                token = json.loads(launch.env[GRANT_ENV])["token"]
                assert token not in json.dumps(effective)
                assert tomllib.loads(callback_overrides(launch)[0])["mcp_servers"]["flowly-tools"]["required"]
                overrides = await thread_tool_overrides(client, str(runtime.workspace), callback_server_config(launch))
                assert overrides["mcp_servers"]["unrelated"]["enabled"] is False
                created = await client.request("thread/start", {"cwd": str(runtime.workspace), "config": overrides}, timeout=30)
                thread_id = created["thread"]["id"]
                await verify_thread_tools(client, thread_id, set(launch.tools))
                if names:
                    result = await client.request("mcpServer/tool/call", {
                        "threadId": thread_id, "server": "flowly-tools", "tool": "board_list", "arguments": {},
                    }, timeout=20)
                    assert not result.get("isError"), result
            finally:
                await client.close()
    assert config_path.read_text() == original


@pytest.mark.parametrize("pages", [
    [{"data": []}],
    [{"data": [{"name": "flowly-tools", "tools": {"extra": {}}}]}],
    [{"data": [{"name": "unrelated", "tools": {"extra": {}}}]}],
    [{"data": [None]}],
    [{"data": [{"name": "flowly-tools", "tools": []}]}],
    [{"data": [], "nextCursor": "same"}] * 2,
    [{"data": [], "nextCursor": 42}],
])
async def test_catalog_verification_fails_closed(pages):
    client = SimpleNamespace(request=AsyncMock(side_effect=pages))
    with pytest.raises(CodexProtocolError):
        await verify_thread_tools(client, "thread", {"board_list"})


async def test_catalog_verification_accepts_bounded_pagination():
    client = SimpleNamespace(request=AsyncMock(side_effect=[
        {"data": [{"name": "disabled", "tools": {}}], "nextCursor": "second"},
        {"data": [{"name": "flowly-tools", "tools": {"board_list": {}}}]},
    ]))
    await verify_thread_tools(client, "thread", {"board_list"})
    assert client.request.call_args_list[1].args[1]["cursor"] == "second"


async def test_persisted_callback_grant_is_not_copied_or_used():
    client = SimpleNamespace(request=AsyncMock(return_value={"config": {"mcp_servers": {
        "flowly-tools": {"env": {GRANT_ENV: "old-secret"}},
    }}}))
    with pytest.raises(CodexProtocolError, match="persisted tool grant"):
        await thread_tool_overrides(client, None, {})


async def test_thread_overrides_disable_plugins_without_copying_secrets():
    client = SimpleNamespace(request=AsyncMock(return_value={"config": {
        "mcp_servers": {"a.b": {"env": {"SECRET": "do-not-copy"}}},
        "plugins": {"installed@marketplace": {"enabled": True}},
    }}))
    overrides = await thread_tool_overrides(client, None, {"command": "owned"})
    assert overrides["mcp_servers"]["a.b"] == {"enabled": False, "required": False}
    assert overrides["plugins"]["installed@marketplace"] == {"enabled": False}
    assert "do-not-copy" not in json.dumps(overrides)


async def test_callback_context_keeps_narrowed_permissions_when_rescheduled():
    with tool_execution_scope("cli:first", allowed_tools=frozenset({"board_list"})):
        with tool_execution_scope(None, allowed_tools=frozenset({"board_list", "board_add"})):
            origin = current_tool_origin()
    assert current_tool_origin() is None
    assert origin.context.run(current_tool_origin) is origin
    assert origin.allowed_tools == frozenset({"board_list"})


async def test_managed_profile_callback_reaches_exact_owner_reverse_rpc(runtime):
    from flowly.agent.tools.shared_service import SharedBoardAddTool
    from flowly.gateway.server import _PROFILE_RUN_BINDING, GatewayServer, _ProfileRunBinding

    server = object.__new__(GatewayServer)
    server._shared_service_pending = {}
    server._shared_service_pending_clients = {}
    server._ws_clients = {"owner-socket": SimpleNamespace(closed=False)}
    requests = []

    async def send(ws, message):
        requests.append(message["params"])
        result = {"id": message["id"], "result": {"output": '{"ok":true}'}}
        server._handle_shared_service_result(result, "unrelated-socket")
        assert not server._shared_service_pending[message["id"]].done()
        server._handle_shared_service_result(result, "owner-socket")

    server._ws_send = send
    runtime.tools.register(SharedBoardAddTool(server))
    binding = _ProfileRunBinding("owner-socket", "cli:first", "research", ("research",), "parent-call")
    mark = _PROFILE_RUN_BINDING.set(binding)
    try:
        with tool_execution_scope("cli:first", allowed_tools=frozenset({"board_add"})):
            async with managed_tool_launch(runtime, "cli:first", allow_writes=True) as launch:
                async with _client(launch) as mcp:
                    result = await mcp.session.call_tool("board_add", {"title": "Shared card", "session_key": "cli:forged"})
                    assert not result.is_error
    finally:
        _PROFILE_RUN_BINDING.reset(mark)
    assert requests[0]["sourceProfile"] == "research"
    assert requests[0]["sourceSessionKey"] == "cli:first"
    assert not runtime.store.list_cards()
    assert not server._shared_service_pending


async def test_approval_callbacks_use_task_owner_despite_unrelated_active_chat():
    from flowly.codex.approval_bridge import build_codex_approval_callback

    manager = SimpleNamespace(request_and_wait=AsyncMock(return_value="allow-once"))
    callback = build_codex_approval_callback(approval_manager=manager, session_key_getter=lambda: "cli:wrong")

    async def request(key):
        with tool_execution_scope(key):
            return await callback({"method": "item/commandExecution/requestApproval", "params": {"command": "read fixture"}})

    assert await asyncio.gather(request("cli:first"), request("cli:second")) == [{"decision": "accept"}] * 2
    assert [call.args[0].session_key for call in manager.request_and_wait.call_args_list] == ["cli:first", "cli:second"]


async def test_native_remote_tools_reuse_parent_consent_and_session(runtime, monkeypatch):
    from flowly.exec.approval_manager import ApprovalManager
    from tests.mcp import test_interaction as interaction

    approvals = ApprovalManager()
    monkeypatch.setattr("flowly.exec.approval_manager._manager", approvals)
    registry = await asyncio.to_thread(interaction.discover, trust="untrusted")
    for name in registry.tool_names:
        runtime.tools.register(registry.get(name))
    observed = []

    async def deny(pending):
        observed.append(pending.session_key)
        approvals.resolve(pending.id, "deny")

    approvals.add_notify_callback(deny)
    try:
        with tool_execution_scope("cli:first", allowed_tools=frozenset({"mcp_forms_read", "mcp_forms_write"})):
            async with managed_tool_launch(runtime, "cli:first", allow_writes=True) as launch:
                async with _client(launch) as mcp:
                    assert {t.name for t in (await mcp.session.list_tools()).tools} == {"mcp_forms_read", "mcp_forms_write"}
                    assert (await mcp.session.call_tool("mcp_forms_write", {"session_key": "cli:forged"})).is_error
                    read = await mcp.session.call_tool("mcp_forms_read", {})
                    assert json.loads(read.content[0].text) == {"writes": 0}
                    assert observed == ["cli:first"]
            async with managed_tool_launch(runtime, "cli:first", allow_writes=False) as launch:
                assert launch.tools == ("mcp_forms_read",)
    finally:
        from flowly.mcp import shutdown_mcp_servers

        await asyncio.to_thread(shutdown_mcp_servers)


@pytest.mark.skipif(shutil.which("codex") is None, reason="requires installed coding client")
async def test_real_coding_turns_rotate_grants_and_resume_stored_history(runtime, tmp_path):
    """Real client/stdio/RPC paths; all model responses are local fixed fixtures."""
    from dataclasses import replace

    from aiohttp import web

    from flowly.codex.session import CodexSession

    requests = []

    async def respond(request):
        body = await request.json()
        requests.append(body)
        response_id = f"resp_fixture_{len(requests)}"
        message = {
            "type": "message", "id": f"msg_{len(requests)}", "role": "assistant",
            "status": "completed", "content": [{"type": "output_text", "text": "local-fixture-history", "annotations": []}],
        }
        events = [
            {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0, "item": message},
            {"type": "response.output_text.delta", "output_index": 0, "item_id": message["id"], "content_index": 0, "delta": "local-fixture-history"},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": {"id": response_id, "status": "completed", "output": [message],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
        ]
        return web.Response(text="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events), content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/v1/responses", respond)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    private_home = tmp_path / "coding-client"
    private_home.mkdir()
    original = (
        'model = "unchanged-model"\nmodel_provider = "local-fixture"\n'
        '[model_providers.local-fixture]\nname = "Local fixture"\n'
        f'base_url = "http://127.0.0.1:{runner.addresses[0][1]}/v1"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\n'
    )
    config_path = private_home / "config.toml"
    config_path.write_text(original)
    thread_id = None
    tokens = []
    try:
        for names, prompt in (({"board_list"}, "first fixture turn"), (set(), "second fixture turn")):
            with tool_execution_scope("cli:first", allowed_tools=frozenset(names)):
                async with managed_tool_launch(runtime, "cli:first", allow_writes=False) as launch:
                    tokens.append(json.loads(launch.env[GRANT_ENV])["token"])
                    config = replace(CodexSessionConfig(), codex_home=str(private_home), cwd=str(runtime.workspace),
                        extra_env=launch.env, config_overrides=callback_overrides(launch), turn_timeout_s=20,
                        managed_tool_callback=True, managed_tool_server=callback_server_config(launch))
                    session = CodexSession(config=config)
                    if thread_id:
                        session.set_thread_id(thread_id)
                    try:
                        result = await session.run_turn(prompt)
                        assert result.error is None, result.error
                        assert "local-fixture-history" in result.final_text
                        if thread_id:
                            assert result.thread_id == thread_id
                        thread_id = result.thread_id
                    finally:
                        launch.withdraw()
                        await session.close()
    finally:
        await runner.cleanup()
    assert len(requests) == 2
    assert all(request["model"] == "unchanged-model" for request in requests)
    assert "first fixture turn" in json.dumps(requests[1]["input"])
    assert "local-fixture-history" in json.dumps(requests[1]["input"])
    assert tokens[0] != tokens[1]
    assert config_path.read_text() == original
    assert not runtime._external_tool_bridge._grants
    # The client may persist env variable NAMES, never the transient credential.
    for path in private_home.rglob("*"):
        if path.is_file():
            assert not any(token.encode() in path.read_bytes() for token in tokens), path.name


async def test_native_remote_media_and_output_schema_survive_public_stdio(runtime, monkeypatch):
    import base64

    from mcp import types

    from flowly.mcp.tool import MCPTool

    encoded = base64.b64encode(b"bounded-media-fixture").decode()
    remote = SimpleNamespace(call_tool=AsyncMock(return_value=types.CallToolResult(
        content=[types.ImageContent(data=encoded, mimeType="image/png")],
        structuredContent={"count": 1},
    )))
    source = SimpleNamespace(name="native", session=remote, rpc_lock=asyncio.Lock(), tool_timeout=2, _config={})
    tool = MCPTool(server_task=source, remote_tool=types.Tool(name="media", description="Media fixture",
        inputSchema={"type": "object", "properties": {}},
        outputSchema={"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]},
        annotations=types.ToolAnnotations(readOnlyHint=True)))
    runtime.tools.register(tool)
    owner_loop = asyncio.get_running_loop()
    monkeypatch.setattr("flowly.mcp.client.get_mcp_loop", lambda: owner_loop)
    with tool_execution_scope("cli:first", allowed_tools=frozenset({tool.name})):
        async with managed_tool_launch(runtime, "cli:first", allow_writes=False) as launch:
            async with _client(launch) as mcp:
                definition = (await mcp.session.list_tools()).tools[0]
                assert definition.output_schema == tool.output_schema
                result = await mcp.session.call_tool(tool.name, {})
                assert not result.is_error
                assert result.structured_content == {"count": 1}
                assert result.content[0].type == "image"
                assert result.content[0].data == encoded
                # An updated schema cannot silently become write-capable within
                # an existing read-only grant.
                tool.annotations["readOnlyHint"] = False
                assert (await mcp.session.call_tool(tool.name, {})).is_error
    assert not getattr(tool, "_bridge_native_result", False)
    assert remote.call_tool.await_count == 1


async def test_managed_session_does_not_start_model_when_callback_is_missing():
    from flowly.codex.session import CodexSession
    from tests.test_codex_session import FakeCodexClient

    client = FakeCodexClient()
    client.script_response("config/read", {"config": {}})
    client.script_response("thread/start", {"thread": {"id": "thread"}})
    client.script_response("mcpServerStatus/list", {"data": []})
    session = CodexSession(config=CodexSessionConfig(managed_tool_callback=True, managed_tool_server={"command": "owned"}))
    session._client = client
    result = await session.run_turn("must not start")
    assert result.should_retire
    assert "did not initialize" in result.error
    assert "turn/start" not in [method for method, *_ in client.requests]


async def test_missing_persisted_history_is_not_silently_replaced():
    from flowly.codex.session import CodexSession
    from tests.test_codex_session import FakeCodexClient

    client = FakeCodexClient()
    client.script_error("thread/resume", -32602, "thread not found")
    session = CodexSession(config=CodexSessionConfig())
    session.set_thread_id("persisted")
    session._client = client
    result = await session.run_turn("must not start fresh")
    assert result.error
    assert [method for method, *_ in client.requests] == ["thread/resume"]
    assert session.thread_id == "persisted"


async def test_bundled_callback_does_not_use_placeholder_python(tmp_path, monkeypatch):
    from flowly.mcp.server import managed_tools

    executable = tmp_path / "flowly-bundled"
    executable.write_text("fixture")
    executable.chmod(0o700)
    monkeypatch.setattr(managed_tools.sys, "argv", [str(executable)])
    monkeypatch.setattr(managed_tools.sys, "frozen", True, raising=False)
    assert managed_tools._callback_command() == (str(executable), ["mcp", "tools"])
    executable.chmod(0o600)
    with pytest.raises(ToolBridgeError, match="unavailable"):
        managed_tools._callback_command()


async def test_catalog_pagination_shares_one_deadline(monkeypatch):
    from flowly.codex import live_tools

    async def slow(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(live_tools, "CATALOG_TIMEOUT_S", 0.01)
    with pytest.raises(TimeoutError):
        await verify_thread_tools(SimpleNamespace(request=slow), "thread", set())


async def test_agent_loop_registers_managed_callback_with_runtime_readonly_policy(runtime, monkeypatch):
    from flowly.agent.loop import AgentLoop
    from flowly.config.schema import Config

    runtime._main_config = Config()
    runtime._main_config.tools.codex_session.enabled = True
    runtime._main_config.tools.codex_session.sandbox = "read-only"
    runtime._main_config.tools.codex_session.cwd = str(runtime.workspace)
    runtime._codex_sessions = {}
    runtime._codex_active_session_key = "cli:wrong"
    monkeypatch.setattr("flowly.codex.tool_migration.migrate_flowly_tools_to_codex", lambda **_kwargs: None)
    AgentLoop._register_codex_session_tool(runtime)
    tool = runtime.tools.get("codex_session")
    assert tool is not None
    with tool_execution_scope("cli:first", allowed_tools=frozenset({"codex_session", "board_list", "board_add"})):
        async with tool._tool_bridge_factory("cli:first") as launch:
            assert launch.tools == ("board_list",)
            async with _client(launch) as mcp:
                result = await mcp.session.call_tool("board_list", {})
                assert not result.is_error
    assert not runtime._external_tool_bridge._grants
