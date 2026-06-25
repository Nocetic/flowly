"""Integration: post_tool_call hook → governance ingest (the live chat path).

Drives the real ToolRegistry + HookRegistry firing path (no full AgentLoop) to
prove that a memory_append / knowledge_graph add flows into the governance store
and regenerates MEMORY.md — the same wiring loop._governance_post_tool installs.
"""

from __future__ import annotations

import re

import pytest

from flowly.agent.hooks import HookRegistry
from flowly.agent.memory import MemoryStore
from flowly.agent.tools.filesystem import MemoryAppendTool
from flowly.agent.tools.knowledge_graph import KnowledgeGraphTool
from flowly.agent.tools.registry import ToolRegistry
from flowly.memory.coordinator import MemoryGovernance
from flowly.memory.governance import GovernanceStore, STATUS_ACTIVE, STATUS_SUPERSEDED
from flowly.memory.kg_mirror import SqliteKGMirror


def _make(tmp_path):
    ws = tmp_path / "ws"
    state = ws / ".flowly_state"
    state.mkdir(parents=True, exist_ok=True)
    gov = GovernanceStore(state / "memory_governance.sqlite3")
    kg_path = state / "knowledge_graph.sqlite3"
    mg = MemoryGovernance(
        gov, memory_store=MemoryStore(ws), kg_mirror=SqliteKGMirror(str(kg_path))
    )

    def hook(ctx):  # mirrors loop._governance_post_tool
        if not getattr(ctx, "success", True):
            return
        name = getattr(ctx, "tool_name", "")
        params = getattr(ctx, "params", {}) or {}
        if name == "memory_append":
            mg.ingest_append(params.get("content", ""))
        elif name == "knowledge_graph" and params.get("action") == "add":
            m = re.search(r"id:\s*(t_[^)\s]+)", getattr(ctx, "result", "") or "")
            if m:
                mg.ingest_kg_fact(params.get("subject", ""), params.get("predicate", ""),
                                  params.get("object", ""), m.group(1))

    hooks = HookRegistry()
    hooks.register("post_tool_call", hook)
    reg = ToolRegistry(hooks=hooks)
    reg.register(MemoryAppendTool(workspace=ws))
    reg.register(KnowledgeGraphTool(state_dir=state))
    return reg, gov, mg, ws


async def test_memory_append_flows_into_governance(tmp_path):
    reg, gov, mg, ws = _make(tmp_path)
    await reg.execute("memory_append", {"content": "prefers dark mode"})
    actives = gov.list_items(status=STATUS_ACTIVE)
    assert len(actives) == 1
    assert actives[0].text == "prefers dark mode"
    assert "prefers dark mode" in (ws / "memory" / "MEMORY.md").read_text()


async def test_subagent_registry_also_fires_governance_hook(tmp_path):
    """Self-review runs in a SubagentToolRegistry; its memory writes must be
    governed too (same hook mechanism)."""
    from flowly.agent.hooks import HookRegistry
    from flowly.agent.subagent import SubagentToolRegistry

    ws = tmp_path / "ws"
    state = ws / ".flowly_state"
    state.mkdir(parents=True, exist_ok=True)
    gov = GovernanceStore(state / "memory_governance.sqlite3")
    mg = MemoryGovernance(gov, memory_store=MemoryStore(ws))

    def hook(ctx):
        if getattr(ctx, "tool_name", "") == "memory_append" and getattr(ctx, "success", True):
            mg.ingest_append(getattr(ctx, "params", {}).get("content", ""))

    hooks = HookRegistry()
    hooks.register("post_tool_call", hook)
    reg = SubagentToolRegistry(hooks=hooks)
    reg.register(MemoryAppendTool(workspace=ws))

    await reg.execute("memory_append", {"content": "self-review captured this"})
    actives = gov.list_items(status=STATUS_ACTIVE)
    assert len(actives) == 1 and actives[0].text == "self-review captured this"


async def test_kg_add_flows_and_supersedes(tmp_path):
    reg, gov, mg, ws = _make(tmp_path)
    await reg.execute("knowledge_graph", {
        "action": "add", "subject": "Hakan", "predicate": "email",
        "object": "old@x.com", "subject_type": "person",
    })
    await reg.execute("knowledge_graph", {
        "action": "add", "subject": "Hakan", "predicate": "email",
        "object": "new@x.com", "subject_type": "person",
    })
    facts = [i for i in gov.list_items() if i.kind == "fact"]
    active = [f for f in facts if f.status == STATUS_ACTIVE]
    superseded = [f for f in facts if f.status == STATUS_SUPERSEDED]
    assert len(active) == 1 and "new@x.com" in active[0].text
    assert len(superseded) == 1 and "old@x.com" in superseded[0].text
