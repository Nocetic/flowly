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
from contextlib import AsyncExitStack
from typing import Any

from flowly.agent.tools.base import Tool
from flowly.mcp.content import (
    mcp_attr,
    mcp_wire_value,
    render_content_blocks,
    render_resource_contents,
)
from flowly.mcp.lifecycle import MCPContractChangedError, MCPUnavailableError
from flowly.mcp.media_cache import DEFAULT_MAX_BINARY_BYTES
from flowly.mcp.pagination import collect_mcp_pages
from flowly.mcp.requests import call_with_input
from flowly.mcp.schema import mcp_tool_name, normalize_mcp_input_schema
from flowly.mcp.security import diagnostic_secrets, exception_diagnostic, sanitize_error

logger = logging.getLogger(__name__)


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
        _bump_server_error,
        _release_server_probe,
        _reset_server_error,
        get_mcp_loop,
    )

    server_name = server_task.name

    loop = get_mcp_loop()
    if loop is None:
        _bump_server_error(server_name)
        return _error_envelope(
            f"MCP loop is not running (server '{server_name}')"
        )
    session = server_task.session
    lease = getattr(server_task, "connection_lease", None)
    if session is None and not callable(lease):
        _bump_server_error(server_name)
        return _error_envelope(f"MCP server '{server_name}' is not connected")

    from flowly.agent.tool_context import current_tool_origin

    origin = current_tool_origin()

    async def invoke():
        nonlocal session
        async with AsyncExitStack() as stack:
            if callable(lease):
                session = await stack.enter_async_context(lease())
            get_interaction = getattr(server_task, "get_interaction", None)
            if callable(get_interaction):
                await stack.enter_async_context(get_interaction().invocation(session, origin))
            return await coro_factory(session)

    future = asyncio.run_coroutine_threadsafe(invoke(), loop)
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
        _release_server_probe(server_name)
        raise
    except on_interrupt:
        _release_server_probe(server_name)
        return _error_envelope("MCP call interrupted: user sent a new message")
    except (MCPContractChangedError, MCPUnavailableError) as exc:
        # Local admission/contract rejection is not a failed remote operation.
        _release_server_probe(server_name)
        return _error_envelope(sanitize_error(str(exc)))
    except Exception as exc:
        # A failed request is often the first signal that a long-idle stream
        # has died. Wake the connection supervisor immediately instead of
        # waiting for the next keepalive. Application/protocol errors are
        # deliberately excluded so a bad argument never churns the session.
        from flowly.mcp.lifecycle import is_transport_failure

        if is_transport_failure(exc):
            loop.call_soon_threadsafe(
                server_task.report_transport_failure,
                exc,
                session,
            )
        _bump_server_error(server_name)
        detail = exception_diagnostic(exc, secrets=diagnostic_secrets(getattr(server_task, "_config", None)))
        logger.error("MCP tool %s call failed: %s", sanitize_error(tool_name, limit=200), detail)
        return _error_envelope(
            sanitize_error(f"MCP call failed: {type(exc).__name__}: {detail}")
        )

    # Success path: only OUR error envelope (exactly ``{"error": ...}``)
    # counts as a server-side failure for the breaker. A tool that
    # legitimately returns data containing an ``error`` key alongside
    # other fields is a healthy call and must not trip the breaker.
    try:
        parsed = json.loads(result)
        is_error_envelope = isinstance(parsed, dict) and (
            set(parsed.keys()) == {"error"} or parsed.get("isError") is True
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
            mcp_attr(remote_tool, "input_schema", "inputSchema")
        )
        self._raw_parameters = mcp_wire_value(mcp_attr(remote_tool, "input_schema", "inputSchema"))
        self.title = getattr(remote_tool, "title", None)
        self.output_schema = mcp_wire_value(mcp_attr(remote_tool, "output_schema", "outputSchema"))
        self.annotations = mcp_wire_value(getattr(remote_tool, "annotations", None))
        self.execution = mcp_wire_value(getattr(remote_tool, "execution", None))
        self.icons = mcp_wire_value(getattr(remote_tool, "icons", None))
        self.mcp_metadata = mcp_wire_value(mcp_attr(remote_tool, "meta", "_meta"))

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def toolset(self) -> str:
        return "mcp"

    @property
    def discovery_source(self) -> str:
        return self._server_name

    def contract_fingerprint(self) -> str:
        """Include execution/permission metadata, not just the model schema."""
        return json.dumps({
            "remoteName": self._remote_name,
            "schema": self.to_schema(),
            "remoteInputSchema": self._raw_parameters,
            "title": self.title,
            "outputSchema": self.output_schema,
            "annotations": self.annotations,
            "execution": self.execution,
            "icons": self.icons,
            "meta": self.mcp_metadata,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)

    async def execute(self, **kwargs: Any) -> str:
        from flowly.mcp.client import (
            MCPCallInterrupted,
            circuit_breaker_block_reason,
        )

        # Consent precedes all transport work, including waking idle servers.
        config = getattr(self._server_task, "_config", {})
        trust = config.get("trust", "full")
        if trust not in {"full", "untrusted"}:
            return _error_envelope("Invalid MCP server trust policy")
        if trust == "untrusted":
            approved = await self._server_task.get_interaction().authorize(
                self._remote_name, self.annotations, kwargs,
            )
            if not approved:
                return _error_envelope("MCP write-capable tool was not approved by the calling user")

        # Circuit breaker (T10): short-circuit a server that has failed
        # repeatedly so the model stops hammering it.
        blocked = circuit_breaker_block_reason(self._server_name)
        if blocked is not None:
            return json.dumps({"error": blocked}, ensure_ascii=False)

        async def _call(session: Any) -> str:
            timeout = self._server_task.tool_timeout
            slot = getattr(self._server_task, "tool_call_slot", None)
            guard = slot() if callable(slot) else self._server_task.rpc_lock
            async with guard:
                validate = getattr(self._server_task, "validate_tool_contract", None)
                if callable(validate):
                    validate(self._remote_name, self.contract_fingerprint())
                result = await asyncio.wait_for(
                    call_with_input(session, "call_tool", self._remote_name, arguments=kwargs),
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
        is_error = bool(mcp_attr(result, "is_error", "isError", False))
        if is_error:
            # Inspect bounded text only, before the success renderer can decode
            # or cache remote binary attachments. Rich/structured error payloads
            # and metadata may contain credentials too; never relay them raw.
            chunks = []
            size = 0
            for index, block in enumerate(getattr(result, "content", None) or []):
                if index >= 64:
                    chunks = ["MCP error content exceeds the diagnostic limit"]
                    break
                text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
                if isinstance(text, str):
                    size += len(text)
                    if size > 64 * 1024:
                        chunks = ["MCP error content exceeds the diagnostic limit"]
                        break
                    chunks.append(text)
            message = sanitize_error(
                "\n".join(chunks) or "MCP tool returned an error",
                secrets=diagnostic_secrets(getattr(self._server_task, "_config", None)),
            )
            if getattr(self, "_bridge_native_result", False):
                return json.dumps({"content": [{"type": "text", "text": message}], "isError": True}, ensure_ascii=False)
            return json.dumps({"error": message, "isError": True}, ensure_ascii=False)
        if getattr(self, "_bridge_native_result", False):
            # Only a context-bound runtime copy sets this flag. Returning the
            # wire JSON still lets ordinary registry hooks block/transform it.
            return json.dumps(mcp_wire_value(result), ensure_ascii=False)
        content_blocks = getattr(result, "content", None) or []
        rendered = render_content_blocks(
            content_blocks,
            max_binary_bytes=getattr(
                self._server_task,
                "max_binary_bytes",
                DEFAULT_MAX_BINARY_BYTES,
            ),
        )

        structured = mcp_attr(result, "structured_content", "structuredContent")
        envelope = {"result": rendered.text or structured or ""}
        if structured is not None and rendered.text:
            envelope["structuredContent"] = structured
        if rendered.rich:
            envelope["content"] = list(rendered.blocks)
        meta = mcp_attr(result, "meta", "_meta")
        if meta is not None:
            envelope["_meta"] = mcp_wire_value(meta)
        result_type = mcp_attr(result, "result_type", "resultType", "complete")
        if result_type and result_type != "complete":
            envelope["resultType"] = str(result_type)
        return json.dumps(envelope, ensure_ascii=False, default=str)


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

    @property
    def toolset(self) -> str:
        return "mcp"

    @property
    def discovery_source(self) -> str:
        return self._server_name

    async def _run(self, coro_factory: Any) -> str:
        from flowly.mcp.client import MCPCallInterrupted

        async def checked(session):
            from flowly.mcp.client import _capability_advertised

            family = "resources" if "resource" in self._suffix else "prompts"
            cfg = getattr(self._server_task, "_config", None)
            if isinstance(cfg, dict) and (
                not (cfg.get("tools") or {}).get(family)
                or not _capability_advertised(self._server_task, family)
            ):
                raise MCPContractChangedError(
                    f"MCP {family} capability changed or is no longer enabled; "
                    "refresh the tool list. No operation was sent."
                )
            return await coro_factory(session)

        return await _run_on_mcp_loop(
            server_task=self._server_task,
            tool_name=self._tool_name,
            coro_factory=checked,
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
        return {
            "type": "object",
            "properties": {
                "cursor": {
                    "type": "string",
                    "description": "Optional MCP cursor to resume listing from",
                },
            },
        }

    async def execute(self, cursor: str = "", **kwargs: Any) -> str:
        async def _call(session: Any) -> str:
            async def _fetch(page_cursor: str | None) -> Any:
                if page_cursor is None:
                    return await session.list_resources()
                return await session.list_resources(cursor=page_cursor)

            async with self._server_task.rpc_lock:
                pages = await collect_mcp_pages(
                    _fetch,
                    "resources",
                    initial_cursor=cursor or None,
                    max_pages=getattr(self._server_task, "pagination_max_pages", 100),
                    max_items=getattr(self._server_task, "pagination_max_items", 10_000),
                )
            return json.dumps({
                "resources": [mcp_wire_value(resource) for resource in pages.items],
                "pagination": pages.metadata(),
            }, ensure_ascii=False, default=str)

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
                result = await call_with_input(session, "read_resource", uri)
            rendered = render_resource_contents(
                getattr(result, "contents", None) or [],
                max_binary_bytes=getattr(
                    self._server_task,
                    "max_binary_bytes",
                    DEFAULT_MAX_BINARY_BYTES,
                ),
            )
            envelope: dict[str, Any] = {
                "result": rendered.text,
                "contents": list(rendered.blocks),
            }
            for snake, wire in (
                ("meta", "_meta"),
                ("ttl_ms", "ttlMs"),
                ("cache_scope", "cacheScope"),
                ("result_type", "resultType"),
            ):
                value = mcp_attr(result, snake, wire)
                if value is not None:
                    envelope[wire] = mcp_wire_value(value)
            return json.dumps(envelope, ensure_ascii=False, default=str)

        return await self._run(_call)


class MCPListPromptsTool(_MCPUtilityTool):
    _suffix = "list_prompts"

    @property
    def description(self) -> str:
        return f"List the prompts exposed by MCP server '{self._server_name}'."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "cursor": {
                    "type": "string",
                    "description": "Optional MCP cursor to resume listing from",
                },
            },
        }

    async def execute(self, cursor: str = "", **kwargs: Any) -> str:
        async def _call(session: Any) -> str:
            async def _fetch(page_cursor: str | None) -> Any:
                if page_cursor is None:
                    return await session.list_prompts()
                return await session.list_prompts(cursor=page_cursor)

            async with self._server_task.rpc_lock:
                pages = await collect_mcp_pages(
                    _fetch,
                    "prompts",
                    initial_cursor=cursor or None,
                    max_pages=getattr(self._server_task, "pagination_max_pages", 100),
                    max_items=getattr(self._server_task, "pagination_max_items", 10_000),
                )
            return json.dumps({
                "prompts": [mcp_wire_value(prompt) for prompt in pages.items],
                "pagination": pages.metadata(),
            }, ensure_ascii=False, default=str)

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
                result = await call_with_input(session, "get_prompt", name, arguments=arguments or {})
            messages = []
            for m in getattr(result, "messages", []) or []:
                role = getattr(m, "role", "")
                content = getattr(m, "content", None)
                rendered = render_content_blocks(
                    [content] if content is not None else [],
                    max_binary_bytes=getattr(
                        self._server_task,
                        "max_binary_bytes",
                        DEFAULT_MAX_BINARY_BYTES,
                    ),
                )
                messages.append({
                    "role": role,
                    "text": rendered.text,
                    "content": (
                        rendered.blocks[0]
                        if len(rendered.blocks) == 1
                        else list(rendered.blocks)
                    ),
                })
            envelope = {
                "description": getattr(result, "description", None),
                "messages": messages,
            }
            meta = mcp_attr(result, "meta", "_meta")
            if meta is not None:
                envelope["_meta"] = mcp_wire_value(meta)
            result_type = mcp_attr(result, "result_type", "resultType", "complete")
            if result_type and result_type != "complete":
                envelope["resultType"] = str(result_type)
            return json.dumps(envelope, ensure_ascii=False, default=str)

        return await self._run(_call)
