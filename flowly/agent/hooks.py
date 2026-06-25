"""Hook system for agent lifecycle events.

Hooks observe and react to runtime events: tool calls, LLM calls,
session lifecycle, gateway message dispatch.  Both core code and
plugins register callbacks against the same registry.

Usage::

    from flowly.agent.hooks import HookRegistry, ToolHookContext

    hooks = HookRegistry()

    @hooks.on("post_tool_call")
    async def track_tool_usage(ctx: ToolHookContext):
        print(f"{ctx.tool_name} took {ctx.duration_ms:.0f}ms")

Hook callbacks may return action objects to influence runtime flow:

* :class:`BlockAction` from ``pre_tool_call`` aborts dispatch.
* :class:`RewriteAction` / :class:`SkipAction` from
  ``pre_gateway_dispatch`` rewrite or drop an inbound message.
* A bare ``str`` from ``transform_tool_result`` /
  ``transform_terminal_output`` replaces the original output.

The first matching action wins.  Non-matching returns are silently
ignored, so observer-only hooks coexist freely with action hooks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, get_args

logger = logging.getLogger(__name__)


# ── Event names ──────────────────────────────────────────────────

HookEvent = Literal[
    "pre_tool_call",
    "post_tool_call",
    "transform_tool_result",
    "transform_terminal_output",
    "pre_llm_call",
    "post_llm_call",
    "pre_api_request",
    "post_api_request",
    "on_session_start",
    "on_session_end",
    "on_session_finalize",
    "on_session_reset",
    "subagent_stop",
    "pre_gateway_dispatch",
]

VALID_EVENTS: set[str] = set(get_args(HookEvent))

HookFn = Callable[..., Any | Awaitable[Any]]


# ── Action return protocols ─────────────────────────────────────

@dataclass
class BlockAction:
    """Returned from ``pre_tool_call`` to abort dispatch."""
    message: str


@dataclass
class RewriteAction:
    """Returned from ``pre_gateway_dispatch`` to replace inbound text."""
    text: str


@dataclass
class SkipAction:
    """Returned from ``pre_gateway_dispatch`` to drop the message."""
    reason: str = ""


# ── Context dataclasses ─────────────────────────────────────────

@dataclass
class HookContext:
    """Base context shared by all events."""
    session_id: str = ""
    task_id: str = ""


@dataclass
class ToolHookContext(HookContext):
    """Context for tool lifecycle events.

    Used by ``pre_tool_call``, ``post_tool_call``,
    ``transform_tool_result``, and ``transform_terminal_output``.
    """
    tool_name: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""
    # Populated for post / transform events:
    result: str | None = None
    duration_ms: float = 0.0
    success: bool | None = None


@dataclass
class LLMHookContext(HookContext):
    """Context for LLM call lifecycle events.

    Used by ``pre_llm_call``, ``post_llm_call``, ``pre_api_request``,
    ``post_api_request``.
    """
    model: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    system: str = ""
    # User message extracted from messages tail (for context injection
    # in pre_llm_call returns):
    user_message: str = ""
    # Populated for post events:
    response: Any = None
    assistant_message: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    interrupted: bool = False


@dataclass
class SessionHookContext(HookContext):
    """Context for session lifecycle events."""
    model: str = ""
    platform: str = ""
    completed: bool = True
    interrupted: bool = False


@dataclass
class SubagentStopContext(HookContext):
    """Context for ``subagent_stop`` event."""
    subagent_id: str = ""
    reason: str = ""


@dataclass
class GatewayDispatchContext(HookContext):
    """Context for ``pre_gateway_dispatch`` event.

    Fired once per inbound :class:`InboundMessage` BEFORE auth/pairing
    and command parsing.  Plugins may return :class:`SkipAction` to
    drop the message or :class:`RewriteAction` to replace its text.
    """
    event: Any = None         # flowly.bus.queue.InboundMessage
    gateway: Any = None
    session_store: Any = None


# ── Registry ────────────────────────────────────────────────────

class HookRegistry:
    """Registry for lifecycle hooks.

    Both core code and plugins call :meth:`register` (or use the
    :meth:`on` decorator) to subscribe.  Callsites in agent runtime
    fire events via :meth:`fire`.

    Each callback runs inside its own ``try``/``except`` so a misbehaving
    hook never breaks the agent loop.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookFn]] = {}

    # ── Registration ────────────────────────────────────────────

    def register(self, event: str, fn: HookFn) -> HookFn:
        """Subscribe *fn* to *event*.  Returns *fn* unchanged so this
        can also be used as a decorator (``@hooks.register("foo")``).
        """
        if event not in VALID_EVENTS:
            logger.warning(
                "registering unknown hook event %r (valid: %s)",
                event, ", ".join(sorted(VALID_EVENTS)),
            )
        self._hooks.setdefault(event, []).append(fn)
        return fn

    def on(self, event: str) -> Callable[[HookFn], HookFn]:
        """Decorator sugar — ``@hooks.on("post_tool_call")``."""
        def decorator(fn: HookFn) -> HookFn:
            return self.register(event, fn)
        return decorator

    # ── Backward-compat decorators ──────────────────────────────

    def on_pre_tool(self, fn: HookFn) -> HookFn:
        """Deprecated — prefer ``register("pre_tool_call", fn)``."""
        return self.register("pre_tool_call", fn)

    def on_post_tool(self, fn: HookFn) -> HookFn:
        """Deprecated — prefer ``register("post_tool_call", fn)``."""
        return self.register("post_tool_call", fn)

    # ── Generic firing ──────────────────────────────────────────

    async def fire(self, event: str, ctx: HookContext) -> list[Any]:
        """Invoke all callbacks for *event*.  Returns non-``None``
        values in registration order.  Errors logged, never raised.
        """
        results: list[Any] = []
        for fn in self._hooks.get(event, []):
            try:
                ret = fn(ctx)
                if ret is not None and hasattr(ret, "__await__"):
                    ret = await ret
                if ret is not None:
                    results.append(ret)
            except Exception:
                logger.exception("hook %s callback failed", event)
        return results

    # ── Typed sugar — tool lifecycle ────────────────────────────

    async def fire_pre_tool(
        self, ctx: ToolHookContext
    ) -> BlockAction | None:
        """Fire ``pre_tool_call``; returns first :class:`BlockAction`."""
        for r in await self.fire("pre_tool_call", ctx):
            if isinstance(r, BlockAction):
                return r
        return None

    async def fire_post_tool(self, ctx: ToolHookContext) -> None:
        """Fire ``post_tool_call``.  Observational only."""
        await self.fire("post_tool_call", ctx)

    async def fire_transform_tool_result(
        self, ctx: ToolHookContext
    ) -> str | None:
        """Fire ``transform_tool_result``; first ``str`` return wins."""
        for r in await self.fire("transform_tool_result", ctx):
            if isinstance(r, str):
                return r
        return None

    async def fire_transform_terminal_output(
        self, ctx: ToolHookContext
    ) -> str | None:
        """Fire ``transform_terminal_output``; first ``str`` return wins."""
        for r in await self.fire("transform_terminal_output", ctx):
            if isinstance(r, str):
                return r
        return None

    # ── Typed sugar — LLM lifecycle ─────────────────────────────

    async def fire_pre_llm_call(
        self, ctx: LLMHookContext
    ) -> list[str]:
        """Fire ``pre_llm_call``; collect context strings to inject.

        Returned strings are appended to the user message (never the
        system prompt — preserves the prompt cache prefix).  Plugins
        return either a bare ``str`` or ``{"context": "..."}``.
        """
        contexts: list[str] = []
        for r in await self.fire("pre_llm_call", ctx):
            if isinstance(r, str):
                contexts.append(r)
            elif isinstance(r, dict) and "context" in r:
                contexts.append(str(r["context"]))
        return contexts

    async def fire_post_llm_call(self, ctx: LLMHookContext) -> None:
        """Fire ``post_llm_call``.  Observational only."""
        await self.fire("post_llm_call", ctx)

    async def fire_pre_api_request(self, ctx: LLMHookContext) -> None:
        """Fire ``pre_api_request``.  Observational only."""
        await self.fire("pre_api_request", ctx)

    async def fire_post_api_request(self, ctx: LLMHookContext) -> None:
        """Fire ``post_api_request``.  Observational only."""
        await self.fire("post_api_request", ctx)

    # ── Typed sugar — session lifecycle ─────────────────────────

    async def fire_session_start(self, ctx: SessionHookContext) -> None:
        await self.fire("on_session_start", ctx)

    async def fire_session_end(self, ctx: SessionHookContext) -> None:
        await self.fire("on_session_end", ctx)

    async def fire_session_finalize(self, ctx: SessionHookContext) -> None:
        await self.fire("on_session_finalize", ctx)

    async def fire_session_reset(self, ctx: SessionHookContext) -> None:
        await self.fire("on_session_reset", ctx)

    async def fire_subagent_stop(self, ctx: SubagentStopContext) -> None:
        await self.fire("subagent_stop", ctx)

    # ── Typed sugar — gateway dispatch ──────────────────────────

    async def fire_gateway_dispatch(
        self, ctx: GatewayDispatchContext
    ) -> SkipAction | RewriteAction | None:
        """Fire ``pre_gateway_dispatch``; first Skip or Rewrite wins."""
        for r in await self.fire("pre_gateway_dispatch", ctx):
            if isinstance(r, (SkipAction, RewriteAction)):
                return r
        return None
