"""A tool-free turn is a backend policy, not a prompt suggestion."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.tool_policy import (
    resolve_exclusive_tool_scope,
    resolve_turn_tool_policy,
)
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


@pytest.mark.parametrize(
    "content",
    [
        (
            "Context7 MCP sunucusundaki bir aracı tam bir kez çağırarak "
            "React Query kimliğini çöz. Başka hiçbir araç kullanma."
        ),
        "Run tool Context7 and do not use any other tools.",
        "Invoke the Context7 MCP server without calling any other tools.",
        "Use the Context7 tool; use no other tools.",
    ],
)
def test_scoped_exclusion_keeps_requested_tool_execution_enabled(content):
    decision = resolve_turn_tool_policy({}, content)
    assert decision.allowed is True
    assert decision.source == "content_scoped_exclusion"
    assert AgentLoop._is_action_turn(object.__new__(AgentLoop), "cli", content) is True


@pytest.mark.parametrize(
    "content",
    [
        "Use the Context7 tool, but do not use any tools.",
        "Context7 MCP aracını çağır fakat hiçbir araç kullanma.",
        "Başka hiçbir araç kullanma.",
        "Do not use any other tools.",
        "Use no other tools.",
    ],
)
def test_global_or_ambiguous_deny_remains_fail_closed(content):
    decision = resolve_turn_tool_policy({}, content)
    assert decision.allowed is False
    assert decision.source == "content_global_deny"


def test_structured_deny_wins_over_scoped_plain_language():
    content = "Run tool Context7 and do not use any other tools."
    decision = resolve_turn_tool_policy(
        {"tools_allowed": True, "tool_policy": "none"},
        content,
    )
    assert decision.allowed is False
    assert decision.source == "structured_deny"


def test_structured_allow_is_not_reinterpreted_from_plain_language():
    decision = resolve_turn_tool_policy(
        {"tools_allowed": True},
        "Answer without using tools.",
    )
    assert decision.allowed is True
    assert decision.source == "structured_allow"


def test_exclusive_scope_resolves_an_mcp_server_family():
    tools = [
        ("mcp_context7_resolve_library_id", "context7"),
        ("mcp_context7_query_docs", "context7"),
        ("web_search", "builtin"),
    ]

    scope = resolve_exclusive_tool_scope(
        (
            "Context7 MCP sunucusundaki bir aracı tam bir kez çağırarak "
            "React Query kimliğini çöz. Başka hiçbir araç kullanma."
        ),
        tools,
    )

    assert scope == frozenset({
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
    })


def test_exclusive_scope_prefers_an_exact_remote_tool_name():
    tools = [
        ("mcp_context7_resolve_library_id", "context7"),
        ("mcp_context7_query_docs", "context7"),
    ]

    scope = resolve_exclusive_tool_scope(
        "Use resolve-library-id and do not use any other tools.",
        tools,
    )

    assert scope == frozenset({"mcp_context7_resolve_library_id"})


def test_named_tool_with_scoped_exclusion_is_an_enforced_action_turn():
    content = "Use resolve-library-id and do not use any other tools."

    decision = resolve_turn_tool_policy({}, content)

    assert decision.allowed is True
    assert decision.exclusive is True
    assert AgentLoop._is_action_turn(object.__new__(AgentLoop), "cli", content) is True


def test_exclusive_scope_fails_closed_for_an_unknown_target():
    scope = resolve_exclusive_tool_scope(
        "Run MissingServer MCP and do not use any other tools.",
        [("mcp_context7_query_docs", "context7")],
    )

    assert scope == frozenset()


@pytest.mark.parametrize(
    "content",
    [
        "Explain how tool calling works.",
        "Bu aracın kullanımını anlat.",
        "MCP araçlarının nasıl çalıştığını açıkla.",
    ],
)
def test_tool_discussion_does_not_force_an_action_turn(content):
    assert AgentLoop._is_action_turn(object.__new__(AgentLoop), "cli", content) is False


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


class _ScopedTool(Tool):
    def __init__(self, name: str, source: str) -> None:
        self._name = name
        self._source = source
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Test capability from {self._source}."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def discovery_source(self) -> str:
        return self._source

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "SCOPED_EXECUTED"


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


class _ScopedProvider(LLMProvider):
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
                        id="out-of-scope-1",
                        name="mcp_other_dangerous_action",
                        arguments={},
                    )
                ],
            )
        if len(self.calls) == 2:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="scoped-2",
                        name="mcp_context7_resolve_library_id",
                        arguments={},
                    )
                ],
            )
        return LLMResponse(content="MCP_LIVE_OK")


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
async def test_scoped_exclusion_filters_schemas_and_executor_end_to_end(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    provider = _ScopedProvider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=3,
        soft_warn_at_iteration=0,
    )
    resolve_tool = _ScopedTool(
        "mcp_context7_resolve_library_id",
        "context7",
    )
    query_tool = _ScopedTool("mcp_context7_query_docs", "context7")
    unrelated_tool = _ScopedTool("mcp_other_dangerous_action", "other")
    loop.tools.register(resolve_tool, toolset="mcp")
    loop.tools.register(query_tool, toolset="mcp")
    loop.tools.register(unrelated_tool, toolset="mcp")
    session = loop.sessions.get_or_create("web:exclusive")
    session.metadata["title"] = "Existing title"
    loop.sessions.save(session)

    try:
        result = await loop.process_direct(
            (
                "Context7 MCP sunucusundaki bir aracı tam bir kez çağırarak "
                "React Query kimliğini çöz. Başka hiçbir araç kullanma."
            ),
            session_key="web:exclusive",
        )
    finally:
        loop.stop()

    first_schema_names = {
        tool["function"]["name"]
        for tool in provider.calls[0]["tools"]
    }
    assert result == "MCP_LIVE_OK"
    assert first_schema_names == {
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
    }
    assert provider.calls[0]["tool_choice"] == "auto"
    assert resolve_tool.calls == [{}]
    assert query_tool.calls == []
    assert unrelated_tool.calls == []


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
