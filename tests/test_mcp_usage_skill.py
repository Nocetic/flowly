"""MCP workflow routing and bundled skill/reference delivery."""

import json

import pytest

from flowly.agent.context import ContextBuilder, _MCP_SKILL_GUIDANCE
from flowly.agent.skills import clear_skills_snapshot
from flowly.agent.tools.skill_view import SkillViewTool


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    path = tmp_path / "workspace"
    path.mkdir()
    clear_skills_snapshot()
    yield path
    clear_skills_snapshot()


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize(
    "available,reachable,present",
    [
        ({"mcp_connection", "skill_view"}, None, True),
        ({"mcp_service_read", "skill_view"}, None, True),
        ({"tool_search", "tool_call", "skill_view"}, {"mcp_service_read"}, True),
        ({"mcp_connection"}, None, False),
        ({"mcp_service_read"}, None, False),
        ({"skill_view", "web_search"}, None, False),
        ({"skill_view", "mcp_service_read"}, set(), False),
        (None, None, False),
    ],
)
def test_workflow_routing_requires_mcp_and_skill_reader(
    workspace, deferred, available, reachable, present,
):
    context = ContextBuilder(workspace)
    prompt = context.build_system_prompt(
        skip_memory=True,
        skip_context_files=True,
        available_tools=available,
        reachable_tools=reachable,
        defer_dynamic_guidance=deferred,
    )
    assert prompt.count(_MCP_SKILL_GUIDANCE) == int(present)


async def test_bundled_workflow_is_searchable_and_research_loads_on_demand(workspace):
    tool = SkillViewTool(workspace=workspace)
    search = json.loads(await tool.execute(action="search", query="mcp-usage"))
    assert any(skill["name"] == "mcp-usage" for skill in search["skills"])

    skill = json.loads(await tool.execute(name="mcp-usage"))
    assert skill["readiness"] == "available"
    assert skill["missing_requirements"] == []
    assert "references/research.md" in skill["linked_files"]["references"]

    reference = json.loads(await tool.execute(
        name="mcp-usage", file_path="references/research.md",
    ))
    assert "error" not in reference
    assert reference["content"]
    assert reference["content"] not in skill["content"]
