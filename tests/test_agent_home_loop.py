"""The agent's first message and setup turns, through the real agent loop."""

from __future__ import annotations

import json
from typing import Any

import pytest

import flowly.profile as profiles
from flowly.agent.loop import AgentLoop
from flowly.agent_home import (
    AGENT_INTRODUCTION,
    HOME_SESSION,
    SETUP_TOOLS,
    claim_introduction,
    resolve_home,
)
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from flowly.session.manager import SessionManager


class _Scripted(LLMProvider):
    def __init__(self, script: list[LLMResponse]) -> None:
        super().__init__(api_key="test")
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self.script.pop(0) if self.script else LLMResponse(content="Done.")


def _call(name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(id=f"{name}-1", name=name, arguments=arguments)])


@pytest.fixture
def agent(tmp_path, monkeypatch):
    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    home = profiles.create_profile("planner", description="Weekly planning for a small team")
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    return home


def _loop(home, provider) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=home / "workspace",
        main_config=Config(),
        max_iterations=4,
        soft_warn_at_iteration=0,
    )


def _state(home) -> dict:
    return json.loads((home / "agent-home.json").read_text())


def _tool_names(call: dict) -> set[str]:
    return {tool["function"]["name"] for tool in call.get("tools") or []}


async def _introduce(loop: AgentLoop) -> str:
    resolve_home({"locale": "en"})
    claim = claim_introduction({})
    await loop.process_direct(
        claim["prompt"],
        session_key=HOME_SESSION,
        run_id=claim["runId"],
        extra_metadata={AGENT_INTRODUCTION: True, "allowed_tools": ["agent_setup_ask"]},
    )
    return claim["runId"]


@pytest.mark.asyncio
async def test_agent_speaks_first_with_hidden_trigger_and_one_tool(agent):
    provider = _Scripted([
        _call("agent_setup_ask", question="Where should we start?", options=["This week's priorities", "A team routine"]),
        LLMResponse(content="Hi! I'm your planner for the team."),
    ])
    loop = _loop(agent, provider)
    try:
        run_id = await _introduce(loop)
    finally:
        loop.stop()

    assert _tool_names(provider.calls[0]) == {"agent_setup_ask"}
    first_user = next(message for message in provider.calls[0]["messages"] if message["role"] == "user")
    assert "Owner-supplied role context" in str(first_user["content"])
    state = _state(agent)
    assert state["intro"]["state"] == "done" and state["intro"]["runId"] == run_id
    assert [option["label"] for option in state["pendingAsk"]["options"]] == [
        "This week's priorities", "A team routine",
    ]
    sm = SessionManager(agent / "workspace")
    displayed = [message for message in sm.get_full_messages(HOME_SESSION) if message.get("role") in ("user", "assistant") and not message.get("tool_calls")]
    assert [message["role"] for message in displayed] == ["assistant"]
    assert displayed[0]["content"] == "Hi! I'm your planner for the team."
    # The trigger stays in the model's own context so later turns know why it spoke.
    history = sm.get_or_create(HOME_SESSION).get_history()
    assert history[0]["role"] == "user"
    assert sm.get_or_create(HOME_SESSION).metadata.get("title") in (None, "")


@pytest.mark.asyncio
async def test_failed_introduction_leaves_only_the_static_welcome(agent):
    provider = _Scripted([LLMResponse(content="Invalid API key", finish_reason="error")])
    loop = _loop(agent, provider)
    try:
        await _introduce(loop)
    finally:
        loop.stop()
    state = _state(agent)
    assert state["intro"]["state"] == "fallback"
    assert state["pendingAsk"]["options"]
    messages = SessionManager(agent / "workspace").get_full_messages(HOME_SESSION)
    assert [message.get("kind") for message in messages] == ["agent_introduction"]
    assert "Invalid API key" not in json.dumps(messages)


@pytest.mark.asyncio
async def test_tapped_planning_cannot_end_setup_through_the_model(agent):
    provider = _Scripted([
        _call("agent_setup_ask", question="Where should we start?", options=["Planning", "Research"]),
        LLMResponse(content="Hi!"),
        # The regression: the model tries to finish setup on a tapped choice.
        _call("agent_setup_finish", reason="task"),
        _call("agent_setup_ask", question="What should we plan?", options=["Work projects", "Weekly routine"]),
        LLMResponse(content="Planning it is. What should we plan?"),
    ])
    loop = _loop(agent, provider)
    try:
        await _introduce(loop)
        offered = _state(agent)["pendingAsk"]
        planning = next(option for option in offered["options"] if option["label"] == "Planning")
        await loop.process_direct(
            "Planning",
            session_key=HOME_SESSION,
            run_id="tap-planning",
            extra_metadata={"setup_answer": {"askId": offered["id"], "optionId": planning["id"]}},
        )
    finally:
        loop.stop()

    system = provider.calls[2]["messages"][0]["content"]
    assert 'The owner chose "Planning"' in system
    assert "not a task" in system
    refused = next(
        message for message in provider.calls[3]["messages"]
        if message.get("role") == "tool" and "agent_setup_finish" in str(message.get("name", "")) or "NOT_A_TASK" in str(message.get("content", ""))
    )
    assert "NOT_A_TASK" in str(refused["content"])
    state = _state(agent)
    assert state["setup"] == "active"
    assert state["answers"][-1]["choice"] == "Planning"
    assert [option["label"] for option in state["pendingAsk"]["options"]] == ["Work projects", "Weekly routine"]


@pytest.mark.asyncio
async def test_setup_tools_are_absent_outside_the_active_home(agent):
    provider = _Scripted([LLMResponse(content="Sure.")] * 3)
    loop = _loop(agent, provider)
    try:
        resolve_home({})
        await loop.process_direct("hello", session_key="desktop:chat-old", run_id="other")
        await loop.process_direct("hello", session_key=HOME_SESSION, run_id="home-1")
    finally:
        loop.stop()
    # Auto-titling shares the provider; only tool-bearing calls are turns.
    other, home = [call for call in provider.calls if call.get("tools")][:2]
    assert not (_tool_names(other) & SETUP_TOOLS)
    assert _tool_names(home) >= SETUP_TOOLS
    # The generic get-to-know-you offer would compete with setup questions.
    assert "Getting to know the user" in other["messages"][0]["content"]
    assert "Getting to know the user" not in home["messages"][0]["content"]


@pytest.mark.asyncio
async def test_card_row_follows_the_agents_words_and_survives_saving(agent):
    provider = _Scripted([
        _call("agent_setup_propose_card", role="Team planner", focus="Weekly priorities", style="Short"),
        LLMResponse(content="Here is how I would work with you."),
        LLMResponse(content="Saved. Shall we list this week's priorities?"),
    ])
    loop = _loop(agent, provider)
    try:
        resolve_home({"locale": "en"})
        await loop.process_direct("I run a small team", session_key=HOME_SESSION, run_id="typed-1")
        offered = _state(agent)["pendingAsk"]
        await loop.process_direct(
            "Save and start", session_key=HOME_SESSION, run_id="save-1",
            extra_metadata={"setup_answer": {"askId": offered["id"], "optionId": "save"}},
        )
    finally:
        loop.stop()
    visible = [
        (message["role"], message.get("kind"), message["content"])
        for message in SessionManager(agent / "workspace").get_full_messages(HOME_SESSION)
        if message["role"] in ("user", "assistant") and not message.get("tool_calls") and not message.get("_display_hidden")
    ]
    assert [row[:2] for row in visible] == [
        ("user", None), ("assistant", None), ("assistant", "agent_setup_card"), ("user", None), ("assistant", None),
    ]
    state = _state(agent)
    assert state["setup"] == "complete" and state["savedCardId"] == offered["id"]
    assert "## Working style" in (agent / "workspace" / "SOUL.md").read_text()

