"""A correction holds everywhere memory is stated.

The owner told a call "I'm not allergic to penicillin". The agent closed the
knowledge-graph triple, but the governed copy of that fact stayed active, so
MEMORY.md (the chat prompt) and the next call's memory said both "allergic"
and "not allergic". The same split hid behind a rejected fact the graph kept
stating, and every memory note reached the prompt twice.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from flowly.agent.hooks import HookRegistry
from flowly.agent.tools.filesystem import MemoryAppendTool
from flowly.agent.tools.knowledge_graph import KnowledgeGraphTool
from flowly.agent.tools.registry import ToolRegistry
from flowly.memory.consolidate import OP_STALE, ConsolidateOp, apply_operations
from flowly.memory.coordinator import mirror_tool_write, open_memory_governance
from flowly.memory.governance import (
    STATUS_ACTIVE,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    STATUS_STALE,
    STATUS_SUPERSEDED,
)
from flowly.memory.knowledge_graph import KnowledgeGraph
from flowly.memory.summary import (
    SENTINEL_END,
    SENTINEL_START,
    governance_states,
    withhold_retired_notes,
)


@pytest.fixture
def agent(tmp_path):
    """The agent's own wiring: shared facade, real tools, the real hook."""
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    state.mkdir()
    facade = open_memory_governance(state, workspace)
    sessions = {"current": "telegram:owner"}

    def hook(ctx):  # the body of AgentLoop._governance_post_tool
        if getattr(ctx, "success", True):
            from flowly.memory.dreamer import is_automation_session
            mirror_tool_write(facade, ctx.tool_name, ctx.params or {}, ctx.result or "",
                              source_session=sessions["current"],
                              auto_activate=not is_automation_session(sessions["current"]))

    hooks = HookRegistry()
    hooks.register("post_tool_call", hook)
    tools = ToolRegistry(hooks=hooks)
    tools.register(MemoryAppendTool(workspace=workspace))
    tools.register(KnowledgeGraphTool(state_dir=state))
    yield facade, tools, workspace, state, sessions
    facade.gov.close()


def memory_md(workspace):
    return (workspace / "memory" / "MEMORY.md").read_text()


def triple_is_current(state, triple_id):
    with sqlite3.connect(state / "knowledge_graph.sqlite3") as conn:
        return conn.execute("SELECT valid_to IS NULL FROM triples WHERE id=?", (triple_id,)).fetchone()[0] == 1


async def add(tools, predicate="allergic_to", obj="penicillin"):
    return await tools.execute("knowledge_graph", {"action": "add", "subject": "Hakan", "predicate": predicate,
                                                  "object": obj, "subject_type": "person"})


async def test_an_invalidated_fact_stops_being_memory_everywhere(agent):
    facade, tools, workspace, state, _ = agent
    await add(tools)
    [fact] = facade.gov.list_items(status=STATUS_ACTIVE)
    facade.refresh()
    assert "Hakan allergic to penicillin" in memory_md(workspace)

    result = await tools.execute("knowledge_graph", {"action": "invalidate", "subject": "Hakan",
                                                    "predicate": "allergic_to", "object": "penicillin"})
    assert fact.ref_id in result
    await add(tools, "not_allergic_to")
    facade.refresh_if_dirty()

    assert facade.gov.get_item(fact.id).status == STATUS_STALE
    text = memory_md(workspace)
    assert "Hakan not allergic to penicillin" in text
    assert "Hakan allergic to penicillin" not in text
    assert not re.search(r"(?<!not_)allergic_to → penicillin", text)  # nor the graph

    # Undo brings the fact and its triple back together.
    facade.undo(fact.id)
    assert facade.gov.get_item(fact.id).status == STATUS_ACTIVE and triple_is_current(state, fact.ref_id)


async def test_an_invalidate_that_closed_nothing_retires_nothing(agent):
    facade, tools, _, _, _ = agent
    await add(tools)
    await tools.execute("knowledge_graph", {"action": "invalidate", "subject": "Hakan",
                                           "predicate": "allergic_to", "object": "aspirin"})
    assert [i.text for i in facade.gov.list_items(status=STATUS_ACTIVE)] == ["Hakan allergic to penicillin"]


async def test_a_rejected_fact_stops_being_current_in_the_graph(agent):
    facade, tools, workspace, state, _ = agent
    await add(tools, "loves", "dark mode")
    [fact] = facade.gov.list_items(status=STATUS_ACTIVE)
    facade.reject(fact.id)
    facade.refresh()
    assert not triple_is_current(state, fact.ref_id)
    assert "dark mode" not in memory_md(workspace)


def test_a_fact_consolidation_finds_outdated_is_closed_in_the_graph(agent):
    facade, _, _, state, _ = agent
    graph = KnowledgeGraph(str(state / "knowledge_graph.sqlite3"))
    triple = graph.add_triple("Hakan", "lives_in", "Ankara")
    fact = facade.ingest_kg_fact("Hakan", "lives_in", "Ankara", triple)
    apply_operations(facade.gov, [ConsolidateOp(op=OP_STALE, item_id=fact.id, reason="moved")],
                     kg_mirror=facade.kg_mirror)
    assert facade.gov.get_item(fact.id).status == STATUS_STALE
    assert not triple_is_current(state, triple)


def test_facts_and_triples_written_out_of_step_are_repaired(agent):
    facade, _, _, state, _ = agent
    graph = KnowledgeGraph(str(state / "knowledge_graph.sqlite3"))
    # What the owner's memory held: an invalidated fact still active, a
    # rejected one still current, and two items that must be left alone.
    closed = graph.add_triple("Hakan", "allergic_to", "penicillin")
    stale_fact = facade.ingest_kg_fact("Hakan", "allergic_to", "penicillin", closed)
    graph.invalidate("Hakan", "allergic_to", "penicillin")
    rejected_triple = graph.add_triple("Hakan", "loves", "dark mode")
    rejected = facade.ingest_kg_fact("Hakan", "loves", "dark mode", rejected_triple)
    facade.gov.transition(rejected.id, STATUS_REJECTED, actor="user", reason="panel without mirror")
    shared = graph.add_triple("Hakan", "uses", "Ghostty")
    old_copy = facade.ingest_kg_fact("Hakan", "uses", "Ghostty", shared)
    facade.gov.transition(old_copy.id, STATUS_SUPERSEDED, actor="system", reason="test")
    facade.gov.add_item(kind="fact", text="Hakan uses Ghostty", status=STATUS_ACTIVE,
                        ref_kind="kg_triple", ref_id=shared)
    missing = facade.gov.add_item(kind="fact", text="Gone from the graph", status=STATUS_ACTIVE,
                                  ref_kind="kg_triple", ref_id="t_missing")

    assert facade.reconcile_kg_triples() == {"staled": 1, "closed": 1}
    assert facade.gov.get_item(stale_fact.id).status == STATUS_STALE
    assert not triple_is_current(state, rejected_triple)
    assert triple_is_current(state, shared)  # a live item still stands on it
    assert facade.gov.get_item(missing.id).status == STATUS_ACTIVE
    assert facade.reconcile_kg_triples() == {"staled": 0, "closed": 0}


def test_an_unreadable_graph_repairs_nothing(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "knowledge_graph.sqlite3").write_text("not a database")
    facade = open_memory_governance(state, tmp_path / "workspace")
    item = facade.gov.add_item(kind="fact", text="Hakan uses Codex", status=STATUS_ACTIVE,
                               ref_kind="kg_triple", ref_id="t_1")
    assert facade.reconcile_kg_triples() == {"staled": 0, "closed": 0}
    assert facade.gov.get_item(item.id).status == STATUS_ACTIVE
    facade.gov.close()


async def test_the_graph_summary_leaves_out_facts_waiting_for_review(agent):
    facade, tools, _, _, sessions = agent
    sessions["current"] = "cron:nightly"  # an autonomous write waits for review
    await add(tools, "works_at", "Unverified Corp")
    assert facade.gov.list_items()[0].status == STATUS_NEEDS_REVIEW
    assert "Unverified Corp" not in facade.kg_summary_fn()


async def test_a_memory_note_is_stated_once(agent):
    facade, tools, workspace, _, _ = agent
    await tools.execute("memory_append", {"content": "Prefers Ghostty over iTerm."})
    facade.refresh_if_dirty()
    assert memory_md(workspace).count("Prefers Ghostty over iTerm.") == 1
    # Removed from the file by hand, the note is still memory: its governed
    # copy comes back in the generated block.
    text = memory_md(workspace)
    start = text.index(SENTINEL_START)
    (workspace / "memory" / "MEMORY.md").write_text(text[start:])
    facade.refresh()
    assert memory_md(workspace).count("Prefers Ghostty over iTerm.") == 1


def test_the_prompt_leaves_out_a_note_that_is_no_longer_memory():
    md = (f"<!-- 2026-10-04 15:14 -->\nHakan is allergic to penicillin.\n\nThe studio is on the 3rd floor.\n\n"
          f"<!-- 2026-10-04 15:15 -->\nCodex\n\nHakan uses Codex for coding.\n\n"
          f"{SENTINEL_START}\n- Hakan is allergic to penicillin.\n{SENTINEL_END}\n")
    states = [("rejected", "Hakan is allergic to penicillin."), ("stale", "Codex"),
              ("active", "The studio is on the 3rd floor.")]
    shown = withhold_retired_notes(md, states)
    assert "studio" in shown and "Hakan uses Codex for coding." in shown
    # The whole short note goes; a longer note that mentions it stays.
    assert "\nCodex\n" not in shown
    assert shown.count("allergic") == 1  # only the generated block, untouched
    # Nothing retired, nothing changed.
    assert withhold_retired_notes(md, [("active", "x")]) == md
    # The same text still active elsewhere is not withheld.
    assert "allergic" in withhold_retired_notes("Hakan is allergic to penicillin.",
                                                 [("rejected", "Hakan is allergic to penicillin."),
                                                  ("active", "Hakan is allergic to penicillin.")])


def test_an_unreadable_governance_index_reports_none(tmp_path):
    path = tmp_path / "memory_governance.sqlite3"
    path.write_text("not a database")
    assert governance_states(path) is None
    assert governance_states(tmp_path / "absent.sqlite3") == []


def test_the_chat_prompt_states_memory_once_and_without_retired_notes(agent, monkeypatch):
    from flowly.agent.context import ContextBuilder

    facade, _, workspace, state, _ = agent
    monkeypatch.setattr("flowly.config.loader.get_data_dir", lambda: state)
    graph = KnowledgeGraph(str(state / "knowledge_graph.sqlite3"))
    facade.ingest_kg_fact("Hakan", "works_at", "Nocetic", graph.add_triple("Hakan", "works_at", "Nocetic"))
    retired = facade.ingest_append("Hakan is allergic to penicillin.")
    facade.reject(retired.id)
    note = workspace / "memory" / "MEMORY.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("<!-- 2026-10-04 15:14 -->\nHakan is allergic to penicillin.\n\nThe studio is on the 3rd floor.\n")
    facade.refresh()

    block = ContextBuilder(workspace)._compute_memory_block(memory_search_enabled=True)
    assert block.count("works_at → Nocetic") == 1  # the graph, once
    assert "studio" in block and "allergic" not in block


def test_a_call_states_a_note_once(tmp_path):
    from datetime import datetime

    from flowly.live_voice.memory_snapshot import VoiceMemorySnapshot

    workspace = tmp_path / "workspace"
    facade = open_memory_governance(tmp_path, workspace)
    (workspace / "memory").mkdir(parents=True, exist_ok=True)
    (workspace / "memory" / "MEMORY.md").write_text("<!-- 2026-10-04 15:14 -->\nPrefers Ghostty over iTerm.\n\nThe studio is on the 3rd floor.\n")
    facade.ingest_append("Prefers Ghostty over iTerm.")
    reader = VoiceMemorySnapshot(workspace, state_db=lambda name: tmp_path / name, profile=lambda: ("default", "bot-1"),
                                 search_enabled=lambda: True, today=lambda: datetime(2026, 10, 4))
    result = reader.snapshot({})
    stated = " ".join(section["text"] for section in result["sections"])
    assert stated.count("Prefers Ghostty over iTerm.") == 1 and "studio" in stated
    facade.gov.close()


def test_a_decision_in_the_memory_panel_reaches_the_prompt_at_once(tmp_path, monkeypatch):
    from flowly.channels import feature_rpc

    home, workspace = tmp_path / "home", tmp_path / "home" / "workspace"
    (workspace / "memory").mkdir(parents=True)
    monkeypatch.setattr(feature_rpc, "get_flowly_home", lambda: home)
    monkeypatch.setattr(feature_rpc, "workspace_dir", lambda: workspace)
    monkeypatch.setattr(feature_rpc, "state_db", lambda name: home / name)
    graph = KnowledgeGraph(str(home / "knowledge_graph.sqlite3"))
    facade = open_memory_governance(home, workspace)
    fact = facade.ingest_kg_fact("Hakan", "loves", "dark mode", graph.add_triple("Hakan", "loves", "dark mode"))
    facade.refresh()
    assert "dark mode" in (workspace / "memory" / "MEMORY.md").read_text()

    feature_rpc.memory_gov("reject", {"id": fact.id})
    assert "dark mode" not in (workspace / "memory" / "MEMORY.md").read_text()
    with sqlite3.connect(home / "knowledge_graph.sqlite3") as conn:
        assert conn.execute("SELECT valid_to FROM triples WHERE id=?", (fact.ref_id,)).fetchone()[0]
    facade.gov.close()
