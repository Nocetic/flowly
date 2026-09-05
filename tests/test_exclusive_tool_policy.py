"""Exclusive grants come from imperative selectors, never incidental mentions."""

from __future__ import annotations

import pytest

from flowly.agent.tool_policy import (
    has_explicit_tool_invocation_intent,
    resolve_exclusive_tool_scope,
    resolve_turn_tool_policy,
)

CATALOG = [
    ("mcp_context7_resolve_library_id", "context7"),
    ("mcp_context7_query_docs", "context7"),
    ("mcp_other_query_docs", "other"),
    ("web_search", "builtin"),
    ("board_list", "builtin"),
]
CONTEXT = frozenset({"mcp_context7_resolve_library_id", "mcp_context7_query_docs"})


@pytest.mark.parametrize("text", [
    "Sadece Context7 kullan.",
    "Yalnızca Context7'yi kullan.",
    "Yalnizca Context7 kullanin.",
    "Lütfen yalnızca Context7'yi çağırın.",
    "SADECE CONTEXT7'Yİ KULLANIN.",
    "Context7'yi sadece kullan.",
    "Context7 kullan, başka araç kullanma.",
    "Context7'yi çağır; diğer araçları çağırma.",
    "Only use Context7.",
    "Please use only Context7.",
    "Use Context7 only.",
    "Invoke exclusively the Context7 MCP server.",
    "Use Context7, do not use any other tools.",
    "Do not use any tools except Context7.",
    "Context7 dışında hiçbir araç kullanma.",
    "Context7 haricinde başka araç kullanma.",
    "For this task, please only use Context7.",
    "Lütfen bu görev için sadece Context7 kullan.",
    "Don't use any tools, except Context7.",
    "Use no tools except Context7.",
    "Context7 hariç hiçbir araç kullanma.",
])
def test_equivalent_explicit_selectors_have_the_same_grant(text):
    policy = resolve_turn_tool_policy({}, text)
    assert policy.allowed and policy.exclusive
    assert has_explicit_tool_invocation_intent(text)
    assert resolve_exclusive_tool_scope(text, CATALOG) == CONTEXT


@pytest.mark.parametrize("text, expected", [
    ("Only use Context7. Do not use web_search.", CONTEXT),
    ("Use only Context7, not web_search.", CONTEXT),
    ("Sadece Context7 kullan; web_search kullanma.", CONTEXT),
    ("Only use Context7. Explain web_search.", CONTEXT),
    ("Use Context7 for information about web_search, no other tools.", CONTEXT),
    ("Only use MissingServer. Context7 is an unrelated example.", frozenset()),
    ("Only use Context7 and web_search.", CONTEXT | {"web_search"}),
    ("Sadece Context7 ve board_list kullan.", CONTEXT | {"board_list"}),
    ("Only use Context7. Only use web_search.", frozenset()),
    ("Only use query_docs.", frozenset()),
    ("Only use Context7 query-docs.", frozenset({"mcp_context7_query_docs"})),
    ("Only use mcp_context7_query_docs.", frozenset({"mcp_context7_query_docs"})),
    ("Only use Context7. Never call query_docs.", frozenset({"mcp_context7_resolve_library_id"})),
    ("Sadece Context7 kullan. query_docs çağırma.", frozenset({"mcp_context7_resolve_library_id"})),
    ("Use only `web_search`.", frozenset({"web_search"})),
    ("Sadece Context7 kullan, web_search kullanma.", CONTEXT),
    ("Sadece Context7 kullan ama web_search kullanma.", CONTEXT),
    ("Sadece Context7 kullan, query_docs değil.", frozenset({"mcp_context7_resolve_library_id"})),
    ("Sadece Context7 kullan, query_docs hariç.", frozenset({"mcp_context7_resolve_library_id"})),
    ("Use only Context7 excluding query_docs.", frozenset({"mcp_context7_resolve_library_id"})),
    ("Only use Context7 not query_docs.", frozenset({"mcp_context7_resolve_library_id"})),
    ("Only use Context7 no query_docs.", frozenset({"mcp_context7_resolve_library_id"})),
    ("Only use Context7; never query_docs.", frozenset({"mcp_context7_resolve_library_id"})),
])
def test_scope_uses_only_selected_targets_and_subtracts_denials(text, expected):
    assert resolve_turn_tool_policy({}, text).exclusive
    assert resolve_exclusive_tool_scope(text, CATALOG) == expected


@pytest.mark.parametrize("text", [
    'Explain the instruction "Only use Context7".',
    '"Sadece Context7 kullan" ne demek?',
    "Explain how to only use Context7.",
    "If I say only use Context7, what happens?",
    "````text\nOnly use Context7.\n````\nSummarize the example.",
    "> Only use Context7.\nExplain the quoted instruction.",
    "Use Context7 only once.",
    "Sadece Context7 kullanımını anlat.",
    "Use Context7 only when needed.",
])
def test_discussion_examples_and_frequency_do_not_create_exclusive_grants(text):
    assert not resolve_turn_tool_policy({}, text).exclusive


@pytest.mark.parametrize("metadata", [{"tools_allowed": True}, {"tool_policy": "auto"}])
def test_structured_allow_does_not_erase_an_explicit_narrowing(metadata):
    policy = resolve_turn_tool_policy(metadata, "Only use Context7.")
    assert policy.allowed and policy.exclusive


@pytest.mark.parametrize("text", [
    "Only use Context7. Do not use any tools.",
    "Sadece Context7 kullan. Hiçbir araç kullanma.",
    "Use no other tools.",
    "Başka araç kullanma.",
])
def test_global_or_unresolved_deny_stays_closed(text):
    assert not resolve_turn_tool_policy({}, text).allowed


def test_exact_identifier_wins_over_an_inflected_shorter_identifier():
    assert resolve_exclusive_tool_scope("Only use fooi.", [("foo", "builtin"), ("fooi", "builtin")]) == {"fooi"}


def test_normalized_name_collision_is_not_an_implicit_multi_tool_grant():
    assert resolve_exclusive_tool_scope("Only use araç.", [("arac", "builtin"), ("araç", "builtin")]) == frozenset()


@pytest.mark.parametrize("grant", ["web_search", {}, 1, ["web_search", None], [""]])
def test_malformed_structured_grants_are_empty_not_unrestricted(grant):
    from flowly.agent.loop import resolve_capability_disabled_tools

    assert resolve_capability_disabled_tools({"web_search", "board_list"}, [], grant) == ["board_list", "web_search"]


def test_frozen_structured_grants_are_supported():
    from flowly.agent.loop import resolve_capability_disabled_tools

    assert resolve_capability_disabled_tools({"web_search", "board_list"}, [], frozenset({"board_list"})) == ["web_search"]


@pytest.mark.parametrize("text", ["x" * 65537, "Only use Context7;" * 257])
def test_policy_input_bounds_fail_closed(text):
    assert resolve_turn_tool_policy({}, text).allowed is False
    assert resolve_exclusive_tool_scope(text, CATALOG) == frozenset()


class _PolicyProvider:
    """A scripted provider deliberately requests out-of-scope/late tools."""

    @staticmethod
    def build():
        from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

        class Provider(LLMProvider):
            def __init__(self):
                super().__init__(api_key="fixture")
                self.calls = []
                self.requests = []
                self.on_first_call = None

            def get_default_model(self):
                return "fixture/model"

            async def chat(self, **kwargs):
                self.calls.append(kwargs)
                if self.on_first_call:
                    callback, self.on_first_call = self.on_first_call, None
                    callback()
                if self.requests:
                    name = self.requests.pop(0)
                    return LLMResponse(content=None, tool_calls=[ToolCallRequest(id=f"call-{len(self.calls)}", name=name, arguments={})])
                return LLMResponse(content="POLICY_OK")

        return Provider()


@pytest.fixture
async def owner(tmp_path, monkeypatch):
    from flowly.agent.loop import AgentLoop
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from tests.test_no_tools_policy import _ScopedTool

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    provider = _PolicyProvider.build()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, main_config=Config(), max_iterations=6, soft_warn_at_iteration=0)
    tools = {name: _ScopedTool(name, source) for name, source in CATALOG[:3]}
    for tool in tools.values():
        loop.tools.register(tool, toolset="mcp")
    session = loop.sessions.get_or_create("web:policy")
    session.metadata["title"] = "Existing title"
    loop.sessions.save(session)
    try:
        yield loop, provider, tools
    finally:
        loop.stop()


@pytest.mark.parametrize("text", ["Sadece Context7 kullan.", "Only use Context7.", "Context7 kullan, başka araç kullanma."])
@pytest.mark.parametrize("grant", [None, ["mcp_context7_query_docs"], frozenset({"mcp_context7_query_docs"}), [], "invalid"])
async def test_real_agent_loop_intersects_text_with_transport_grants(owner, text, grant):
    from flowly.bus.events import InboundMessage

    loop, provider, tools = owner
    provider.requests = ["mcp_other_query_docs", "mcp_context7_resolve_library_id", "mcp_context7_query_docs"]
    message = InboundMessage(channel="web", sender_id="fixture", chat_id="policy", content=text,
        metadata={"tools_allowed": True, "allowed_tools": grant})
    await loop._process_message(message)
    expected = CONTEXT if grant is None else (frozenset(grant) if not isinstance(grant, str) else frozenset())
    for call in provider.calls:
        advertised = {item["function"]["name"] for item in call.get("tools", [])}
        assert advertised <= expected
    assert {name for name, tool in tools.items() if tool.calls} == expected


async def test_late_registration_cannot_widen_turn_but_next_turn_is_independent(owner):
    from flowly.agent.tool_context import current_tool_origin
    from tests.test_no_tools_policy import _ScopedTool

    loop, provider, tools = owner
    late = _ScopedTool("mcp_context7_late", "context7")
    provider.on_first_call = lambda: loop.tools.register(late, toolset="mcp")
    provider.requests = [late.name, "mcp_context7_query_docs"]
    await loop.process_direct("Only use Context7.", session_key="web:policy")
    assert not late.calls
    assert tools["mcp_context7_query_docs"].calls == [{}]
    assert all(late.name not in {item["function"]["name"] for item in call.get("tools", [])} for call in provider.calls)
    assert current_tool_origin() is None
    provider.requests = [late.name]
    await loop.process_direct("Continue.", session_key="web:policy")
    assert late.calls == [{}]


async def test_language_and_structured_ceiling_reach_real_delegated_stdio(owner):
    import json
    import os

    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from flowly.agent.tools.base import Tool
    from flowly.agent.tools.board import build_board_tools
    from flowly.board.store import BoardStore
    from flowly.bus.events import InboundMessage
    from flowly.mcp.server.managed_tools import managed_tool_launch

    loop, provider, _ = owner
    store = BoardStore(loop.workspace / "policy-board.db")
    for tool in build_board_tools(store):
        loop.tools.register(tool)
    seen = []

    class Probe(Tool):
        name = "scope_probe"
        description = "Exercise delegated stdio from a real parent turn"
        parameters = {"type": "object", "properties": {}}

        async def execute(self):
            async with managed_tool_launch(loop, "web:policy", allow_writes=True) as launch:
                async with Client(stdio_client(StdioServerParameters(
                    command=launch.command, args=launch.args, env={**os.environ, **launch.env},
                ))) as client:
                    seen.append({item.name for item in (await client.session.list_tools()).tools})
                    assert (await client.session.call_tool("board_add", {"title": "must not write"})).is_error
                    return json.dumps({"ok": not (await client.session.call_tool("board_list", {})).is_error})

    loop.tools.register(Probe())
    provider.requests = ["scope_probe"]
    try:
        await loop._process_message(InboundMessage(channel="web", sender_id="fixture", chat_id="policy",
            content="Sadece scope_probe ve board_list kullan.",
            metadata={"tools_allowed": True, "allowed_tools": ["scope_probe", "board_list", "board_add"]}))
        assert seen == [{"board_list"}]
        assert not store.list_cards()
    finally:
        store.close()


async def test_concurrent_turns_keep_separate_grants_and_schema_disclosure(owner):
    import asyncio
    from collections import defaultdict

    from flowly.agent.tool_context import current_tool_origin
    from flowly.bus.events import InboundMessage
    from flowly.providers.base import LLMResponse, ToolCallRequest

    loop, provider, tools = owner
    targets = {"web:first": "mcp_context7_query_docs", "web:second": "mcp_other_query_docs"}
    counts = defaultdict(int)
    seen = []

    async def chat(**kwargs):
        origin = current_tool_origin()
        key = origin.session_key
        assert origin.allowed_tools == frozenset({targets[key]})
        seen.append((key, {item["function"]["name"] for item in kwargs.get("tools", [])}))
        number = counts[key]
        counts[key] += 1
        await asyncio.sleep(0)
        if number >= 2:
            return LLMResponse(content="POLICY_OK")
        name = targets[key] if number else next(value for other, value in targets.items() if other != key)
        return LLMResponse(content=None, tool_calls=[ToolCallRequest(id=f"{key}-{number}", name=name, arguments={})])

    provider.chat = chat
    for key in targets:
        session = loop.sessions.get_or_create(key)
        session.metadata["title"] = "Existing title"
        loop.sessions.save(session)
    await asyncio.gather(*[
        loop._process_message(InboundMessage(channel="web", sender_id="fixture", chat_id=key.split(":", 1)[1],
            content=f"Only use {name}.", metadata={"allowed_tools": [name]}))
        for key, name in targets.items()
    ])
    assert all(names <= {targets[key]} for key, names in seen)
    assert tools["mcp_context7_query_docs"].calls == [{}]
    assert tools["mcp_other_query_docs"].calls == [{}]
    assert current_tool_origin() is None
