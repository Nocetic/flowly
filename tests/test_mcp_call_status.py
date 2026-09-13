"""MCP failures remain failures through chat status and plugin hooks."""

import json

import pytest

from flowly.agent.tool_result_status import mcp_tool_result_failed


@pytest.mark.parametrize(
    "name,payload,failed",
    [
        ("mcp_service_read", {"isError": True, "error": "Invalid URL"}, True),
        ("mcp_service_read", {"isError": True, "content": []}, True),
        ("mcp_service_read", {"error": "Connection reset"}, True),
        ("mcp_service_read", {"error": "Invalid arguments", "code": "INVALID"}, True),
        ("mcp_connection", {"status": "failed", "connected": False}, True),
        ("mcp_connection", {"code": "INVALID", "error": "No connection"}, True),
        ("mcp_connection", {"status": "complete", "connected": True}, False),
        ("mcp_service_read", {"isError": False, "error": "user data"}, False),
        ("mcp_service_read", {"result": {"error": "user data"}}, False),
        ("mcp_service_read", {"error": "user data", "ok": True}, False),
        ("mcp_service_read", ["error"], False),
        ("email", {"isError": True}, False),
    ],
)
def test_mcp_status_envelopes(name, payload, failed):
    assert mcp_tool_result_failed(name, json.dumps(payload)) is failed


@pytest.mark.parametrize("result", ["not json", "{bad json", "", "null"])
def test_mcp_status_tolerates_non_envelopes(result):
    assert not mcp_tool_result_failed("mcp_service_read", result)


@pytest.mark.parametrize(
    "name,result,failed",
    [
        ("mcp_service_executeRead", '{"isError":true,"error":"Invalid URL"}', True),
        ("mcp_service_executeRead", '{"result":{"error":"user data"}}', False),
        ("browser_tab", '{"error":"Page action failed"}', True),
        ("read_file", "Error: not found", True),
        ("read_file", "file contents", False),
    ],
)
def test_chat_status_keeps_mcp_and_existing_error_handling(name, result, failed):
    from flowly.agent.loop import _tool_result_failed

    assert _tool_result_failed(name, result) is failed


@pytest.mark.parametrize("failed", [True, False])
async def test_post_tool_hook_receives_actual_mcp_status(failed):
    from flowly.agent.hooks import HookRegistry
    from flowly.agent.tools.base import Tool
    from flowly.agent.tools.registry import ToolRegistry

    raw = json.dumps({"error": "Missing site", "isError": True} if failed else {"result": "ok"})

    class PeerTool(Tool):
        name = "mcp_service_read"
        description = "Test read"
        parameters = {"type": "object"}

        async def execute(self, **kwargs):
            return raw

    statuses = []
    hooks = HookRegistry()
    hooks.register("post_tool_call", lambda ctx: statuses.append(ctx.success))
    registry = ToolRegistry(hooks=hooks)
    registry.register(PeerTool())
    assert await registry.execute("mcp_service_read", {}) == raw
    assert statuses == [not failed]


@pytest.mark.parametrize(
    "available,reachable,present",
    [
        ({"mcp_service_executeRead"}, None, True),
        ({"tool_search", "tool_call"}, {"mcp_service_executeRead"}, True),
        ({"mcp_connection"}, None, False),
        ({"read_file"}, None, False),
        (None, None, False),
        ({"mcp_service_executeRead"}, set(), False),
    ],
)
def test_mcp_call_guidance_is_conditional(tmp_path, monkeypatch, available, reachable, present):
    from flowly.agent.context import ContextBuilder

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = ContextBuilder(workspace)
    prompt = context.build_system_prompt(
        skip_memory=True,
        skip_context_files=True,
        available_tools=available,
        reachable_tools=reachable,
    )
    assert prompt.count("## Connected MCP services") == int(present)
    if present:
        assert "available, permitted" in prompt
        assert "Never invent an ID" in prompt
        assert "do not repeat unchanged failed calls" in prompt


async def test_chat_loop_records_mcp_error_as_failed_operation(tmp_path, monkeypatch):
    from flowly.agent.loop import AgentLoop
    from flowly.agent.tools.base import Tool
    from flowly.agent.tools.registry import ToolRegistry
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("flowly.auth.xai_oauth.load_token_payload", lambda: None)
    raw = '{"error":"Failed to fetch cloud ID: Invalid URL","isError":true}'
    invocations = []

    class PeerTool(Tool):
        name = "mcp_service_executeRead"
        description = "Read service issues"
        parameters = {"type": "object"}

        async def execute(self, **kwargs):
            invocations.append(kwargs)
            return raw

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="test")
            self.searched = False

        def get_default_model(self):
            return "test/model"

        async def chat(self, *args, **kwargs):
            if invocations:
                return LLMResponse(content="The operation failed; site information is missing.")
            visible = {definition["function"]["name"] for definition in kwargs["tools"]}
            if PeerTool.name in visible:
                name, arguments = PeerTool.name, {}
            elif not self.searched:
                self.searched = True
                name, arguments = "tool_search", {"query": "Read service issues"}
            else:
                name, arguments = "tool_call", {"name": PeerTool.name, "arguments": {}}
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id=name, name=name, arguments=arguments)]
            )

    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(),
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=4,
        soft_warn_at_iteration=0,
    )
    loop.tools = ToolRegistry()
    loop.tools.register(PeerTool(), toolset="mcp")
    _, results, _, _, _ = await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "Read service issues."},
        ],
        action_turn=False,
        turn_content="Read service issues.",
        session_key="web:test",
        tool_platform="web",
    )
    operation = next(result for result in results if result["tool"] == PeerTool.name)
    assert operation["success"] is False
    assert operation["result"] == raw
    assert invocations == [{}]
