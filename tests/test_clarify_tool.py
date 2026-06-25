"""ClarifyTool builds a ClarifyRequest, awaits the manager, and returns the
user's answer (or a timeout note) as JSON to the agent."""

from __future__ import annotations

import json

import pytest

from flowly.agent.tools.clarify import ClarifyTool
from flowly.clarify.types import MAX_CHOICES


def test_schema_shape():
    tool = ClarifyTool()
    assert tool.name == "clarify"
    schema = tool.parameters
    assert schema["required"] == ["question"]
    assert schema["properties"]["choices"]["maxItems"] == MAX_CHOICES


@pytest.mark.asyncio
async def test_missing_question_errors():
    tool = ClarifyTool()
    out = await tool.execute(question="   ")
    assert out.startswith("Error")


@pytest.mark.asyncio
async def test_execute_returns_user_answer(monkeypatch):
    import flowly.agent.tools.clarify as mod

    captured = {}

    class _FakeMgr:
        async def request_and_wait(self, pending):
            captured["pending"] = pending
            return "the red one"

    monkeypatch.setattr(mod, "get_clarify_manager", lambda: _FakeMgr())

    tool = ClarifyTool()
    out = await tool.execute(
        question="Which color?",
        choices=["red", "blue"],
        session_key="web:7",
    )
    data = json.loads(out)
    assert data["user_response"] == "the red one"
    assert data["choices_offered"] == ["red", "blue"]
    assert captured["pending"].session_key == "web:7"
    assert captured["pending"].question == "Which color?"


@pytest.mark.asyncio
async def test_choices_clamped_to_max(monkeypatch):
    import flowly.agent.tools.clarify as mod

    captured = {}

    class _FakeMgr:
        async def request_and_wait(self, pending):
            captured["pending"] = pending
            return "x"

    monkeypatch.setattr(mod, "get_clarify_manager", lambda: _FakeMgr())

    tool = ClarifyTool()
    too_many = [f"opt{i}" for i in range(MAX_CHOICES + 3)]
    await tool.execute(question="Pick", choices=too_many)
    assert len(captured["pending"].choices) == MAX_CHOICES


@pytest.mark.asyncio
async def test_empty_choices_become_open_ended(monkeypatch):
    import flowly.agent.tools.clarify as mod

    captured = {}

    class _FakeMgr:
        async def request_and_wait(self, pending):
            captured["pending"] = pending
            return "free text"

    monkeypatch.setattr(mod, "get_clarify_manager", lambda: _FakeMgr())

    tool = ClarifyTool()
    out = await tool.execute(question="Anything?", choices=["", "  "])
    assert captured["pending"].choices is None
    assert json.loads(out)["choices_offered"] is None


@pytest.mark.asyncio
async def test_timeout_returns_note(monkeypatch):
    import flowly.agent.tools.clarify as mod

    class _FakeMgr:
        async def request_and_wait(self, pending):
            return None

    monkeypatch.setattr(mod, "get_clarify_manager", lambda: _FakeMgr())

    tool = ClarifyTool()
    out = await tool.execute(question="Which?")
    data = json.loads(out)
    assert data["user_response"] is None
    assert "note" in data
