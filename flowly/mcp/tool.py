"""Adapter that exposes an MCP server tool as a Flowly :class:`Tool`.

An :class:`MCPTool` instance wraps a single remote MCP tool (already
discovered via ``ClientSession.list_tools``) and exposes it through
Flowly's standard tool ABC so the agent loop can call it like any
built-in.

Execution path:

1. Agent loop awaits ``tool.execute(**params)``.
2. We submit a coroutine to the shared MCP background event loop via
   :func:`asyncio.run_coroutine_threadsafe`, then await the resulting
   ``concurrent.futures.Future`` from the agent's own event loop. This
   keeps the per-server anyio cancel-scopes alive on the MCP loop while
   integrating cleanly with Flowly's async tool registry.
3. The MCP loop calls ``session.call_tool``, collects content blocks
   into a string + structured-content envelope, and returns a JSON
   string the agent can hand back to the model.

Errors are returned as ``{"error": "..."}`` JSON strings rather than
raised — the agent loop already treats string-returns as the contract
and a raised exception inside an MCP call should not abort the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.mcp.schema import mcp_tool_name, normalize_mcp_input_schema
from flowly.mcp.security import sanitize_error


logger = logging.getLogger(__name__)


def _exc_text(exc: BaseException) -> str:
    """Return non-empty text for exceptions whose ``str`` is empty."""
    text = str(exc).strip()
    return text if text else repr(exc)


def _error_envelope(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


async def _run_on_mcp_loop(
    *,
    server_task: Any,
    tool_name: str,
    coro_factory: Any,
    timeout: float,
    on_interrupt: type[BaseException],
) -> str:
    """Schedule ``coro_factory(session)`` on the MCP loop, return its string.

    Handles loop/session presence checks, cross-thread scheduling,
    circuit-breaker accounting (success resets, failure bumps), interrupt
    translation, timeout, and credential-sanitized error envelopes.
    Shared by :class:`MCPTool` and the resource/prompt utility tools.
    """
    from flowly.mcp.client import (
        get_mcp_loop,
        _bump_server_error,
        _reset_server_error,
    )

    server_name = server_task.name

    loop = get_mcp_loop()
    if loop is None:
        return _error_envelope(
            f"MCP loop is not running (server '{server_name}')"
        )
    session = server_task.session
    if session is None:
        _bump_server_error(server_name)
        return _error_envelope(f"MCP server '{server_name}' is not connected")

    future = asyncio.run_coroutine_threadsafe(coro_factory(session), loop)
    try:
        result = await asyncio.wrap_future(future)
    except asyncio.TimeoutError:
        _bump_server_error(server_name)
        return _error_envelope(
            f"MCP tool '{tool_name}' timed out after {timeout:.0f}s"
        )
    except asyncio.CancelledError:
        # Caller is cancelling us; cancel the underlying MCP work too so
        # the server isn't left holding the call, then propagate.
        future.cancel()
        raise
    except on_interrupt:
        return _error_envelope("MCP call interrupted: user sent a new message")
    except Exception as exc:
        _bump_server_error(server_name)
        logger.error("MCP tool %s call failed: %s", tool_name, exc)
        return _error_envelope(
            sanitize_error(f"MCP call failed: {type(exc).__name__}: {_exc_text(exc)}")
        )

    # Success path: only OUR error envelope (exactly ``{"error": ...}``)
    # counts as a server-side failure for the breaker. A tool that
    # legitimately returns data containing an ``error`` key alongside
    # other fields is a healthy call and must not trip the breaker.
    try:
        parsed = json.loads(result)
        is_error_envelope = (
            isinstance(parsed, dict)
            and set(parsed.keys()) == {"error"}
        )
    except (json.JSONDecodeError, TypeError):
        is_error_envelope = False
    if is_error_envelope:
        _bump_server_error(server_name)
    else:
        _reset_server_error(server_name)
    return result


class MCPTool(Tool):
    """A Flowly ``Tool`` backed by a single MCP server tool.

    The wrapper holds a reference to the owning :class:`MCPServerTask`
    rather than the raw ``ClientSession`` so that reconnects (Faz 2)
    swap out the session transparently without re-registering tools.
    """

    def __init__(
        self,
        *,
        server_task: Any,  # MCPServerTask — forward declaration to avoid cycle
        remote_tool: Any,  # mcp.types.Tool
    ) -> None:
        self._server_task = server_task
        self._server_name = server_task.name
        self._remote_name = remote_tool.name
        self._tool_name = mcp_tool_name(server_task.name, remote_tool.name)
        self._description = (
            remote_tool.description
            or f"MCP tool {remote_tool.name} from server '{server_task.name}'"
        )
        self._parameters = normalize_mcp_input_schema(
            getattr(remote_tool, "inputSchema", None)
        )

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        from flowly.mcp.client import (
            MCPCallInterrupted,
            circuit_breaker_block_reason,
        )

        # Circuit breaker (T10): short-circuit a server that has failed
        # repeatedly so the model stops hammering it.
        blocked = circuit_breaker_block_reason(self._server_name)
        if blocked is not None:
            return json.dumps({"error": blocked}, ensure_ascii=False)

        async def _call(session: Any) -> str:
            timeout = self._server_task.tool_timeout
            async with self._server_task.rpc_lock:
                result = await asyncio.wait_for(
                    session.call_tool(self._remote_name, arguments=kwargs),
                    timeout=timeout,
                )
            return self._format_result(result)

        return await _run_on_mcp_loop(
            server_task=self._server_task,
            tool_name=self._tool_name,
            coro_factory=_call,
            timeout=self._server_task.tool_timeout,
            on_interrupt=MCPCallInterrupted,
        )

    def _format_result(self, result: Any) -> str:
        """Render an MCP ``CallToolResult`` into the agent's JSON envelope."""
        is_error = getattr(result, "isError", False)
        content_blocks = getattr(result, "content", None) or []

        if is_error:
            text = ""
            for block in content_blocks:
                block_text = getattr(block, "text", None)
                if block_text:
                    text += block_text
            return json.dumps({
                "error": sanitize_error(text or "MCP tool returned an error"),
            }, ensure_ascii=False)

        from flowly.mcp.media_cache import cache_image_block

        text_parts: list[str] = []
        for block in content_blocks:
            block_text = getattr(block, "text", None)
            if block_text:
                text_parts.append(block_text)
                continue
            # ImageContent → cache to $FLOWLY_HOME/media/mcp/ and emit a
            # MEDIA: token so messaging adapters render it (E3).
            media_tag = cache_image_block(block)
            if media_tag:
                text_parts.append(media_tag)

        text_result = "\n".join(text_parts)

        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            envelope: dict[str, Any] = {"result": text_result or structured}
            if text_result and structured:
                envelope = {
                    "result": text_result,
                    "structuredContent": structured,
                }
            return json.dumps(envelope, ensure_ascii=False, default=str)
        return json.dumps({"result": text_result}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Resource / prompt utility tools (D9)
# ---------------------------------------------------------------------------
#
# MCP servers can expose Resources (readable URIs) and Prompts (named
# prompt templates) alongside their tools. We surface each capability as
# a small fixed tool so the model can browse and fetch them. These only
# register when the server advertises the capability AND the user opts in
# via ``tools.resources`` / ``tools.prompts`` in config — see
# ``flowly.mcp.client._utility_tools_for_server``.


class _MCPUtilityTool(Tool):
    """Base for the four resource/prompt utility tools."""

    _suffix = ""

    def __init__(self, *, server_task: Any) -> None:
        self._server_task = server_task
        self._server_name = server_task.name
        self._tool_name = mcp_tool_name(server_task.name, self._suffix)

    @property
    def name(self) -> str:
        return self._tool_name

    async def _run(self, coro_factory: Any) -> str:
        from flowly.mcp.client import MCPCallInterrupted
        return await _run_on_mcp_loop(
            server_task=self._server_task,
            tool_name=self._tool_name,
            coro_factory=coro_factory,
            timeout=self._server_task.tool_timeout,
            on_interrupt=MCPCallInterrupted,
        )


class MCPListResourcesTool(_MCPUtilityTool):
    _suffix = "list_resources"

    @property
    def description(self) -> str:
        return f"List the resources exposed by MCP server '{self._server_name}'."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        async def _call(session: Any) -> str:
            async with self._server_task.rpc_lock:
                result = await session.list_resources()
            resources = []
            for r in getattr(result, "resources", []) or []:
                entry: dict[str, Any] = {}
                if getattr(r, "uri", None) is not None:
                    entry["uri"] = str(r.uri)
                if getattr(r, "name", None):
                    entry["name"] = r.name
                if getattr(r, "description", None):
                    entry["description"] = r.description
                if getattr(r, "mimeType", None):
                    entry["mimeType"] = r.mimeType
                resources.append(entry)
            return json.dumps({"resources": resources}, ensure_ascii=False, default=str)

        return await self._run(_call)


class MCPReadResourceTool(_MCPUtilityTool):
    _suffix = "read_resource"

    @property
    def description(self) -> str:
        return f"Read a resource by URI from MCP server '{self._server_name}'."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "URI of the resource to read"},
            },
            "required": ["uri"],
        }

    async def execute(self, uri: str = "", **kwargs: Any) -> str:
        if not uri:
            return _error_envelope("Missing required parameter 'uri'")

        async def _call(session: Any) -> str:
            async with self._server_task.rpc_lock:
                result = await session.read_resource(uri)
            parts: list[str] = []
            for block in getattr(result, "contents", []) or []:
                block_text = getattr(block, "text", None)
                if block_text:
                    parts.append(block_text)
                elif getattr(block, "blob", None) is not None:
                    parts.append(f"[binary data, {len(block.blob)} bytes]")
            return json.dumps({"result": "\n".join(parts)}, ensure_ascii=False, default=str)

        return await self._run(_call)


class MCPListPromptsTool(_MCPUtilityTool):
    _suffix = "list_prompts"

    @property
    def description(self) -> str:
        return f"List the prompts exposed by MCP server '{self._server_name}'."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        async def _call(session: Any) -> str:
            async with self._server_task.rpc_lock:
                result = await session.list_prompts()
            prompts = []
            for p in getattr(result, "prompts", []) or []:
                entry: dict[str, Any] = {}
                if getattr(p, "name", None):
                    entry["name"] = p.name
                if getattr(p, "description", None):
                    entry["description"] = p.description
                args = getattr(p, "arguments", None)
                if args:
                    entry["arguments"] = [
                        {
                            "name": getattr(a, "name", ""),
                            **({"description": a.description}
                               if getattr(a, "description", None) else {}),
                            **({"required": a.required}
                               if getattr(a, "required", None) is not None else {}),
                        }
                        for a in args
                    ]
                prompts.append(entry)
            return json.dumps({"prompts": prompts}, ensure_ascii=False, default=str)

        return await self._run(_call)


class MCPGetPromptTool(_MCPUtilityTool):
    _suffix = "get_prompt"

    @property
    def description(self) -> str:
        return f"Get a prompt by name from MCP server '{self._server_name}'."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the prompt"},
                "arguments": {
                    "type": "object",
                    "description": "Optional prompt arguments",
                    "properties": {},
                    "additionalProperties": True,
                },
            },
            "required": ["name"],
        }

    async def execute(self, name: str = "", arguments: dict | None = None, **kwargs: Any) -> str:
        if not name:
            return _error_envelope("Missing required parameter 'name'")

        async def _call(session: Any) -> str:
            async with self._server_task.rpc_lock:
                result = await session.get_prompt(name, arguments=arguments or {})
            messages = []
            for m in getattr(result, "messages", []) or []:
                role = getattr(m, "role", "")
                content = getattr(m, "content", None)
                text = getattr(content, "text", None)
                messages.append({"role": role, "content": text if text else str(content)})
            return json.dumps({
                "description": getattr(result, "description", None),
                "messages": messages,
            }, ensure_ascii=False, default=str)

        return await self._run(_call)
