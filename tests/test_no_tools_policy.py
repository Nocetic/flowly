"""A tool-free turn is a backend policy, not a prompt suggestion."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.bus.events import InboundMessage, OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.mark.parametrize(
    ("metadata", "content"),
    [
        ({"tools_allowed": False}, "inspect this"),
        ({"tool_policy": "none"}, "inspect this"),
        ({}, "Do not use any tools; answer from memory."),
        ({}, "Explain it without using tools."),
        ({}, "No tools please."),
        ({}, "Hiçbir tool kullanmadan cevapla."),
        ({}, "Araç çağırma, sadece bildiklerinle yanıtla."),
    ],
)
def test_explicit_no_tools_signals_resolve_to_denied(metadata, content):
    assert AgentLoop._turn_tools_allowed(metadata, content) is False


@pytest.mark.parametrize(
    "content",
    [
        "Explain how tool calling works.",
        "Bu aracın kullanımını anlat.",
        "Tools are useful for this task.",
        "Research the no-tools design pattern.",
    ],
)
def test_unrelated_tool_words_do_not_disable_execution(content):
    assert AgentLoop._turn_tools_allowed({}, content) is True


class _TripwireTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "tripwire"

    @property
    def description(self) -> str:
        return "A tool that must never execute in this test."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "EXECUTED"


class _HallucinatingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="forbidden-1",
                        name="tripwire",
                        arguments={},
                    )
                ],
            )
        assert "backend-enforced no-tools policy" in str(
            kwargs["messages"][-1]["content"]
        )
        return LLMResponse(content="Answered without tools.")


class _PlainProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return LLMResponse(content="Local answer.")


@pytest.mark.asyncio
async def test_executor_blocks_hallucinated_call_even_with_no_schemas(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    provider = _HallucinatingProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=3,
        soft_warn_at_iteration=0,
    )
    tripwire = _TripwireTool()
    loop.tools.register(tripwire, toolset="test")
    try:
        final, results, executed, _usage, messages = (
            await loop._run_llm_tool_loop(
                messages=[
                    {"role": "system", "content": "test"},
                    {"role": "user", "content": "Answer without tools"},
                ],
                action_turn=True,
                turn_content="Answer without tools",
                session_key="web:no-tools",
                tool_platform="web",
                tools_allowed=False,
            )
        )
    finally:
        loop.stop()

    assert final == "Answered without tools."
    assert tripwire.calls == []
    assert executed == []
    assert results == [
        {
            "tool": "tripwire",
            "success": False,
            "policy": "no_tools",
            "result": (
                "BLOCKED: Tool 'tripwire' cannot run because this turn has a "
                "backend-enforced no-tools policy. Answer from the supplied "
                "context without tools."
            ),
        }
    ]
    assert all(call.get("tools") == [] for call in provider.calls)
    assert all(call.get("tool_choice") == "none" for call in provider.calls)
    assert any(
        message.get("role") == "tool"
        and "backend-enforced no-tools policy" in str(message.get("content"))
        for message in messages
    )


@pytest.mark.asyncio
async def test_process_direct_carries_structured_policy_without_api_breakage():
    loop = object.__new__(AgentLoop)
    captured: list[InboundMessage] = []

    async def process(message: InboundMessage) -> OutboundMessage:
        captured.append(message)
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="ok",
        )

    loop._process_message = AsyncMock(side_effect=process)

    result = await loop.process_direct(
        "answer locally",
        session_key="web:direct",
        tools_allowed=False,
    )

    assert result == "ok"
    assert captured[0].metadata["tools_allowed"] is False
    assert captured[0].metadata["tool_policy"] == "none"


@pytest.mark.asyncio
async def test_notools_slash_works_end_to_end_without_client_changes(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    provider = _PlainProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=2,
        soft_warn_at_iteration=0,
    )
    session = loop.sessions.get_or_create("web:slash")
    session.metadata["title"] = "Existing title"
    loop.sessions.save(session)
    try:
        text, metadata = await loop.process_direct(
            "/notools Explain the architecture",
            session_key="web:slash",
            return_metadata=True,
        )
        await loop.process_direct(
            "Explain it again with the available tools",
            session_key="web:slash",
        )
    finally:
        loop.stop()

    assert text == "Local answer."
    assert metadata["toolPolicy"] == "none"
    assert provider.calls[0]["tools"] == []
    assert provider.calls[0]["tool_choice"] == "none"
    assert "<tool_execution_policy" in provider.calls[0]["messages"][-1]["content"]
    assert provider.calls[1]["tools"]
    assert provider.calls[1]["tool_choice"] == "auto"
    assert "<tool_execution_policy" not in provider.calls[1]["messages"][-1]["content"]
    saved = loop.sessions.get_or_create("web:slash")
    assert [
        message["content"]
        for message in saved.messages
        if message.get("role") == "user"
    ][-2:] == [
        "/notools Explain the architecture",
        "Explain it again with the available tools",
    ]


def test_command_registry_advertises_transport_compatible_name():
    from flowly.agent.slash_commands import gateway_commands, resolve_command

    names = {command.name for command in gateway_commands()}
    assert "notools" in names
    assert resolve_command("no-tools").name == "notools"  # type: ignore[union-attr]
