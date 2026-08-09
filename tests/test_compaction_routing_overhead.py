"""Compaction must price the request the route will actually send.

Routing and progressive disclosure shrink what a turn puts on the wire: the
platform's own tools plus a compact bridge, and a system prompt without the
playbooks this turn does not need. The compaction overhead is computed from
the same two things.

If either estimate reverts to the eager surface — the whole registry, or the
legacy always-on prompt — it over-counts by precisely the amount the feature
saves. The turn then compacts a session that never outgrew its window, which
both cancels the benefit and inflates the context figure clients draw. The
merge with main's `fixed_overhead` accounting is exactly where that regression
would reappear, so it is pinned here rather than left to review.
"""

from __future__ import annotations

import json
from typing import Any

from flowly.agent.loop import AgentLoop
from flowly.agent.tools.base import Tool
from flowly.bus.queue import MessageBus
from flowly.compaction.estimator import estimate_tokens
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse


class _BulkyExternalTool(Tool):
    """An MCP-shaped tool whose schema is large enough to be worth deferring."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"External capability {self._name}. " + ("Routing detail. " * 40)

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

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _IdleProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="unused")


def _loop_with_external_catalog(tmp_path, count: int = 24) -> AgentLoop:
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_IdleProvider(),
        workspace=tmp_path,
        main_config=Config(),
        max_iterations=2,
        soft_warn_at_iteration=0,
    )
    for index in range(count):
        loop.tools.register(
            _BulkyExternalTool(f"mcp_vendor_action_{index}"),
            toolset="mcp",
        )
    return loop


def test_schema_overhead_prices_the_disclosed_surface(tmp_path):
    loop = _loop_with_external_catalog(tmp_path)

    disclosure = loop._routed_tool_disclosure(None)
    assert disclosure.enabled, (
        "the fixture no longer defers anything, so this test would pass "
        "against an unrouted estimate too"
    )

    eager_tokens = estimate_tokens(json.dumps(loop.tools.get_definitions()))
    routed_tokens = loop._tool_schema_tokens(None)

    assert routed_tokens < eager_tokens, (
        "the compaction estimate is pricing the whole registry again — every "
        "deferred schema is counted against a window that never carries it"
    )
    assert routed_tokens == estimate_tokens(
        json.dumps(list(disclosure.definitions))
    ), "the estimate and the wire disagree about which schemas this turn sends"


def test_schema_overhead_follows_the_platform_route(tmp_path):
    loop = _loop_with_external_catalog(tmp_path)

    # Cron may not deliver, schedule, or ask — a narrower surface than chat,
    # so its request is cheaper and its budget must say so.
    cron_tokens = loop._tool_schema_tokens("cron")
    chat_tokens = loop._tool_schema_tokens("telegram")

    assert cron_tokens != chat_tokens, (
        "every platform is being charged one shared estimate, so the memo key "
        "has lost the route and one surface is paying another's schemas"
    )


def test_prompt_overhead_drops_guidance_the_route_cannot_use(tmp_path):
    loop = _loop_with_external_catalog(tmp_path)

    eager = loop._system_prompt_tokens("telegram")
    advertised, reachable = loop._routed_prompt_surface("telegram")
    routed = loop._system_prompt_tokens(
        "telegram",
        available_tools=advertised,
        reachable_tools=reachable,
    )

    assert advertised, "the fixture routed no tools at all"
    assert routed < eager, (
        "the prompt estimate is back on the legacy eager build — it counts "
        "playbooks and per-tool guidance the routed turn never sends"
    )
