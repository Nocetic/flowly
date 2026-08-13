"""Platform/toolset routing and runtime availability tests."""

import json
from typing import Any

import pytest

from flowly.agent.context import ContextBuilder
from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.agent.tools.discovery import (
    annotate_search_repetition,
    build_tool_disclosure,
    estimate_schema_transmission_tokens,
)
from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.routing import infer_toolset, resolve_toolset_filters
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class RoutedTool(Tool):
    def __init__(self, name: str, *, available: bool = True):
        self._name = name
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def is_available(self) -> bool:
        return self._available

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class VerboseRoutedTool(RoutedTool):
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed deferred input. " * 500,
                },
            },
        }


def _definition_names(definitions: list[dict[str, Any]]) -> set[str]:
    return {item["function"]["name"] for item in definitions}


def test_builtin_and_extension_toolset_inference() -> None:
    assert infer_toolset("read_file") == "filesystem"
    assert infer_toolset("cron") == "scheduling"
    assert infer_toolset("mcp_context7_query") == "mcp"
    assert infer_toolset("third_party_widget") == "extensions"


def test_cron_defaults_hide_recursive_and_interactive_toolsets() -> None:
    enabled, disabled = resolve_toolset_filters("cron")
    assert enabled is None
    assert {"delivery", "interactive", "scheduling"} <= disabled


def test_explicit_platform_toolsets_are_an_allowlist() -> None:
    enabled, disabled = resolve_toolset_filters(
        "Telegram",
        platform_toolsets={"TELEGRAM": ["WEB", "memory"]},
        disabled_toolsets=["MEMORY"],
    )
    assert enabled == frozenset({"web", "memory"})
    assert disabled == frozenset({"memory"})


def test_wildcard_platform_toolsets_apply_to_every_job_surface() -> None:
    web_enabled, _ = resolve_toolset_filters(
        "web",
        platform_toolsets={"*": ["web", "mcp"], "cron": ["memory"]},
    )
    cron_enabled, cron_disabled = resolve_toolset_filters(
        "cron",
        platform_toolsets={"*": ["web", "mcp"], "cron": ["memory"]},
    )

    assert web_enabled == frozenset({"web", "mcp"})
    assert cron_enabled == frozenset({"memory"})
    assert {"delivery", "interactive", "scheduling"} <= cron_disabled


def test_registry_filters_by_toolset_and_platform() -> None:
    registry = ToolRegistry()
    registry.register(RoutedTool("read_file"))
    registry.register(
        RoutedTool("message"),
        platforms={"telegram", "web"},
    )

    telegram = registry.get_definitions(
        platform="telegram",
        enabled_toolsets=frozenset({"filesystem"}),
    )
    assert _definition_names(telegram) == {"read_file"}

    cli = registry.get_definitions(platform="cli")
    assert _definition_names(cli) == {"read_file"}


def test_registry_hides_unavailable_tool_and_blocks_execution() -> None:
    registry = ToolRegistry()
    registry.register(RoutedTool("docker", available=False))

    assert registry.get_definitions(platform="cli") == []


@pytest.mark.asyncio
async def test_registry_blocks_execution_of_unavailable_tool() -> None:
    registry = ToolRegistry()
    registry.register(RoutedTool("docker", available=False))

    result = await registry.execute("docker", {}, platform="cli")
    assert result == "Error: Tool 'docker' is unavailable for platform 'cli'"


def test_check_fn_is_cached_and_can_be_invalidated() -> None:
    calls: list[str] = []
    state = {"available": True}
    registry = ToolRegistry(availability_cache_ttl=60)
    registry.register(
        RoutedTool("custom"),
        check_fn=lambda context: calls.append(context.platform) or state["available"],
    )

    assert registry.get_available_names(platform="web") == ["custom"]
    state["available"] = False
    assert registry.get_available_names(platform="web") == ["custom"]
    assert calls == ["web"]

    registry.invalidate_availability("custom")
    assert registry.get_available_names(platform="web") == []
    assert calls == ["web", "web"]


def test_agent_loop_uses_configured_platform_toolsets() -> None:
    config = Config.model_validate({
        "tools": {
            "routing": {
                "platform_toolsets": {"telegram": ["web"]},
            },
        },
    })
    loop = AgentLoop.__new__(AgentLoop)
    loop._main_config = config
    loop.tools = ToolRegistry()
    loop.tools.register(RoutedTool("web_search"))
    loop.tools.register(RoutedTool("read_file"))

    definitions = loop._routed_tool_definitions("telegram")
    assert _definition_names(definitions) == {"web_search"}


def test_configured_builtin_generators_and_board_stay_direct(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    config = Config.model_validate({
        "tools": {
            "media_generation": {
                "enabled": True,
                "api_key": "media-test",
                "defaults": {"text_to_video": "vendor/video"},
            },
        },
        "integrations": {
            "elevenlabs": {
                "enabled": True,
                "api_key": "voice-test",
                "voice_id": "voice-1",
                "model_id": "voice-model",
            },
        },
    })

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            raise AssertionError("chat is not used by this routing test")

    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(),
        workspace=tmp_path,
        main_config=config,
    )
    disclosure = loop._routed_tool_disclosure("web")
    expected = {
        "image_generate",
        "video_generate",
        "voice_generate",
        "board_list",
    }

    assert expected <= disclosure.direct_names
    assert not (expected & set(disclosure.deferred))
    assert not disclosure.enabled


def test_discovery_never_resurrects_tool_outside_platform_route() -> None:
    config = Config.model_validate({
        "tools": {
            "routing": {
                "platform_toolsets": {"web": ["filesystem"]},
            },
        },
    })
    loop = AgentLoop.__new__(AgentLoop)
    loop._main_config = config
    loop.tools = ToolRegistry()
    loop.tools.register(RoutedTool("read_file"))
    loop.tools.register(VerboseRoutedTool("image_generate"))

    disclosure = loop._routed_tool_disclosure("web")

    assert disclosure.all_names == {"read_file"}
    assert "image_generate" not in disclosure.deferred


def test_system_prompt_omits_guidance_for_tools_outside_route(tmp_path) -> None:
    builder = ContextBuilder(workspace=tmp_path)

    prompt = builder.build_system_prompt(
        skip_memory=True,
        skip_context_files=True,
        channel="telegram",
        available_tools={"web_search"},
    )

    assert "## exec Tool" not in prompt
    assert "## Filesystem Access" not in prompt
    assert "## Cron" not in prompt
    assert "## Background Tasks" not in prompt
    assert "# Specialist Agents" not in prompt
    assert "# Artifacts" not in prompt
    assert "## Knowledge Graph" not in prompt


def test_progressive_disclosure_keeps_deferred_tools_reachable() -> None:
    registry = ToolRegistry()
    registry.register(RoutedTool("read_file"))
    registry.register(VerboseRoutedTool("image_generate"))
    definitions = registry.get_definitions(platform="web")

    disclosure = build_tool_disclosure(
        definitions,
        enabled=True,
        deferred_toolsets={"media"},
        always_visible_tools={"read_file"},
        minimum_deferred_schema_tokens=0,
    )

    assert "read_file" in disclosure.direct_names
    assert "image_generate" in disclosure.deferred
    assert "image_generate" in disclosure.all_names
    visible_names = _definition_names(list(disclosure.definitions))
    assert {"tool_search", "tool_call"} <= visible_names
    assert "tool_describe" not in visible_names
    assert "image_generate" not in _definition_names(list(disclosure.definitions))

    search_result = disclosure.search("image")
    assert "image_generate" in search_result
    assert '"input_schema"' in search_result
    assert '"name":"image_generate"' in disclosure.describe("image_generate")
    assert disclosure.resolve_call(
        "image_generate", {"prompt": "a cat"}
    ) == ("image_generate", {"prompt": "a cat"})
    assert disclosure.resolve_call(
        "image_generate", '{"prompt":"a dog"}'
    ) == ("image_generate", {"prompt": "a dog"})


def test_default_discovery_keeps_builtin_tools_direct() -> None:
    registry = ToolRegistry()
    for name in (
        "image_generate",
        "video_generate",
        "voice_generate",
        "board_list",
        "exec",
    ):
        registry.register(VerboseRoutedTool(name))

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
    )

    assert disclosure.deferred == {}
    assert disclosure.direct_names == {
        "image_generate",
        "video_generate",
        "voice_generate",
        "board_list",
        "exec",
    }
    bridge_names = {"tool_search", "tool_call"}
    assert not (bridge_names & _definition_names(list(disclosure.definitions)))


def test_intent_routing_promotes_matching_external_schemas_only() -> None:
    registry = ToolRegistry()
    for name in (
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
        "mcp_context7_unrelated_admin_action",
        "mcp_typefully_list_drafts",
        "mcp_typefully_publish_draft",
        "mcp_typefully_typefully_comments_resolve_thread",
        "mcp_typefully_typefully_linkedin_resolve_linkedin_organization_from_url",
    ):
        registry.register(VerboseRoutedTool(name), toolset="mcp")

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        intent_text="Use Context7 to resolve React and query the documentation",
    )
    visible_names = _definition_names(list(disclosure.definitions))

    assert disclosure.promoted_names == {
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
    }
    assert disclosure.promoted_names <= disclosure.direct_names
    assert "mcp_typefully_list_drafts" in disclosure.deferred
    assert "mcp_context7_unrelated_admin_action" in disclosure.deferred
    assert "mcp_typefully_typefully_comments_resolve_thread" in disclosure.deferred
    assert (
        "mcp_typefully_typefully_linkedin_resolve_linkedin_organization_from_url"
        in disclosure.deferred
    )
    assert disclosure.promoted_names <= visible_names
    assert {"tool_search", "tool_call"} <= visible_names


def test_search_finds_promoted_and_deferred_tools_in_one_external_catalog() -> None:
    registry = ToolRegistry()
    registry.register(RoutedTool("read_file"))
    for name in (
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
        "mcp_context7_unrelated_admin_action",
        "mcp_typefully_typefully_comments_resolve_thread",
    ):
        registry.register(VerboseRoutedTool(name), toolset="mcp")

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        intent_text="Use Context7 to query React documentation",
    )
    assert "mcp_context7_query_docs" in disclosure.promoted_names

    payload = json.loads(disclosure.search(
        "Context7 resolve library ID and query documentation tools",
        toolset="mcp",
        limit=10,
    ))
    matches = {match["name"]: match for match in payload["matches"]}

    assert {
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
    } <= matches.keys()
    assert "mcp_typefully_typefully_comments_resolve_thread" not in matches
    assert matches["mcp_context7_query_docs"]["visibility"] == "direct"
    assert (
        matches["mcp_context7_query_docs"]["invoke_via"]
        == ["mcp_context7_query_docs", "tool_call"]
    )
    assert matches["mcp_context7_resolve_library_id"]["visibility"] == "deferred"
    assert matches["mcp_context7_resolve_library_id"]["invoke_via"] == ["tool_call"]
    assert payload["scope"] == "route_enabled_external_tools"
    assert disclosure.resolve_call(
        "mcp_context7_query_docs", {"prompt": "React hooks"}
    ) == ("mcp_context7_query_docs", {"prompt": "React hooks"})
    assert isinstance(disclosure.resolve_call("read_file", {}), str)


def test_search_can_page_through_every_route_enabled_external_tool() -> None:
    registry = ToolRegistry()
    names = {f"mcp_catalog_service_action_{index}" for index in range(7)}
    for name in names:
        registry.register(VerboseRoutedTool(name), toolset="mcp")
    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
    )

    found: set[str] = set()
    offset = 0
    while True:
        payload = json.loads(disclosure.search("", limit=3, offset=offset))
        found.update(match["name"] for match in payload["matches"])
        next_offset = payload["next_offset"]
        if next_offset is None:
            break
        offset = next_offset

    assert found == names
    assert payload["total_candidates"] == len(names)


def test_repeated_search_results_are_reported_without_blocking() -> None:
    registry = ToolRegistry()
    registry.register(VerboseRoutedTool("mcp_context7_query_docs"), toolset="mcp")
    registry.register(VerboseRoutedTool("mcp_context7_resolve_library_id"), toolset="mcp")
    registry.register(VerboseRoutedTool("mcp_other_action"), toolset="mcp")
    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
    )
    seen: dict[tuple[str, ...], int] = {}

    first = json.loads(annotate_search_repetition(
        disclosure.search("Context7 documentation", toolset="mcp"),
        seen,
    ))
    second = json.loads(annotate_search_repetition(
        disclosure.search("Context7 docs", toolset="mcp"),
        seen,
    ))

    assert first["repeated_result_set"] is False
    assert first["previous_occurrences"] == 0
    assert second["repeated_result_set"] is True
    assert second["previous_occurrences"] == 1
    assert "repeat_context" in second


def test_intent_promotion_reduces_complete_turn_schema_transmission() -> None:
    registry = ToolRegistry()
    for index in range(12):
        name = (
            "mcp_context7_query_docs"
            if index == 0
            else f"mcp_unrelated_service_action_{index}"
        )
        registry.register(VerboseRoutedTool(name), toolset="mcp")
    eager = registry.get_definitions()
    disclosure = build_tool_disclosure(
        eager,
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        intent_text="Query Context7 docs for React",
    )

    # The promoted tool can be called on the first LLM round, so both paths
    # need the same two rounds (tool + final). Compare the whole turn rather
    # than claiming victory from a one-request schema snapshot.
    eager_turn = estimate_schema_transmission_tokens(eager, llm_rounds=2)
    routed_turn = estimate_schema_transmission_tokens(
        disclosure.definitions,
        llm_rounds=2,
    )
    assert "mcp_context7_query_docs" in disclosure.promoted_names
    assert routed_turn < eager_turn / 3


def test_registry_source_metadata_overrides_name_inference() -> None:
    registry = ToolRegistry()
    registry.register(VerboseRoutedTool("image_generate"), toolset="extensions")

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
    )

    assert disclosure.toolsets["image_generate"] == "extensions"
    assert "image_generate" in disclosure.deferred


def test_discovery_bridge_cannot_call_direct_or_absent_tool() -> None:
    registry = ToolRegistry()
    registry.register(RoutedTool("read_file"))
    registry.register(VerboseRoutedTool("image_generate"))
    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        enabled=True,
        deferred_toolsets={"media"},
        always_visible_tools={"read_file"},
        minimum_deferred_schema_tokens=0,
    )

    assert isinstance(disclosure.resolve_call("read_file", {}), str)
    assert isinstance(disclosure.resolve_call("missing_tool", {}), str)
    assert isinstance(disclosure.resolve_call("image_generate", "bad"), str)


def test_progressive_disclosure_reduces_large_schema_payload() -> None:
    large_parameters = {
        "type": "object",
        "properties": {
            f"field_{index}": {
                "type": "string",
                "description": "A deliberately verbose deferred input field. " * 8,
            }
            for index in range(20)
        },
    }

    class LargeMediaTool(RoutedTool):
        @property
        def parameters(self) -> dict[str, Any]:
            return large_parameters

    registry = ToolRegistry()
    registry.register(RoutedTool("read_file"))
    registry.register(LargeMediaTool("image_generate"))
    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        deferred_toolsets={"media"},
        always_visible_tools={"read_file"},
        minimum_deferred_schema_tokens=0,
    )

    assert disclosure.disclosed_schema_tokens < disclosure.original_schema_tokens / 2


def test_availability_last_good_grace_absorbs_transient_probe_failure() -> None:
    state = {"fails": False}

    def _check(_context) -> bool:
        if state["fails"]:
            raise RuntimeError("temporary probe failure")
        return True

    registry = ToolRegistry(
        availability_cache_ttl=0,
        availability_failure_grace=60,
    )
    registry.register(
        RoutedTool("custom"),
        check_fn=_check,
    )

    assert registry.get_available_names(platform="web") == ["custom"]
    state["fails"] = True
    assert registry.get_available_names(platform="web") == ["custom"]

    registry.set_availability_failure_grace(0)
    assert registry.get_available_names(platform="web") == []


def test_schema_cache_reuses_schema_and_registration_invalidates_it() -> None:
    calls = {"count": 0}

    class CountingTool(RoutedTool):
        def to_schema(self) -> dict[str, Any]:
            calls["count"] += 1
            return super().to_schema()

    registry = ToolRegistry(availability_cache_ttl=60, schema_cache_ttl=60)
    registry.register(CountingTool("custom"))

    registry.get_definitions(platform="web")
    registry.get_definitions(platform="web")
    assert calls["count"] == 1

    registry.register(RoutedTool("another_custom"))
    registry.get_definitions(platform="web")
    assert calls["count"] == 2


def test_prompt_uses_reachable_tools_for_skill_dependency_filtering(
    tmp_path,
    monkeypatch,
) -> None:
    builder = ContextBuilder(workspace=tmp_path)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(builder.skills, "get_always_skills", lambda: [])

    def _summary(*, available_tools):
        captured["tools"] = available_tools
        return ""

    monkeypatch.setattr(builder.skills, "build_skills_summary", _summary)
    builder.build_system_prompt(
        skip_memory=True,
        skip_context_files=True,
        available_tools={"skill_view", "tool_call"},
        reachable_tools={"skill_view", "image_generate"},
    )

    assert captured["tools"] == {"skill_view", "image_generate"}


@pytest.mark.asyncio
async def test_main_loop_executes_deferred_mcp_tool_through_bridge(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    calls: list[dict[str, Any]] = []

    class RemoteTool(RoutedTool):
        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed generation prompt. " * 500,
                    },
                },
                "required": ["prompt"],
            }

        async def execute(self, **kwargs: Any) -> str:
            calls.append(kwargs)
            return "image created"

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.tool_surfaces: list[set[str]] = []
            self.index = 0

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            definitions = kwargs.get("tools") or []
            self.tool_surfaces.append(_definition_names(definitions))
            self.index += 1
            if self.index == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="call-1",
                        name="tool_call",
                        arguments={
                            "name": "mcp_canvas_render",
                            "arguments": {"prompt": "a cat"},
                        },
                    )],
                )
            return LLMResponse(content="done")

    provider = Provider()
    remote_name = "mcp_canvas_render"
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=3,
        soft_warn_at_iteration=0,
    )
    loop.tools.register(RemoteTool(remote_name), toolset="mcp")

    final, results, executed, _usage, messages = await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "generate an image"},
        ],
        action_turn=False,
        turn_content="generate an image",
        session_key="web:test",
        tool_platform="web",
    )

    assert final == "done"
    assert calls == [{"prompt": "a cat"}]
    assert executed == [remote_name]
    assert results[0]["tool"] == remote_name
    assert remote_name not in provider.tool_surfaces[0]
    assert {"tool_search", "tool_call"} <= provider.tool_surfaces[0]
    assert "tool_describe" not in provider.tool_surfaces[0]
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    assert tool_messages[0]["name"] == "tool_call"


@pytest.mark.asyncio
async def test_turn_journal_survives_working_context_shrink(
    tmp_path,
    monkeypatch,
) -> None:
    """Overflow recovery may make the final list shorter than its input.

    Durable assistant/tool events must come from the append-only turn journal,
    never from a positional slice of that mutable working list.
    """
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.index = 0

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.index += 1
            if self.index == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="search-1",
                        name="tool_search",
                        arguments={"query": "context integrity"},
                    )],
                )
            if self.index == 2:
                return LLMResponse(
                    content="Error calling LLM: maximum context length exceeded"
                )
            return LLMResponse(content="done")

    loop = AgentLoop(
        bus=MessageBus(),
        provider=Provider(),
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=4,
        soft_warn_at_iteration=0,
    )
    input_messages = [{"role": "system", "content": "test"}]
    input_messages.extend(
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"old-{index}"}
        for index in range(30)
    )
    turn_messages: list[dict[str, Any]] = []

    final, _results, _executed, _usage, working_messages = await loop._run_llm_tool_loop(
        messages=input_messages,
        action_turn=False,
        turn_content="search context integrity",
        session_key="web:test",
        tool_platform="web",
        turn_messages_out=turn_messages,
    )

    assert final == "done"
    assert len(working_messages) < len(input_messages)
    assert [message.get("role") for message in turn_messages] == ["assistant", "tool"]
    assert turn_messages[0]["tool_calls"][0]["id"] == "search-1"
    assert turn_messages[1]["tool_call_id"] == "search-1"

    # A later prompt-only mutation cannot corrupt the durable delta.
    working_tool = next(message for message in working_messages if message.get("role") == "tool")
    working_tool["content"] = "mutated prompt projection"
    assert turn_messages[1]["content"] != "mutated prompt projection"

    from flowly.session.manager import Session

    session = Session(key="web:test")
    session.extend_with_turn_messages(
        user_content="search context integrity",
        new_messages=turn_messages,
        final_content=final,
    )
    persisted = session.get_history()
    assert any(message.get("tool_calls") for message in persisted)
    assert any(message.get("tool_call_id") == "search-1" for message in persisted)


@pytest.mark.asyncio
async def test_main_loop_promotes_matching_mcp_tool_before_first_llm_call(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    calls: list[dict[str, Any]] = []

    class RemoteTool(VerboseRoutedTool):
        async def execute(self, **kwargs: Any) -> str:
            calls.append(kwargs)
            return "docs returned"

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.tool_surfaces: list[set[str]] = []

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            surface = _definition_names(kwargs.get("tools") or [])
            self.tool_surfaces.append(surface)
            if len(self.tool_surfaces) == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="call-1",
                        name="mcp_context7_query_docs",
                        arguments={"prompt": "React hooks"},
                    )],
                )
            return LLMResponse(content="done")

    provider = Provider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=3,
        soft_warn_at_iteration=0,
    )
    target = "mcp_context7_query_docs"
    loop.tools.register(RemoteTool(target), toolset="mcp")
    for index in range(4):
        loop.tools.register(
            RemoteTool(f"mcp_typefully_unrelated_action_{index}"),
            toolset="mcp",
        )

    final, results, executed, _usage, _messages = await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "Use Context7 to query React docs"},
        ],
        action_turn=False,
        turn_content="Use Context7 to query React docs",
        session_key="web:test",
        tool_platform="web",
    )

    assert final == "done"
    assert calls == [{"prompt": "React hooks"}]
    assert executed == [target]
    assert results[0]["tool"] == target
    assert target in provider.tool_surfaces[0]
    assert "mcp_typefully_unrelated_action_0" not in provider.tool_surfaces[0]
    assert len(provider.tool_surfaces) == 2


@pytest.mark.asyncio
async def test_unknown_mcp_capability_uses_search_then_call_without_describe(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    calls: list[dict[str, Any]] = []

    class RemoteTool(VerboseRoutedTool):
        async def execute(self, **kwargs: Any) -> str:
            calls.append(kwargs)
            return "rendered"

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.index = 0
            self.tool_surfaces: list[set[str]] = []

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.index += 1
            self.tool_surfaces.append(_definition_names(kwargs.get("tools") or []))
            if self.index == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="search-1",
                        name="tool_search",
                        arguments={"query": "canvas render"},
                    )],
                )
            if self.index == 2:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="call-1",
                        name="tool_call",
                        arguments={
                            "name": "mcp_canvas_render",
                            "arguments": {"prompt": "a cat"},
                        },
                    )],
                )
            return LLMResponse(content="done")

    provider = Provider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=4,
        soft_warn_at_iteration=0,
    )
    loop.tools.register(RemoteTool("mcp_canvas_render"), toolset="mcp")
    for index in range(3):
        loop.tools.register(
            RemoteTool(f"mcp_other_service_{index}"),
            toolset="mcp",
        )

    final, results, executed, _usage, messages = await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "Use an obscure connected capability"},
        ],
        action_turn=False,
        turn_content="Use an obscure connected capability",
        session_key="web:test",
        tool_platform="web",
    )

    assert final == "done"
    assert provider.index == 3
    assert calls == [{"prompt": "a cat"}]
    assert executed == ["mcp_canvas_render"]
    assert [item["tool"] for item in results] == ["tool_search", "mcp_canvas_render"]
    assert all("tool_describe" not in surface for surface in provider.tool_surfaces)
    search_result = next(
        message["content"]
        for message in messages
        if message.get("role") == "tool" and message.get("name") == "tool_search"
    )
    assert '"input_schema"' in search_result


@pytest.mark.asyncio
async def test_explicit_discovery_finds_promoted_workflow_in_one_search(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    calls: list[tuple[str, dict[str, Any]]] = []

    class RemoteTool(VerboseRoutedTool):
        async def execute(self, **kwargs: Any) -> str:
            calls.append((self.name, kwargs))
            return "ok"

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.index = 0
            self.search_matches: set[str] = set()
            self.tool_surfaces: list[set[str]] = []

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.index += 1
            self.tool_surfaces.append(_definition_names(kwargs.get("tools") or []))
            messages = kwargs["messages"]
            if self.index == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="search-1",
                        name="tool_search",
                        arguments={
                            "query": (
                                "Context7 resolve library ID and query "
                                "documentation tools"
                            ),
                            "toolset": "mcp",
                        },
                    )],
                )
            if self.index == 2:
                payload = json.loads(messages[-1]["content"])
                self.search_matches = {
                    match["name"] for match in payload["matches"]
                }
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="resolve-1",
                        name="tool_call",
                        arguments={
                            "name": "mcp_context7_resolve_library_id",
                            "arguments": {"prompt": "React"},
                        },
                    )],
                )
            if self.index == 3:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="docs-1",
                        name="tool_call",
                        arguments={
                            "name": "mcp_context7_query_docs",
                            "arguments": {"prompt": "useEffect cleanup"},
                        },
                    )],
                )
            return LLMResponse(content="done")

    provider = Provider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=5,
        soft_warn_at_iteration=0,
    )
    for name in (
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
        "mcp_context7_unrelated_admin_action",
        "mcp_typefully_typefully_comments_resolve_thread",
    ):
        loop.tools.register(RemoteTool(name), toolset="mcp")

    final, results, executed, _usage, _messages = await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {
                "role": "user",
                "content": (
                    "First use tool_search to find the Context7 documentation "
                    "workflow, then fetch React useEffect cleanup docs."
                ),
            },
        ],
        action_turn=False,
        turn_content=(
            "First use tool_search to find the Context7 documentation workflow, "
            "then fetch React useEffect cleanup docs."
        ),
        session_key="web:test",
        tool_platform="web",
    )

    assert final == "done"
    assert provider.index == 4
    assert "mcp_context7_query_docs" in provider.tool_surfaces[0]
    assert {
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
    } <= provider.search_matches
    assert [name for name, _args in calls] == [
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
    ]
    assert executed == [
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
    ]
    assert [item["tool"] for item in results] == [
        "tool_search",
        "mcp_context7_resolve_library_id",
        "mcp_context7_query_docs",
    ]
