"""Tool registry for dynamic tool management."""

from __future__ import annotations

import inspect
import json
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.agent.tools.routing import infer_toolset

if TYPE_CHECKING:
    from flowly.agent.hooks import HookRegistry


@dataclass(frozen=True)
class ToolAvailabilityContext:
    """Runtime facts available to a synchronous tool capability check."""

    platform: str = ""


AvailabilityCheck = Callable[[ToolAvailabilityContext], bool]
RegistryChangeCallback = Callable[[int], None]


@dataclass(frozen=True)
class _ToolRegistration:
    toolset: str
    platforms: frozenset[str] | None
    check_fn: AvailabilityCheck | None


@dataclass(frozen=True)
class ToolCatalogSnapshot:
    """One generation-consistent snapshot of static registry metadata.

    This deliberately ignores per-platform availability.  Consumers such as
    the local semantic index may precompute metadata for every registered
    tool, while the live route remains the authority over which names can be
    disclosed or executed.
    """

    generation: int
    definitions: tuple[dict[str, Any], ...]
    toolsets: dict[str, str]
    sources: dict[str, str]


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


def _normalized_tool_definition(tool: Tool) -> dict[str, Any]:
    definition = tool.to_schema()
    fn = definition.get("function")
    if isinstance(fn, dict):
        normalized_fn = dict(fn)
        normalized_fn["parameters"] = _normalize_tool_parameters_schema(
            normalized_fn.get("parameters")
        )
        definition = dict(definition)
        definition["function"] = normalized_fn
    return definition


class ToolRegistry:
    """
    Registry for agent tools.
    
    Allows dynamic registration and execution of tools.
    """
    
    def __init__(
        self,
        hooks: HookRegistry | None = None,
        *,
        availability_cache_ttl: float = 30.0,
        availability_failure_grace: float = 60.0,
        schema_cache_ttl: float = 10.0,
    ):
        self._tools: dict[str, Tool] = {}
        self._registrations: dict[str, _ToolRegistration] = {}
        self._availability_cache: dict[tuple[str, str], tuple[float, bool]] = {}
        self._availability_last_good: dict[tuple[str, str], float] = {}
        self._availability_cache_ttl = max(0.0, float(availability_cache_ttl))
        self._availability_failure_grace = max(0.0, float(availability_failure_grace))
        self._schema_cache_ttl = max(0.0, float(schema_cache_ttl))
        self._schema_cache: dict[tuple[Any, ...], tuple[float, int, tuple[dict[str, Any], ...]]] = {}
        self._catalog_snapshot_cache: ToolCatalogSnapshot | None = None
        self._generation = 0
        self._lock = threading.RLock()
        self._change_listeners: list[RegistryChangeCallback] = []
        self._hooks = hooks
        # Caller (AgentLoop) sets this for the duration of a turn so
        # ToolHookContext.session_id is populated for plugin hooks that
        # need to correlate tool calls with the owning session
        # (e.g. disk-cleanup's per-session tracker).
        self._active_session_id: str = ""

    def set_active_session(self, session_id: str) -> None:
        """Bind the current session id to subsequent ``execute()`` calls."""
        self._active_session_id = session_id or ""

    def register(
        self,
        tool: Tool,
        *,
        toolset: str | None = None,
        platforms: set[str] | frozenset[str] | None = None,
        check_fn: AvailabilityCheck | None = None,
    ) -> None:
        """Register a tool and its model-facing routing metadata."""
        declared_toolset = toolset or tool.toolset or infer_toolset(tool.name)
        declared_platforms = platforms
        if declared_platforms is None:
            declared_platforms = tool.supported_platforms
        normalized_platforms = (
            frozenset(str(value).strip().lower() for value in declared_platforms if str(value).strip())
            if declared_platforms is not None
            else None
        )
        with self._lock:
            self._tools[tool.name] = tool
            self._registrations[tool.name] = _ToolRegistration(
                toolset=(
                    str(declared_toolset or "extensions").strip().lower()
                    or "extensions"
                ),
                platforms=normalized_platforms,
                check_fn=check_fn,
            )
            self._generation += 1
            generation = self._generation
            self._catalog_snapshot_cache = None
        self.invalidate_availability(tool.name)
        self._notify_change(generation)
    
    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        with self._lock:
            existed = name in self._tools or name in self._registrations
            self._tools.pop(name, None)
            self._registrations.pop(name, None)
            if existed:
                self._generation += 1
                self._catalog_snapshot_cache = None
            generation = self._generation
        self.invalidate_availability(name)
        if existed:
            self._notify_change(generation)

    def subscribe_changes(
        self,
        callback: RegistryChangeCallback,
    ) -> Callable[[], None]:
        """Subscribe to generation changes; return an idempotent unsubscribe."""
        with self._lock:
            if callback not in self._change_listeners:
                self._change_listeners.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._change_listeners.remove(callback)
                except ValueError:
                    pass

        return _unsubscribe

    def _notify_change(self, generation: int) -> None:
        """Notify observers outside the registry lock."""
        with self._lock:
            listeners = tuple(self._change_listeners)
        for callback in listeners:
            try:
                callback(generation)
            except Exception as exc:  # noqa: BLE001 - observers are advisory
                logger.warning(
                    "Tool registry change observer failed at generation {}: {}",
                    generation,
                    type(exc).__name__,
                )
    
    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        with self._lock:
            return self._tools.get(name)
    
    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        with self._lock:
            return name in self._tools

    def get_toolsets(self) -> dict[str, str]:
        """Return the authoritative toolset recorded for each tool."""
        with self._lock:
            return {
                name: registration.toolset
                for name, registration in self._registrations.items()
            }

    def get_discovery_sources(self) -> dict[str, str]:
        """Return provider/plugin labels for connected external tools."""
        with self._lock:
            tools = dict(self._tools)
        sources: dict[str, str] = {}
        for name, tool in tools.items():
            try:
                source = str(tool.discovery_source or "").strip()
            except Exception:  # noqa: BLE001 - metadata must not break a turn
                source = ""
            if source:
                sources[name] = source
        return sources

    def get_catalog_snapshot(self) -> ToolCatalogSnapshot:
        """Return static schemas and metadata from one registry generation.

        Tool schema rendering happens outside the registry lock because a
        third-party tool may compute its schema dynamically. If registration
        changes while rendering, retry against the newer generation. A final
        continuously-mutating snapshot is still safe: its definitions are the
        authority and metadata maps are filtered to those exact names.
        """
        last: ToolCatalogSnapshot | None = None
        for _attempt in range(3):
            with self._lock:
                generation = self._generation
                cached = self._catalog_snapshot_cache
                if cached is not None and cached.generation == generation:
                    return cached
                tools = dict(self._tools)
                registrations = dict(self._registrations)
            definitions: list[dict[str, Any]] = []
            toolsets: dict[str, str] = {}
            sources: dict[str, str] = {}
            for name, tool in tools.items():
                try:
                    definition = _normalized_tool_definition(tool)
                except Exception as exc:  # noqa: BLE001 - index metadata is optional
                    logger.warning(
                        "Skipping tool '{}' in static catalog snapshot: {}",
                        name,
                        type(exc).__name__,
                    )
                    continue
                definitions.append(definition)
                registration = registrations.get(name)
                toolsets[name] = (
                    registration.toolset
                    if registration is not None
                    else infer_toolset(name)
                )
                try:
                    source = str(tool.discovery_source or "").strip()
                except Exception:  # noqa: BLE001 - optional metadata
                    source = ""
                if source:
                    sources[name] = source
            last = ToolCatalogSnapshot(
                generation=generation,
                definitions=tuple(definitions),
                toolsets=toolsets,
                sources=sources,
            )
            with self._lock:
                if self._generation == generation:
                    self._catalog_snapshot_cache = last
                    return last
        assert last is not None
        return last
    
    def invalidate_availability(self, name: str | None = None) -> None:
        """Drop cached capability checks after config/service changes."""
        with self._lock:
            self._schema_cache.clear()
            if name is None:
                self._availability_cache.clear()
                self._availability_last_good.clear()
                return
            stale = [key for key in self._availability_cache if key[0] == name]
            for key in stale:
                self._availability_cache.pop(key, None)
                self._availability_last_good.pop(key, None)

    def set_availability_cache_ttl(self, seconds: float) -> None:
        """Apply a hot-reloaded probe TTL and invalidate old decisions."""
        self._availability_cache_ttl = max(0.0, float(seconds))
        self.invalidate_availability()

    def set_availability_failure_grace(self, seconds: float) -> None:
        """Apply a hot-reloaded last-good grace window."""
        self._availability_failure_grace = max(0.0, float(seconds))
        self.invalidate_availability()

    def set_schema_cache_ttl(self, seconds: float) -> None:
        """Apply a hot-reloaded model schema cache TTL."""
        with self._lock:
            self._schema_cache_ttl = max(0.0, float(seconds))
            self._schema_cache.clear()

    def _passes_availability_check(self, name: str, platform: str) -> bool:
        key = (name, platform)
        now = time.monotonic()
        with self._lock:
            cached = self._availability_cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        with self._lock:
            tool = self._tools.get(name)
            registration = self._registrations.get(name)
        if tool is None or registration is None:
            return False
        probe_failed = False
        try:
            available = bool(
                registration.check_fn(ToolAvailabilityContext(platform=platform))
                if registration.check_fn is not None
                else tool.is_available()
            )
        except Exception as exc:  # noqa: BLE001 - a probe must never break a turn
            logger.debug("Availability check for {} failed: {}", name, exc)
            available = False
            probe_failed = True
        with self._lock:
            if available:
                self._availability_last_good[key] = now
            else:
                last_good = self._availability_last_good.get(key)
                if (
                    probe_failed
                    and last_good is not None
                    and last_good + self._availability_failure_grace > now
                ):
                    logger.debug(
                        "Availability probe for {} failed inside last-good grace",
                        name,
                    )
                    return True
            self._availability_cache[key] = (
                now + self._availability_cache_ttl,
                available,
            )
        return available

    def is_available(
        self,
        name: str,
        *,
        platform: str | None = None,
        enabled_toolsets: set[str] | frozenset[str] | None = None,
        disabled_toolsets: set[str] | frozenset[str] | None = None,
        disabled_tools: set[str] | frozenset[str] | None = None,
    ) -> bool:
        """Return whether a tool is callable in the supplied route."""
        with self._lock:
            tool = self._tools.get(name)
            registration = self._registrations.get(name)
        if tool is None or registration is None:
            return False

        normalized_platform = str(platform or "").strip().lower()
        if disabled_tools and name in disabled_tools:
            return False
        if enabled_toolsets is not None and registration.toolset not in enabled_toolsets:
            return False
        if disabled_toolsets and registration.toolset in disabled_toolsets:
            return False
        if (
            registration.platforms is not None
            and normalized_platform
            and normalized_platform not in registration.platforms
        ):
            return False
        return self._passes_availability_check(name, normalized_platform)

    def get_available_names(
        self,
        *,
        platform: str | None = None,
        enabled_toolsets: set[str] | frozenset[str] | None = None,
        disabled_toolsets: set[str] | frozenset[str] | None = None,
        disabled_tools: set[str] | frozenset[str] | None = None,
    ) -> list[str]:
        """Return registered tool names that survive routing and probes."""
        with self._lock:
            names = list(self._tools)
        return [
            name
            for name in names
            if self.is_available(
                name,
                platform=platform,
                enabled_toolsets=enabled_toolsets,
                disabled_toolsets=disabled_toolsets,
                disabled_tools=disabled_tools,
            )
        ]

    def get_definitions(
        self,
        *,
        platform: str | None = None,
        enabled_toolsets: set[str] | frozenset[str] | None = None,
        disabled_toolsets: set[str] | frozenset[str] | None = None,
        disabled_tools: set[str] | frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get routed and currently available tool definitions."""
        normalized_platform = str(platform or "").strip().lower()
        cache_key = (
            normalized_platform,
            None if enabled_toolsets is None else tuple(sorted(enabled_toolsets)),
            tuple(sorted(disabled_toolsets or ())),
            tuple(sorted(disabled_tools or ())),
        )
        now = time.monotonic()
        with self._lock:
            generation = self._generation
            cached = self._schema_cache.get(cache_key)
            if (
                cached is not None
                and cached[0] > now
                and cached[1] == generation
            ):
                return [dict(definition) for definition in cached[2]]

        # Snapshot the values first: MCP discovery registers tools from a
        # background thread (so a slow server can't delay boot), and iterating
        # the live dict here could otherwise raise "dictionary changed size
        # during iteration". A tool simply appears in this turn's list or the
        # next one.
        available_names = self.get_available_names(
            platform=platform,
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            disabled_tools=disabled_tools,
        )
        with self._lock:
            available_tools = [
                self._tools[name]
                for name in available_names
                if name in self._tools
            ]
        normalized = [_normalized_tool_definition(tool) for tool in available_tools]
        with self._lock:
            if self._generation == generation:
                cache_ttl = min(self._schema_cache_ttl, self._availability_cache_ttl)
                self._schema_cache[cache_key] = (
                    now + cache_ttl,
                    generation,
                    tuple(normalized),
                )
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
    
    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        *,
        platform: str | None = None,
        enabled_toolsets: set[str] | frozenset[str] | None = None,
        disabled_toolsets: set[str] | frozenset[str] | None = None,
        disabled_tools: set[str] | frozenset[str] | None = None,
    ) -> str:
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
        if not self.is_available(
            name,
            platform=platform,
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            disabled_tools=disabled_tools,
        ):
            route = f" for platform '{platform}'" if platform else ""
            return f"Error: Tool '{name}' is unavailable{route}"

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
        with self._lock:
            return list(self._tools.keys())

    @property
    def generation(self) -> int:
        """Monotonic registration generation used by higher-level caches."""
        with self._lock:
            return self._generation
    
    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)
    
    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._tools
