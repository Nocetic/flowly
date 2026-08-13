"""Tool adapter — wraps a function-based tool registration as a Flowly
:class:`Tool` ABC subclass.

Plugins register tools via ``ctx.register_tool(name, schema, handler,
check_fn=, ...)`` — handler is a sync or async callable, schema is a
JSON Schema dict.  Flowly's core uses class-based tools (one ``Tool``
subclass per tool).  This adapter bridges the two so plugin authors
can write the simpler function-based form and Flowly's
:class:`flowly.agent.tools.registry.ToolRegistry` keeps its uniform
interface.

The adapter applies *check_fn* lazily at dispatch time (not register
time) so a plugin can register a tool that needs runtime config (an OAuth
token, an env var) without crashing during plugin load.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from flowly.agent.tools.base import Tool
from flowly.agent.tools.routing import ALL_BUILTIN_TOOLSETS


class FunctionToolAdapter(Tool):
    """Wraps a function-based tool registration as a Flowly :class:`Tool`."""

    def __init__(
        self,
        *,
        name: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[[], bool] | None = None,
        description: str = "",
        discovery_source: str = "",
        toolset: str = "",
    ) -> None:
        self._name = name
        self._handler = handler
        self._check_fn = check_fn
        # Description precedence: explicit arg > schema.description > "".
        self._description = description or str(schema.get("description") or "")
        self._discovery_source = str(discovery_source or "").strip()
        declared_toolset = str(toolset or "extensions").strip().lower()
        self._toolset = (
            declared_toolset
            if declared_toolset in ALL_BUILTIN_TOOLSETS
            else "extensions"
        )
        # Some manifests pass the full OpenAI function schema
        # (``{"type":"function","function":{"parameters":...}}``) and
        # others just the parameters block.  Accept both.
        self._parameters = self._extract_parameters(schema)

    @staticmethod
    def _extract_parameters(schema: dict[str, Any]) -> dict[str, Any]:
        if "parameters" in schema:
            params = schema["parameters"]
            if isinstance(params, dict):
                return params
        if "function" in schema:
            fn = schema["function"]
            if isinstance(fn, dict) and isinstance(fn.get("parameters"), dict):
                return fn["parameters"]
        # Schema itself looks like a JSON Schema (has ``type`` or
        # ``properties``) — use as-is.
        if "properties" in schema or schema.get("type") == "object":
            return schema
        return {"type": "object", "properties": {}}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def toolset(self) -> str:
        """Use the trusted plugin declaration, defaulting to deferred."""
        return self._toolset

    @property
    def discovery_source(self) -> str | None:
        return self._discovery_source or None

    def is_available(self) -> bool:
        """Hide plugin tools whose runtime capability check is currently false."""
        if self._check_fn is None:
            return True
        return bool(self._check_fn())

    async def execute(self, **kwargs: Any) -> str:
        # Dispatch-time gating: if the plugin declared a check_fn (e.g.
        # "is the user authenticated?") and it returns False, surface a
        # clear error instead of calling the handler.
        if self._check_fn is not None:
            try:
                ok = self._check_fn()
            except Exception as exc:
                return f"Error: {self._name} availability check failed: {exc}"
            if not ok:
                return (
                    f"Error: {self._name} is unavailable "
                    f"(plugin check_fn returned False)"
                )

        try:
            result = self._handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return f"Error executing {self._name}: {exc}"

        if result is None:
            return ""
        return str(result)
