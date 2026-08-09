"""Pre-release matrix for lossless, non-coercive external tool discovery."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.agent.tools.discovery import build_tool_disclosure
from flowly.agent.tools.registry import ToolRegistry
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class CatalogTool(Tool):
    def __init__(self, name: str, *, available: bool = True) -> None:
        self._name = name
        self._available = available
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Capability for {self._name}. " + ("Detailed routing context. " * 30)

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "description": "Complete external input. " * 40,
                },
            },
        }

    def is_available(self) -> bool:
        return self._available

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "ok"


class DescribedCatalogTool(CatalogTool):
    def __init__(self, name: str, description: str) -> None:
        super().__init__(name)
        self._description = description

    @property
    def description(self) -> str:
        return self._description


def _visible_names(disclosure) -> set[str]:
    return {
        definition["function"]["name"]
        for definition in disclosure.definitions
    }


def _search_description(disclosure) -> str:
    return next(
        definition["function"]["description"]
        for definition in disclosure.definitions
        if definition["function"]["name"] == "tool_search"
    )


@pytest.mark.parametrize("catalog_size", [1, 2, 5, 25, 100])
def test_catalog_scale_never_loses_a_route_enabled_tool(catalog_size: int) -> None:
    registry = ToolRegistry()
    registry.register(CatalogTool("read_file"), toolset="filesystem")
    external_names = {
        f"mcp_service_action_{index:03d}"
        for index in range(catalog_size)
    }
    for name in external_names:
        registry.register(CatalogTool(name), toolset="mcp")

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        catalog_max_chars=500,
        search_default_limit=7,
        search_max_limit=7,
    )
    visible = _visible_names(disclosure)

    assert disclosure.all_names == external_names | {"read_file"}
    assert set(disclosure.external) == external_names
    for name in external_names:
        assert name in disclosure.direct_names or (
            "tool_call" in visible and name in disclosure.external
        )

    found: set[str] = set()
    offset = 0
    while True:
        payload = json.loads(disclosure.search("", limit=1000, offset=offset))
        assert len(payload["matches"]) <= 7
        found.update(match["name"] for match in payload["matches"])
        if payload["next_offset"] is None:
            break
        offset = payload["next_offset"]
    assert found == external_names


def test_catalog_budget_keeps_every_connected_source_visible() -> None:
    registry = ToolRegistry()
    for index in range(40):
        registry.register(
            CatalogTool(f"mcp_aaa_action_{index:03d}"),
            toolset="mcp",
        )
    registry.register(
        DescribedCatalogTool(
            "mcp_zzz_query_docs",
            "Query official library documentation and code examples.",
        ),
        toolset="mcp",
    )
    registry.register(
        DescribedCatalogTool(
            "mcp_zzz_resolve_library_id",
            "Resolve a library before querying its documentation.",
        ),
        toolset="mcp",
    )

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        catalog_max_chars=500,
    )
    description = _search_description(disclosure)

    assert "aaa" in description
    assert "zzz" in description
    assert "40 tools" in description
    assert "mcp_zzz_query_docs" in description
    assert "mcp_zzz_resolve_library_id" in description


def test_connected_source_catalog_is_byte_stable() -> None:
    registry = ToolRegistry()
    for name in (
        "mcp_zeta_create_item",
        "mcp_alpha_query_docs",
        "mcp_alpha_resolve_library_id",
    ):
        registry.register(CatalogTool(name), toolset="mcp")

    first = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        catalog_max_chars=500,
    )
    second = build_tool_disclosure(
        reversed(registry.get_definitions()),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        catalog_max_chars=500,
    )

    assert _search_description(first) == _search_description(second)


def test_discovery_disabled_is_a_lossless_kill_switch() -> None:
    registry = ToolRegistry()
    names = {f"mcp_service_action_{index}" for index in range(8)}
    for name in names:
        registry.register(CatalogTool(name), toolset="mcp")

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        enabled=False,
        minimum_deferred_schema_tokens=0,
    )

    assert disclosure.direct_names == names
    assert disclosure.deferred == {}
    assert disclosure.enabled is False
    assert not ({"tool_search", "tool_call"} & _visible_names(disclosure))


def test_always_visible_external_remains_searchable_and_bridge_callable() -> None:
    registry = ToolRegistry()
    registry.register(CatalogTool("mcp_priority_action"), toolset="mcp")
    for index in range(5):
        registry.register(CatalogTool(f"mcp_hidden_action_{index}"), toolset="mcp")

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        always_visible_tools={"mcp_priority_action"},
        minimum_deferred_schema_tokens=0,
    )
    payload = json.loads(disclosure.search("priority", toolset="mcp"))
    priority = next(
        match for match in payload["matches"]
        if match["name"] == "mcp_priority_action"
    )

    assert priority["visibility"] == "direct"
    assert priority["invoke_via"] == ["mcp_priority_action", "tool_call"]
    assert disclosure.resolve_call("mcp_priority_action", {"value": "x"}) == (
        "mcp_priority_action",
        {"value": "x"},
    )


def test_search_provider_scope_and_multi_provider_scope_are_deterministic() -> None:
    registry = ToolRegistry()
    for name in (
        "mcp_context7_query_docs",
        "mcp_context7_resolve_library_id",
        "mcp_typefully_resolve_thread",
        "mcp_typefully_list_drafts",
        "mcp_other_resolve_resource",
    ):
        registry.register(CatalogTool(name), toolset="mcp")
    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
    )

    context7 = json.loads(disclosure.search("Context7 resolve docs", limit=20))
    context7_names = {match["name"] for match in context7["matches"]}
    multi = json.loads(disclosure.search("Context7 Typefully resolve", limit=20))
    multi_names = {match["name"] for match in multi["matches"]}

    assert context7_names == {
        "mcp_context7_query_docs",
        "mcp_context7_resolve_library_id",
    }
    assert multi_names == {
        "mcp_context7_query_docs",
        "mcp_context7_resolve_library_id",
        "mcp_typefully_resolve_thread",
        "mcp_typefully_list_drafts",
    }


def test_named_small_mcp_workflow_is_promoted_across_user_language() -> None:
    registry = ToolRegistry()
    context7_names = {
        "mcp_context7_query_docs",
        "mcp_context7_resolve_library_id",
    }
    for name in context7_names | {"mcp_typefully_list_drafts"}:
        registry.register(CatalogTool(name), toolset="mcp")

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        intent_text=(
            "Context7 aracını kullanarak React useEffect cleanup "
            "dokümantasyonunu bul ve Türkçe özetle."
        ),
    )

    assert disclosure.promoted_names == context7_names
    assert "mcp_typefully_list_drafts" in disclosure.deferred


def test_unique_small_capability_workflow_is_promoted_without_provider_name() -> None:
    registry = ToolRegistry()
    documentation_names = {
        "mcp_reference_query_docs",
        "mcp_reference_resolve_library_id",
    }
    registry.register(
        DescribedCatalogTool(
            "mcp_reference_query_docs",
            "Query official library documentation and code examples.",
        ),
        toolset="mcp",
    )
    registry.register(
        DescribedCatalogTool(
            "mcp_reference_resolve_library_id",
            "Resolve a package to its library ID before querying documentation.",
        ),
        toolset="mcp",
    )
    registry.register(
        DescribedCatalogTool(
            "mcp_social_schedule_post",
            "Schedule and publish social media posts and drafts.",
        ),
        toolset="mcp",
    )
    for index in range(4):
        registry.register(
            CatalogTool(f"mcp_other_action_{index}"),
            toolset="mcp",
        )

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        intent_text=(
            "React useEffect cleanup davranışını resmi dokümantasyondan "
            "kontrol edip Türkçe özetle."
        ),
    )

    assert disclosure.promoted_names == documentation_names
    assert "mcp_social_schedule_post" in disclosure.deferred


def test_ambiguous_capability_does_not_force_a_provider() -> None:
    registry = ToolRegistry()
    for source in ("alpha", "beta"):
        registry.register(
            DescribedCatalogTool(
                f"mcp_{source}_query_docs",
                "Query official library documentation and code examples.",
            ),
            toolset="mcp",
        )
        registry.register(
            DescribedCatalogTool(
                f"mcp_{source}_resolve_library_id",
                "Resolve a package before querying its documentation.",
            ),
            toolset="mcp",
        )

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        intent_text="Resmi dokümantasyonu kullanarak bu davranışı doğrula.",
    )

    assert disclosure.promoted_names == frozenset()
    assert set(disclosure.deferred) == {
        "mcp_alpha_query_docs",
        "mcp_alpha_resolve_library_id",
        "mcp_beta_query_docs",
        "mcp_beta_resolve_library_id",
    }


def test_generic_description_words_do_not_promote_an_unrelated_provider() -> None:
    class DescribedTool(CatalogTool):
        def __init__(self, name: str, description: str) -> None:
            super().__init__(name)
            self._description = description

        @property
        def description(self) -> str:
            return self._description

    registry = ToolRegistry()
    registry.register(DescribedTool(
        "mcp_context7_query_docs",
        "Query library documentation and code examples.",
    ), toolset="mcp")
    registry.register(DescribedTool(
        "mcp_typefully_get_social_set_details",
        (
            "Get authoritative information about a connected external social "
            "integration and its details."
        ),
    ), toolset="mcp")
    for index in range(4):
        registry.register(
            CatalogTool(f"mcp_other_action_{index}"),
            toolset="mcp",
        )

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        intent_text=(
            "Without using web search, use any connected external documentation "
            "integration to find authoritative information about React useEffect "
            "cleanup. Choose the appropriate tool yourself."
        ),
    )

    assert "mcp_typefully_get_social_set_details" not in disclosure.promoted_names
    assert "mcp_typefully_get_social_set_details" in disclosure.external


def test_local_project_file_request_does_not_promote_context7() -> None:
    class DescribedTool(CatalogTool):
        @property
        def description(self) -> str:
            return (
                "Resolves a package or project name to a Context7-compatible "
                "library ID and returns matching documentation libraries."
            )

    registry = ToolRegistry()
    registry.register(
        DescribedTool("mcp_context7_resolve_library_id"),
        toolset="mcp",
    )
    for index in range(4):
        registry.register(
            CatalogTool(f"mcp_other_action_{index}"),
            toolset="mcp",
        )

    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
        intent_text=(
            "read_file aracını kullanarak pyproject.toml dosyasındaki "
            "project name değerini söyle"
        ),
    )

    assert "mcp_context7_resolve_library_id" not in disclosure.promoted_names


def test_bridge_rejects_core_absent_and_invalid_external_calls() -> None:
    registry = ToolRegistry()
    registry.register(CatalogTool("read_file"), toolset="filesystem")
    registry.register(CatalogTool("mcp_external_action"), toolset="mcp")
    registry.register(CatalogTool("mcp_other_action"), toolset="mcp")
    disclosure = build_tool_disclosure(
        registry.get_definitions(),
        toolsets=registry.get_toolsets(),
        minimum_deferred_schema_tokens=0,
    )

    assert isinstance(disclosure.resolve_call("read_file", {}), str)
    assert isinstance(disclosure.resolve_call("missing", {}), str)
    assert isinstance(disclosure.resolve_call("mcp_external_action", []), str)
    assert disclosure.resolve_call(
        "mcp_external_action", '{"value":"ok"}'
    ) == ("mcp_external_action", {"value": "ok"})


def test_registry_concurrent_registration_and_routed_reads_are_consistent() -> None:
    registry = ToolRegistry(
        availability_cache_ttl=0,
        schema_cache_ttl=0,
    )
    expected = {f"mcp_concurrent_action_{index}" for index in range(100)}

    def register_all() -> None:
        for name in sorted(expected):
            registry.register(CatalogTool(name), toolset="mcp")

    def read_repeatedly() -> None:
        for _ in range(100):
            definitions = registry.get_definitions(platform="web")
            names = {
                definition["function"]["name"]
                for definition in definitions
            }
            assert names <= expected

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(register_all)]
        futures.extend(pool.submit(read_repeatedly) for _ in range(5))
        for future in futures:
            future.result()

    assert set(registry.get_available_names(platform="web")) == expected


@pytest.mark.asyncio
async def test_model_may_answer_without_using_discovery_tools(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.calls = 0

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.calls += 1
            assert {"tool_search", "tool_call"} <= {
                definition["function"]["name"]
                for definition in kwargs["tools"]
            }
            return LLMResponse(content="No tool is needed for this answer.")

    provider = Provider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=3,
        soft_warn_at_iteration=0,
    )
    for index in range(5):
        loop.tools.register(
            CatalogTool(f"mcp_external_action_{index}"),
            toolset="mcp",
        )

    final, results, executed, _usage, _messages = await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "Explain this concept from memory."},
        ],
        action_turn=False,
        turn_content="Explain this concept from memory.",
        session_key="web:test",
        tool_platform="web",
    )

    assert final == "No tool is needed for this answer."
    assert provider.calls == 1
    assert results == []
    assert executed == []


@pytest.mark.asyncio
async def test_hallucinated_hidden_direct_call_is_blocked_at_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    target = CatalogTool("mcp_hidden_target")

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.calls = 0

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                visible = {
                    definition["function"]["name"]
                    for definition in kwargs["tools"]
                }
                assert target.name not in visible
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id="hallucinated-1",
                        name=target.name,
                        arguments={"value": "unsafe"},
                    )],
                )
            return LLMResponse(content="recovered")

    provider = Provider()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=3,
        soft_warn_at_iteration=0,
    )
    loop.tools.register(target, toolset="mcp")
    for index in range(4):
        loop.tools.register(
            CatalogTool(f"mcp_other_action_{index}"),
            toolset="mcp",
        )

    final, results, executed, _usage, _messages = await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "Use an unrelated capability."},
        ],
        action_turn=False,
        turn_content="Use an unrelated capability.",
        session_key="web:test",
        tool_platform="web",
    )

    assert final == "recovered"
    assert target.calls == []
    assert executed == [target.name]
    assert results[0]["success"] is False
    assert "unavailable" in results[0]["result"]


@pytest.mark.asyncio
async def test_repeated_search_remains_allowed_and_reports_no_new_names(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.calls = 0
            self.search_payloads: list[dict[str, Any]] = []

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.calls += 1
            messages = kwargs["messages"]
            if self.calls > 1:
                self.search_payloads.append(json.loads(messages[-1]["content"]))
            if self.calls <= 2:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest(
                        id=f"search-{self.calls}",
                        name="tool_search",
                        arguments={
                            "query": "Context7 docs" if self.calls == 1
                            else "Context7 documentation",
                            "toolset": "mcp",
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
    for name in (
        "mcp_context7_query_docs",
        "mcp_context7_resolve_library_id",
        "mcp_other_action",
    ):
        loop.tools.register(CatalogTool(name), toolset="mcp")

    final, results, _executed, _usage, _messages = await loop._run_llm_tool_loop(
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "Inspect available Context7 tools."},
        ],
        action_turn=False,
        turn_content="Inspect available Context7 tools.",
        session_key="web:test",
        tool_platform="web",
    )

    assert final == "done"
    assert [result["tool"] for result in results] == ["tool_search", "tool_search"]
    assert provider.search_payloads[0]["repeated_result_set"] is False
    assert provider.search_payloads[1]["repeated_result_set"] is True
    assert provider.search_payloads[1]["previous_occurrences"] == 1
