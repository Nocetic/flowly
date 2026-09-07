"""An empty selection must never expand an owner's MCP permissions."""
from types import SimpleNamespace

import pytest

from flowly.config.schema import MCPServerToolsFilter
from flowly.integrations.mcp_io import _tool_filter_summary
from flowly.mcp.client import _filter_remote_tool, _utility_tools_for_server


@pytest.mark.parametrize("policy,expected", [
    ({}, True),
    ({"include": ["read"]}, True),
    ({"include": ["write"]}, False),
    ({"exclude": ["read"]}, False),
    ({"mode": "all"}, True),
    ({"mode": "selected", "include": ["read"]}, True),
    ({"mode": "selected", "include": []}, False),
    ({"mode": "none", "include": ["read"]}, False),
    ({"mode": "unknown"}, False),
])
def test_explicit_and_legacy_admission(policy, expected):
    assert _filter_remote_tool({"tools": policy}, "read") is expected


def test_none_disables_resource_and_prompt_tools_too():
    task = SimpleNamespace(name="demo", capabilities=None)
    assert _utility_tools_for_server(task, {
        "tools": {"mode": "none", "resources": True, "prompts": True},
    }) == []


def test_explicit_empty_selection_survives_config_serialization():
    policy = MCPServerToolsFilter(mode="selected", include=[]).model_dump()
    assert policy["mode"] == "selected"
    assert not _filter_remote_tool({"tools": policy}, "newly_discovered_tool")
    assert _tool_filter_summary(policy) == "none"


def test_unknown_mode_is_rejected_by_schema():
    with pytest.raises(ValueError):
        MCPServerToolsFilter(mode="unknown")
