"""Linear provider precedence across legacy API and MCP connections."""

from flowly.agent.context import ContextBuilder
from flowly.agent.loop import AgentLoop
from flowly.agent.tools.linear import LinearTool
from flowly.agent.tools.registry import ToolRegistry
from flowly.config.schema import Config, MCPServerConfig


def _loop(config: Config) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._main_config = config
    loop.tools = ToolRegistry()
    return loop


def test_enabled_linear_mcp_shadows_retained_legacy_api_key() -> None:
    config = Config()
    config.integrations.linear.api_key = "stale-personal-key"
    config.mcp_servers = {
        "linear": MCPServerConfig(url="https://mcp.linear.app/mcp", auth="oauth"),
    }
    loop = _loop(config)
    loop.tools.register(LinearTool(api_key="older-key"))

    assert loop.sync_linear_tool() is False
    assert loop.tools.get("linear") is None


def test_disabling_linear_mcp_restores_retained_legacy_fallback() -> None:
    config = Config()
    config.integrations.linear.api_key = "personal-key"
    config.mcp_servers = {
        "linear": MCPServerConfig(
            url="https://mcp.linear.app/mcp",
            auth="oauth",
            enabled=False,
        ),
    }
    loop = _loop(config)

    assert loop.sync_linear_tool() is True
    assert isinstance(loop.tools.get("linear"), LinearTool)


def test_removing_linear_mcp_restores_retained_legacy_fallback() -> None:
    config = Config()
    config.integrations.linear.api_key = "personal-key"
    config.mcp_servers = {
        "linear": MCPServerConfig(url="https://mcp.linear.app/mcp", auth="oauth"),
    }
    loop = _loop(config)

    assert loop.sync_linear_tool() is False
    config.mcp_servers = {}
    assert loop.sync_linear_tool() is True
    assert isinstance(loop.tools.get("linear"), LinearTool)


def test_linear_prompt_guidance_follows_routed_registry_not_saved_key(tmp_path) -> None:
    builder = ContextBuilder(workspace=tmp_path)

    assert builder._has_linear_tools(frozenset({"linear"})) is True
    assert builder._has_linear_tools(frozenset({"mcp_linear_list_issues"})) is False
