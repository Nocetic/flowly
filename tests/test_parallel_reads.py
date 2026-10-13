"""Independent reads in one response run together; everything else in order.

The provider now lets the model ask for several tool calls at once, but the
loop still ran them one after another, so three page fetches cost three
round trips of waiting. Pure reads in the same response now run at the same
time; results still enter the conversation in call order, every gate still
applies, and tools that write, send or ask keep running one at a time.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_DELAY = 0.3


class _SlowTool(Tool):
    def __init__(self, name: str, log: list[tuple[str, str, float]]) -> None:
        self._name = name
        self.log = log

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"slow {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"q": {"type": "string"}}}

    async def execute(self, q: str = "", **kwargs: Any) -> str:
        self.log.append(("start", f"{self._name}:{q}", time.monotonic()))
        await asyncio.sleep(_DELAY)
        self.log.append(("end", f"{self._name}:{q}", time.monotonic()))
        return f"{self._name} result for {q}"


class _Provider(LLMProvider):
    def __init__(self, calls: list[ToolCallRequest]) -> None:
        super().__init__(api_key="unused")
        self.calls = calls
        self.turn = 0
        self.tool_messages: list[str] = []

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.turn += 1
        if self.turn == 1:
            return LLMResponse(content=None, tool_calls=self.calls)
        self.tool_messages = [m["content"] for m in kwargs["messages"] if m.get("role") == "tool"]
        return LLMResponse(content="done")


def _loop(tmp_path, monkeypatch, provider, log):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     main_config=Config(), max_iterations=3, soft_warn_at_iteration=0)
    for name in ("web_fetch", "web_search", "write_note"):
        loop.tools.register(_SlowTool(name, log), toolset="test")
    return loop


async def _run(loop):
    return await loop._run_llm_tool_loop(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "go"}],
        turn_content="go", session_key="web:p", tool_platform="web",
    )


@pytest.mark.asyncio
async def test_reads_in_one_response_overlap_and_keep_their_order(tmp_path, monkeypatch):
    log: list = []
    provider = _Provider([
        ToolCallRequest(id="a", name="web_fetch", arguments={"q": "1"}),
        ToolCallRequest(id="b", name="web_search", arguments={"q": "2"}),
        ToolCallRequest(id="c", name="web_fetch", arguments={"q": "3"}),
    ])
    loop = _loop(tmp_path, monkeypatch, provider, log)
    started = time.monotonic()
    try:
        await _run(loop)
    finally:
        loop.stop()
    elapsed = time.monotonic() - started
    assert elapsed < 3 * _DELAY  # not one after another
    order = ["web_fetch result for 1", "web_search result for 2", "web_fetch result for 3"]
    assert len(provider.tool_messages) == 3
    for message, expected in zip(provider.tool_messages, order):
        assert expected in message


@pytest.mark.asyncio
async def test_a_tool_that_writes_still_runs_alone_and_in_order(tmp_path, monkeypatch):
    log: list = []
    provider = _Provider([
        ToolCallRequest(id="a", name="write_note", arguments={"q": "1"}),
        ToolCallRequest(id="b", name="write_note", arguments={"q": "2"}),
    ])
    loop = _loop(tmp_path, monkeypatch, provider, log)
    try:
        await _run(loop)
    finally:
        loop.stop()
    events = [(kind, name) for kind, name, _ in log]
    assert events == [
        ("start", "write_note:1"), ("end", "write_note:1"),
        ("start", "write_note:2"), ("end", "write_note:2"),
    ]
