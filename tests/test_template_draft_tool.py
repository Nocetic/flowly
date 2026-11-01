"""``template_draft``: an agent drafts a template of itself, a card for its owner.

The draft leaves as one ``template.drafted`` event (the server keeps it and
puts the card in the chat); a refused draft leaves nothing.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import flowly.profile as profiles
from flowly.agent.tool_context import tool_execution_scope
from flowly.agent.tools.templates import DraftTemplateTool
from flowly.agent_home import HOME_SESSION
from flowly.channels.flowly_server import PROFILE_CHAT_EVENTS
from flowly.templates import drafts
from flowly.templates.recipe import validate_recipe


@pytest.fixture
def agent(tmp_path, monkeypatch):
    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    home = profiles.create_profile("research", display_name="Researcher", description="Finds sources.")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    workspace = home / "workspace"
    (workspace / "skills" / "brief").mkdir(parents=True)
    (workspace / "skills" / "brief" / "SKILL.md").write_text("---\ndescription: A brief.\n---\nWrite a brief.\n")
    sent: list[tuple[str, dict]] = []

    async def carry(session_key: str, data: dict) -> None:
        sent.append((session_key, data))

    drafts.reset_for_tests()
    drafts.add_drafted_callback(carry)
    yield SimpleNamespace(workspace=workspace, cron_service=None, _main_config=None), sent
    drafts.reset_for_tests()


async def _run(tool, session_key, **params):
    with tool_execution_scope(session_key):
        return json.loads(await tool.execute(**params))


async def test_draft_emits_one_event_with_a_valid_recipe(agent):
    loop, sent = agent
    result = await _run(DraftTemplateTool(agent=loop), HOME_SESSION)
    assert result["status"] == "drafted"
    assert result["counts"] == {"skills": 1, "routines": 0, "apps": 0, "memories": 0}
    assert len(sent) == 1
    session_key, data = sent[0]
    assert session_key == HOME_SESSION == data["sessionKey"]
    assert data["draftId"] == result["draftId"]
    assert validate_recipe(data["recipe"])["profile"]["displayName"] == "Researcher"
    assert isinstance(data["createdAt"], int)


async def test_refused_draft_emits_nothing(agent):
    loop, sent = agent
    (loop.workspace / "SOUL.md").write_text("Use sk-" + "Q" * 30)
    result = await _run(DraftTemplateTool(agent=loop), HOME_SESSION)
    assert (result["status"], result["code"], result["field"]) == ("refused", "CONTAINS_SECRET", "profile.soul")
    assert sent == []


async def test_named_agent_drafts_only_in_its_own_conversation(agent):
    loop, sent = agent
    result = await _run(DraftTemplateTool(agent=loop), "web:some-other-chat")
    assert result["status"] == "refused" and result["code"] == "WRONG_CONVERSATION"
    assert sent == []


async def test_a_turn_nobody_watches_cannot_draft(agent):
    loop, sent = agent
    result = await _run(DraftTemplateTool(agent=loop), "subagent:research:thread-1")
    assert result["status"] == "refused"
    assert sent == []


async def test_no_server_means_no_draft(agent):
    loop, sent = agent
    drafts.reset_for_tests()
    result = await _run(DraftTemplateTool(agent=loop), HOME_SESSION)
    assert (result["status"], result["code"]) == ("refused", "TEMPLATES_UNAVAILABLE")


def test_named_agent_event_reaches_the_server():
    assert "template.drafted" in PROFILE_CHAT_EVENTS
