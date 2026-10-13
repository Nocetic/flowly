"""What a subagent saves is linked to its run and lands in the owner's Library.

Two faults hid a child's saved work: its run never recorded the artifact
(the tool answers ``{"artifact": {"id": …}}`` and only a top-level ``id`` was
read), and in a named profile the child wrote to the profile's private store
while the main agent writes to the shared Library the owner looks at.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from flowly.agent.subagent import SubagentManager, _created_artifact_id
from flowly.agent.subagent_registry import SubagentRegistry
from flowly.agent.tools.base import Tool
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _Provider(LLMProvider):
    def __init__(self, calls: list[ToolCallRequest]) -> None:
        super().__init__(api_key="unused")
        self.pending = list(calls)

    def get_default_model(self) -> str:
        return "fake/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        if self.pending:
            return LLMResponse(content=None, tool_calls=[self.pending.pop(0)])
        return LLMResponse(content="Saved the report.")


class _LibraryTool(Tool):
    """Stands in for the shared Library adapter a named profile uses."""

    def __init__(self, store: list[dict]) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "artifact"

    @property
    def description(self) -> str:
        return "Library"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"action": {"type": "string"}}}

    async def execute(self, action: str = "", **kwargs: Any) -> str:
        if action == "list":
            return json.dumps({"action": "list", "artifacts": [{"id": "old-1"}]})
        self.store.append(kwargs)
        return json.dumps({"action": "create", "artifact": {"id": "lib-1", "title": kwargs.get("title")}})


def test_only_a_successful_create_counts_as_saved():
    created = {"action": "create", "artifact": {"id": "a1"}}
    assert _created_artifact_id(created, {"action": "create"}) == "a1"
    assert _created_artifact_id({"action": "list", "artifacts": [{"id": "a1"}]}, {"action": "list"}) is None
    assert _created_artifact_id({"ok": False, "error": "quota"}, {"action": "create"}) is None


@pytest.mark.asyncio
async def test_a_child_saves_into_the_library_its_parent_uses_and_links_it(tmp_path):
    library: list[dict] = []
    manager = SubagentManager(
        provider=_Provider([
            ToolCallRequest(id="l1", name="artifact", arguments={"action": "list"}),
            ToolCallRequest(id="c1", name="artifact", arguments={
                "action": "create", "title": "Rapor", "content": "…", "type": "markdown",
            }),
        ]),
        workspace=tmp_path, bus=MessageBus(),
        registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path,
    )
    manager.artifact_tool_factory = lambda: _LibraryTool(library)
    await manager.spawn("Write the report", wait=True, origin="agent")

    record = manager.registry.all()[0]
    assert record.artifact_ids == ["lib-1"]  # listing linked nothing
    assert library and library[0]["title"] == "Rapor"
    assert not (tmp_path / "artifacts.sqlite").exists()
