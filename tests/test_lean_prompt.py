"""Rarely used built-in tools wait behind the bridge, without a turn losing anything.

Before: every built-in tool's full schema went out on every request, so the
fixed cost of a request grew with each tool added (15.6K tokens of 73 tools on
the owner's Mac, 2026-11-06), whether or not the turn could use them. Now the
tools almost never called (discovery.DEFAULT_DEFERRED_TOOLS, chosen from 1267
sessions) wait behind tool_search / tool_call, and nothing is lost:

* a tool a session uses or promotes stays direct for that session, so the tool
  list grows at most once per newly needed tool (prompt and KV caches survive);
* a tool called by name without its schema is not run, but its schema is
  direct from the next step;
* a capability playbook and its tool always arrive together;
* the system prompt keeps a deferred tool's guidance and does not change when
  the tool is promoted.
"""

from __future__ import annotations

from typing import Any

import pytest

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.agent.tools.discovery import DEFAULT_DEFERRED_TOOLS, build_tool_disclosure
from flowly.bus.queue import MessageBus
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _Tool(Tool):
    def __init__(self, name: str, *, required: tuple[str, ...] = ()) -> None:
        self._name = name
        self._required = required
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"The {self._name} tool. " + ("It explains its options at length. " * 25)

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string", "description": "Input. " * 30}},
            "required": list(self._required),
        }

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"{self._name} ran"


def _names(definitions) -> set[str]:
    return {d["function"]["name"] for d in definitions}


# ── the disclosure ────────────────────────────────────────────────────────

COLD = ("linear", "docker", "video_analyze", "obsidian_read", "memory_feedback")
HOT = ("exec", "read_file", "web_search")


def _defs(*names: str) -> list[dict[str, Any]]:
    return [_Tool(name).to_schema() for name in names]


def test_rarely_used_builtins_wait_behind_the_bridge_and_stay_callable():
    disclosure = build_tool_disclosure(_defs(*HOT, *COLD))
    visible = _names(disclosure.definitions)
    assert set(HOT) <= visible
    assert not (set(COLD) & visible)
    assert {"tool_search", "tool_call"} <= visible
    assert set(COLD) <= set(disclosure.deferred)
    # tool_call runs them, with the same arguments the tool takes.
    assert disclosure.resolve_call("docker", {"value": "ps"}) == ("docker", {"value": "ps"})
    assert disclosure.disclosed_schema_tokens < disclosure.original_schema_tokens


def test_a_pinned_tool_is_direct_and_still_reachable_through_the_bridge():
    disclosure = build_tool_disclosure(_defs(*HOT, *COLD), pinned_tools={"docker"})
    assert "docker" in _names(disclosure.definitions)
    assert "docker" not in disclosure.deferred
    assert disclosure.resolve_call("docker", {}) == ("docker", {})


def test_an_unlisted_builtin_inferred_as_an_extension_is_not_swept_along():
    """``goal`` has no toolset entry, so it is inferred as ``extensions``. On
    its own it is below the deferral threshold; the named built-ins crossing
    it must not hide it too."""
    disclosure = build_tool_disclosure(
        [{"type": "function", "function": {"name": "goal", "description": "Standing goal.", "parameters": {}}},
         *_defs(*HOT, *COLD)],
        toolsets={"goal": "extensions"},
    )
    assert "goal" in _names(disclosure.definitions)
    assert "goal" not in disclosure.deferred


def test_an_empty_list_keeps_every_builtin_direct():
    disclosure = build_tool_disclosure(_defs(*HOT, *COLD), deferred_tools=())
    assert set(COLD) <= _names(disclosure.definitions)
    assert not disclosure.enabled


# ── the agent loop ────────────────────────────────────────────────────────


class _Provider(LLMProvider):
    def __init__(self, script):
        super().__init__(api_key="test")
        self.script = list(script)
        self.seen_tools: list[set[str]] = []

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.seen_tools.append(_names(kwargs.get("tools") or []))
        step = self.script.pop(0)
        return step(self.seen_tools[-1]) if callable(step) else step


def _loop(tmp_path, monkeypatch, provider) -> AgentLoop:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, main_config=Config(),
                     max_iterations=4, soft_warn_at_iteration=0)
    return loop


def _call(name: str, arguments: dict, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)])


async def _turn(loop: AgentLoop, text: str, session: str = "web:s1", messages=None):
    return await loop._run_llm_tool_loop(
        messages=messages or [{"role": "system", "content": "test"}, {"role": "user", "content": text}],
        turn_content=text, session_key=session, tool_platform="web")


def _register(loop: AgentLoop, *names: str) -> dict[str, _Tool]:
    tools = {name: _Tool(name, required=("value",)) for name in names}
    for tool in tools.values():
        loop.tools.register(tool)
    return tools


@pytest.mark.asyncio
async def test_a_tool_used_through_the_bridge_stays_direct_and_the_list_stops_changing(tmp_path, monkeypatch):
    provider = _Provider([
        _call("tool_call", {"name": "docker", "arguments": {"value": "ps"}}),
        LLMResponse(content="done"),
        LLMResponse(content="again"),
        LLMResponse(content="and again"),
    ])
    loop = _loop(tmp_path, monkeypatch, provider)
    tools = _register(loop, *COLD)

    await _turn(loop, "Show the running containers.")
    assert tools["docker"].calls == [{"value": "ps"}]
    assert "docker" not in provider.seen_tools[0]
    assert "docker" in provider.seen_tools[1]  # direct from the next step

    await _turn(loop, "thanks")
    await _turn(loop, "ok")
    # Later turns of the session carry the same list: one change, not a flip.
    assert provider.seen_tools[1] == provider.seen_tools[2] == provider.seen_tools[3]
    # Another session starts lean.
    other = _Provider([LLMResponse(content="hi")])
    loop.provider = other
    await _turn(loop, "hello", session="web:s2")
    assert "docker" not in other.seen_tools[0]


@pytest.mark.asyncio
async def test_a_deferred_tool_called_by_name_is_not_run_but_is_direct_next_step(tmp_path, monkeypatch):
    provider = _Provider([
        _call("docker", {"value": "guessed"}),
        lambda visible: _call("docker", {"value": "ps"}, "c2") if "docker" in visible else LLMResponse(content="lost"),
        LLMResponse(content="done"),
    ])
    loop = _loop(tmp_path, monkeypatch, provider)
    tools = _register(loop, *COLD)

    final, results, _executed, _usage, _messages = await _turn(loop, "Show the running containers.")

    assert results[0]["success"] is False and "next step" in results[0]["result"]
    assert tools["docker"].calls == [{"value": "ps"}]  # only the call made with its schema
    assert final == "done"


@pytest.mark.asyncio
async def test_a_hallucinated_tool_the_route_does_not_have_stays_blocked(tmp_path, monkeypatch):
    provider = _Provider([_call("no_such_tool", {}), LLMResponse(content="recovered")])
    loop = _loop(tmp_path, monkeypatch, provider)
    _register(loop, *COLD)
    _final, results, *_ = await _turn(loop, "do something")
    assert "unavailable for platform" in results[0]["result"]
    assert "no_such_tool" not in loop._session_pins("web:s1")


@pytest.mark.asyncio
async def test_the_computer_playbook_brings_its_tool(tmp_path, monkeypatch):
    provider = _Provider([LLMResponse(content="ok")])
    loop = _loop(tmp_path, monkeypatch, provider)
    _register(loop, "computer", "screenshot", *COLD)
    messages = [
        {"role": "system", "content": "test"},
        {"role": "system", "content": "playbook", "_capability_guidance": ["computer"]},
        {"role": "user", "content": "ekranda ne var"},
    ]
    await _turn(loop, "ekranda ne var", messages=messages)
    assert {"computer", "screenshot"} <= provider.seen_tools[0]


def test_the_turn_estimate_pins_what_the_message_asks_for():
    assert AgentLoop._turn_playbook_pins("ekranda ne yazıyor") == {"computer", "screenshot"}
    assert AgentLoop._turn_playbook_pins("selam") == frozenset()


def test_every_default_deferred_name_is_a_real_builtin(tmp_path, monkeypatch):
    """A renamed tool would silently go back to costing every request."""
    from flowly.agent.tools.routing import infer_toolset

    unknown = sorted(name for name in DEFAULT_DEFERRED_TOOLS if infer_toolset(name) in {"extensions", "mcp"})
    assert unknown == []


# ── the system prompt ─────────────────────────────────────────────────────


def test_the_system_prompt_keeps_guidance_and_does_not_change_on_promotion(tmp_path, monkeypatch):
    from flowly.agent.context import ContextBuilder

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    builder = ContextBuilder(tmp_path)
    reachable = {"exec", "read_file", "docker", "linear", "tool_search", "tool_call"}
    deferred_view = {"exec", "read_file", "tool_search", "tool_call"}
    promoted_view = deferred_view | {"docker"}

    def prompt(visible):
        return builder.build_system_prompt(available_tools=visible, reachable_tools=reachable)

    lean = prompt(deferred_view)
    assert lean == prompt(promoted_view)
    assert "## Docker Integration" in lean  # a waiting tool keeps its guide
    assert "# Deferred Tools" in lean


def test_naming_a_waiting_tool_brings_it_direct_in_the_same_request():
    """Its guide names it (Docker, Linear, email); a message that names it
    too gets its schema at once, with no blocked step."""
    disclosure = build_tool_disclosure(_defs(*HOT, *COLD), intent_text="docker container'larını listele")
    assert "docker" in _names(disclosure.definitions)
    assert "docker" in disclosure.promoted_names


def test_tools_a_prompt_names_are_not_deferred():
    from flowly.agent.tools.discovery import DEFAULT_DEFERRED_TOOLS as names
    assert not ({"session_search", "skill_manage", "follow_up", "proactivity", "process", "request_secret",
                 "email", "google_connection"} & names)
    assert not {name for name in names if name.startswith("agent_")}
