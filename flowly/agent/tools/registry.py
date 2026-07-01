"""Tool registry for dynamic tool management."""

from __future__ import annotations

import inspect
import json
import time
from typing import Any, TYPE_CHECKING

from loguru import logger

from flowly.agent.tools.base import Tool

if TYPE_CHECKING:
    from flowly.agent.hooks import HookRegistry


def _unwrap_raw_envelope(params: Any) -> Any:
    """Unwrap a ``{"raw": "<json object string>"}`` argument envelope.

    Some models (observed with deepseek on large-payload calls) emit the entire
    tool-arguments object as a single JSON STRING under a ``raw`` key instead of
    as structured fields — e.g. ``{"raw": "{\\"action\\":\\"create\\",...}"}``.
    Validation then can't see ``action``/``type``/… and the call fails on
    "Missing required parameter(s)", looping forever. Detect the sole-``raw``
    JSON-object envelope and flatten it back to real params. Conservative: only
    fires when ``raw`` is the ONLY key and parses to a dict, so a tool with a
    genuine ``raw`` field (alongside others) is untouched.
    """
    if (
        isinstance(params, dict)
        and set(params.keys()) == {"raw"}
        and isinstance(params["raw"], str)
    ):
        try:
            parsed = json.loads(params["raw"])
        except (ValueError, TypeError):
            return params
        if isinstance(parsed, dict):
            return parsed
    return params


def _drop_unexpected_kwargs(tool: Tool, params: dict[str, Any]) -> dict[str, Any]:
    """Strip kwargs the tool's ``execute`` signature can't accept.

    Models occasionally invent an argument that isn't in the tool's JSON
    schema (e.g. a ``count`` on ``x_search``). Passed straight through as
    ``**params`` that raises ``TypeError: execute() got an unexpected keyword
    argument`` — which the dispatcher turns into an opaque "Error executing
    <tool>" and the call is wasted. Keep only the parameters ``execute``
    actually names; if it declares ``**kwargs`` (VAR_KEYWORD) pass everything
    through untouched. ``required`` params are already guaranteed present by
    ``validate_tool_call``, so this can only drop non-schema extras.
    """
    try:
        sig = inspect.signature(tool.execute)
    except (TypeError, ValueError):
        return params
    accepts_var_keyword = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_var_keyword:
        return params
    allowed = {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    dropped = [k for k in params if k not in allowed]
    if dropped:
        logger.debug(
            "Dropping unexpected arg(s) {} not accepted by {}.execute",
            dropped,
            type(tool).__name__,
        )
    return {k: v for k, v in params.items() if k in allowed}


def _extract_enum_values(schema: Any) -> list[Any] | None:
    """Extract enum-like values from a JSON schema fragment."""
    if not isinstance(schema, dict):
        return None
    if isinstance(schema.get("enum"), list):
        return list(schema["enum"])
    if "const" in schema:
        return [schema["const"]]
    variants = None
    if isinstance(schema.get("anyOf"), list):
        variants = schema["anyOf"]
    elif isinstance(schema.get("oneOf"), list):
        variants = schema["oneOf"]
    elif isinstance(schema.get("allOf"), list):
        variants = schema["allOf"]
    if not variants:
        return None
    values: list[Any] = []
    for variant in variants:
        extracted = _extract_enum_values(variant)
        if extracted:
            values.extend(extracted)
    return values or None


def _merge_property_schema(existing: Any, incoming: Any) -> Any:
    """Merge two property schema fragments conservatively."""
    if existing is None:
        return incoming
    if incoming is None:
        return existing

    existing_enum = _extract_enum_values(existing)
    incoming_enum = _extract_enum_values(incoming)
    if existing_enum or incoming_enum:
        values = []
        seen = set()
        for value in [*(existing_enum or []), *(incoming_enum or [])]:
            key = repr(value)
            if key in seen:
                continue
            seen.add(key)
            values.append(value)

        merged: dict[str, Any] = {}
        for source in (existing, incoming):
            if isinstance(source, dict):
                for key in ("title", "description", "default"):
                    if key not in merged and key in source:
                        merged[key] = source[key]
        if values:
            merged["enum"] = values
        return merged

    return existing


def _normalize_tool_parameters_schema(parameters: Any) -> dict[str, Any]:
    """
    Normalize tool schemas for provider compatibility.

    Some providers reject top-level oneOf/anyOf/allOf in tool input schema.
    We flatten top-level unions into a single object schema.
    """
    if not isinstance(parameters, dict):
        return {"type": "object", "properties": {}, "additionalProperties": True}

    has_top_union = any(
        isinstance(parameters.get(key), list)
        for key in ("anyOf", "oneOf", "allOf")
    )

    if not has_top_union:
        # Ensure top-level object shape for function tools.
        if "type" not in parameters and (
            isinstance(parameters.get("properties"), dict)
            or isinstance(parameters.get("required"), list)
        ):
            patched = dict(parameters)
            patched["type"] = "object"
            return patched
        return parameters

    variants: list[Any] = []
    for key in ("anyOf", "oneOf", "allOf"):
        raw = parameters.get(key)
        if isinstance(raw, list):
            variants.extend(raw)

    merged_properties: dict[str, Any] = {}
    required_counts: dict[str, int] = {}
    object_variants = 0

    for variant in variants:
        if not isinstance(variant, dict):
            continue
        props = variant.get("properties")
        if not isinstance(props, dict):
            continue
        object_variants += 1
        for prop_key, prop_schema in props.items():
            if prop_key not in merged_properties:
                merged_properties[prop_key] = prop_schema
            else:
                merged_properties[prop_key] = _merge_property_schema(
                    merged_properties[prop_key],
                    prop_schema,
                )

        required = variant.get("required")
        if isinstance(required, list):
            for req_key in required:
                if isinstance(req_key, str):
                    required_counts[req_key] = required_counts.get(req_key, 0) + 1

    base_required = parameters.get("required")
    merged_required: list[str] | None = None
    if isinstance(base_required, list):
        merged_required = [key for key in base_required if isinstance(key, str)]
    elif object_variants > 0:
        merged_required = [
            key for key, count in required_counts.items()
            if count == object_variants
        ]

    normalized: dict[str, Any] = {
        "type": "object",
        "properties": merged_properties if merged_properties else parameters.get("properties", {}),
        "additionalProperties": parameters.get("additionalProperties", True),
    }
    if isinstance(parameters.get("title"), str):
        normalized["title"] = parameters["title"]
    if isinstance(parameters.get("description"), str):
        normalized["description"] = parameters["description"]
    if merged_required:
        normalized["required"] = merged_required

    return normalized


class ToolRegistry:
    """
    Registry for agent tools.
    
    Allows dynamic registration and execution of tools.
    """
    
    def __init__(self, hooks: HookRegistry | None = None):
        self._tools: dict[str, Tool] = {}
        self._hooks = hooks
        # Caller (AgentLoop) sets this for the duration of a turn so
        # ToolHookContext.session_id is populated for plugin hooks that
        # need to correlate tool calls with the owning session
        # (e.g. disk-cleanup's per-session tracker).
        self._active_session_id: str = ""

    def set_active_session(self, session_id: str) -> None:
        """Bind the current session id to subsequent ``execute()`` calls."""
        self._active_session_id = session_id or ""

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
    
    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
    
    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)
    
    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools
    
    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        # Snapshot the values first: MCP discovery registers tools from a
        # background thread (so a slow server can't delay boot), and iterating
        # the live dict here could otherwise raise "dictionary changed size
        # during iteration". A tool simply appears in this turn's list or the
        # next one.
        definitions = [tool.to_schema() for tool in list(self._tools.values())]
        normalized: list[dict[str, Any]] = []
        for definition in definitions:
            fn = definition.get("function")
            if isinstance(fn, dict):
                fn = dict(fn)
                fn["parameters"] = _normalize_tool_parameters_schema(fn.get("parameters"))
                definition = dict(definition)
                definition["function"] = fn
            normalized.append(definition)
        return normalized

    def validate_tool_call(self, name: str, params: dict[str, Any]) -> str | None:
        """Validate required params against normalized schema before execution."""
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"

        if not isinstance(params, dict):
            return f"Error: Invalid parameters for tool '{name}'"

        schema = _normalize_tool_parameters_schema(tool.parameters)
        required = schema.get("required")
        if not isinstance(required, list):
            return None

        missing: list[str] = []
        for key in required:
            if not isinstance(key, str):
                continue
            if key not in params:
                missing.append(key)
                continue
            value = params.get(key)
            if value is None:
                missing.append(key)
                continue
            if isinstance(value, str) and not value.strip():
                missing.append(key)

        if missing:
            joined = ", ".join(sorted(set(missing)))
            return f"Error: Missing required parameter(s) for '{name}': {joined}"
        return None
    
    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """
        Execute a tool by name with given parameters.

        Args:
            name: Tool name.
            params: Tool parameters.

        Returns:
            Tool execution result as string.

        Raises:
            KeyError: If tool not found.
        """
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"

        params = _unwrap_raw_envelope(params)
        validation_error = self.validate_tool_call(name, params)
        if validation_error:
            return validation_error

        # Fire pre-tool hooks; a plugin may abort dispatch by returning
        # a BlockAction (e.g. policy enforcement, rate limiting).
        ctx = None
        if self._hooks:
            from flowly.agent.hooks import ToolHookContext
            ctx = ToolHookContext(
                tool_name=name,
                params=params,
                session_id=self._active_session_id,
            )
            block = await self._hooks.fire_pre_tool(ctx)
            if block is not None:
                return f"[blocked: {block.message}]"

        t0 = time.monotonic()
        try:
            result = await tool.execute(**_drop_unexpected_kwargs(tool, params))
        except Exception as e:
            result = f"Error executing {name}: {str(e)}"

        # Fire post-tool hooks then transform_tool_result; a plugin
        # may rewrite the result string entirely.
        if self._hooks and ctx is not None:
            ctx.result = result
            ctx.duration_ms = (time.monotonic() - t0) * 1000
            ctx.success = not result.startswith("Error")
            await self._hooks.fire_post_tool(ctx)
            transformed = await self._hooks.fire_transform_tool_result(ctx)
            if transformed is not None:
                result = transformed

        return result
    
    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())
    
    def __len__(self) -> int:
        return len(self._tools)
    
    def __contains__(self, name: str) -> bool:
        return name in self._tools
