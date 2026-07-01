"""Tests for ToolRegistry and schema normalization."""

import pytest

from flowly.agent.tools.base import Tool
from flowly.agent.tools.registry import (
    ToolRegistry,
    _extract_enum_values,
    _merge_property_schema,
    _normalize_tool_parameters_schema,
    _unwrap_raw_envelope,
)
import json
from typing import Any


# ── Helpers ─────────────────────────────────────────────────────────


class DummyTool(Tool):
    """Minimal tool for testing."""

    def __init__(self, name: str = "dummy", params: dict | None = None):
        self._name = name
        self._params = params or {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["go", "stop"]},
            },
            "required": ["action"],
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "A dummy tool for testing"

    @property
    def parameters(self) -> dict[str, Any]:
        return self._params

    async def execute(self, **kwargs: Any) -> str:
        return f"executed:{kwargs}"


# ── _extract_enum_values ────────────────────────────────────────────


class TestExtractEnumValues:
    def test_direct_enum(self):
        assert _extract_enum_values({"enum": ["a", "b"]}) == ["a", "b"]

    def test_const(self):
        assert _extract_enum_values({"const": "x"}) == ["x"]

    def test_any_of(self):
        schema = {"anyOf": [{"const": "a"}, {"const": "b"}]}
        assert _extract_enum_values(schema) == ["a", "b"]

    def test_one_of(self):
        schema = {"oneOf": [{"enum": ["x"]}, {"enum": ["y"]}]}
        assert _extract_enum_values(schema) == ["x", "y"]

    def test_non_dict(self):
        assert _extract_enum_values("not a dict") is None

    def test_no_enum(self):
        assert _extract_enum_values({"type": "string"}) is None


# ── _unwrap_raw_envelope ────────────────────────────────────────────


class TestUnwrapRawEnvelope:
    def test_flattens_sole_raw_json_object(self):
        # The exact shape deepseek emits on large-payload artifact calls.
        params = {"raw": json.dumps({"action": "create", "type": "html", "content": "<x>"})}
        assert _unwrap_raw_envelope(params) == {
            "action": "create", "type": "html", "content": "<x>",
        }

    def test_normal_params_untouched(self):
        params = {"action": "create", "type": "html"}
        assert _unwrap_raw_envelope(params) == params

    def test_raw_with_other_keys_untouched(self):
        # A genuine `raw` field alongside others must NOT be flattened.
        params = {"raw": "abc", "other": 1}
        assert _unwrap_raw_envelope(params) == params

    def test_raw_non_json_untouched(self):
        params = {"raw": "just a string"}
        assert _unwrap_raw_envelope(params) == params

    def test_raw_json_non_object_untouched(self):
        params = {"raw": json.dumps([1, 2, 3])}
        assert _unwrap_raw_envelope(params) == params

    @pytest.mark.asyncio
    async def test_execute_unwraps_before_validation(self):
        reg = ToolRegistry()
        reg.register(DummyTool(name="dummy"))  # requires "action"
        # Wrapped envelope would otherwise fail "Missing required parameter: action".
        out = await reg.execute("dummy", {"raw": json.dumps({"action": "go"})})
        assert out == "executed:{'action': 'go'}"


# ── _normalize_tool_parameters_schema ───────────────────────────────


class TestNormalizeSchema:
    def test_passthrough_normal_schema(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        result = _normalize_tool_parameters_schema(schema)
        assert result == schema

    def test_adds_type_object_when_missing(self):
        schema = {"properties": {"a": {"type": "string"}}, "required": ["a"]}
        result = _normalize_tool_parameters_schema(schema)
        assert result["type"] == "object"

    def test_flattens_any_of(self):
        schema = {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"action": {"const": "go"}, "speed": {"type": "integer"}},
                    "required": ["action"],
                },
                {
                    "type": "object",
                    "properties": {"action": {"const": "stop"}},
                    "required": ["action"],
                },
            ]
        }
        result = _normalize_tool_parameters_schema(schema)
        assert result["type"] == "object"
        assert "action" in result["properties"]
        assert "speed" in result["properties"]
        # action is required in all variants
        assert "action" in result.get("required", [])

    def test_non_dict_returns_empty_schema(self):
        result = _normalize_tool_parameters_schema(None)
        assert result["type"] == "object"


# ── ToolRegistry ────────────────────────────────────────────────────


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        tool = DummyTool("test_tool")
        reg.register(tool)

        assert reg.has("test_tool")
        assert reg.get("test_tool") is tool
        assert "test_tool" in reg
        assert len(reg) == 1

    def test_unregister(self):
        reg = ToolRegistry()
        reg.register(DummyTool("x"))
        reg.unregister("x")
        assert not reg.has("x")
        assert len(reg) == 0

    def test_unregister_nonexistent(self):
        reg = ToolRegistry()
        reg.unregister("nope")  # should not raise

    def test_tool_names(self):
        reg = ToolRegistry()
        reg.register(DummyTool("alpha"))
        reg.register(DummyTool("beta"))
        assert sorted(reg.tool_names) == ["alpha", "beta"]

    def test_get_definitions(self):
        reg = ToolRegistry()
        reg.register(DummyTool("my_tool"))
        defs = reg.get_definitions()
        assert len(defs) == 1
        assert defs[0]["type"] == "function"
        assert defs[0]["function"]["name"] == "my_tool"

    def test_validate_missing_required(self):
        reg = ToolRegistry()
        reg.register(DummyTool("t"))
        error = reg.validate_tool_call("t", {})
        assert error is not None
        assert "action" in error

    def test_validate_empty_string_required(self):
        reg = ToolRegistry()
        reg.register(DummyTool("t"))
        error = reg.validate_tool_call("t", {"action": ""})
        assert error is not None
        assert "action" in error

    def test_validate_none_required(self):
        reg = ToolRegistry()
        reg.register(DummyTool("t"))
        error = reg.validate_tool_call("t", {"action": None})
        assert error is not None

    def test_validate_valid(self):
        reg = ToolRegistry()
        reg.register(DummyTool("t"))
        error = reg.validate_tool_call("t", {"action": "go"})
        assert error is None

    def test_validate_unknown_tool(self):
        reg = ToolRegistry()
        error = reg.validate_tool_call("nope", {"a": 1})
        assert error is not None
        assert "not found" in error

    def test_validate_invalid_params_type(self):
        reg = ToolRegistry()
        reg.register(DummyTool("t"))
        error = reg.validate_tool_call("t", "not a dict")
        assert error is not None
        assert "Invalid parameters" in error

    @pytest.mark.asyncio
    async def test_execute_success(self):
        reg = ToolRegistry()
        reg.register(DummyTool("t"))
        result = await reg.execute("t", {"action": "go"})
        assert "executed" in result

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        reg = ToolRegistry()
        result = await reg.execute("missing", {})
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_execute_validation_error(self):
        reg = ToolRegistry()
        reg.register(DummyTool("t"))
        result = await reg.execute("t", {})
        assert "Missing required" in result

    @pytest.mark.asyncio
    async def test_execute_drops_unexpected_kwarg_on_strict_signature(self):
        """A model-invented arg not in the tool's execute() signature is
        dropped instead of raising 'unexpected keyword argument'."""

        class StrictTool(Tool):
            def __init__(self):
                self._name = "strict"

            @property
            def name(self) -> str:
                return self._name

            @property
            def description(self) -> str:
                return "strict-signature tool"

            @property
            def parameters(self) -> dict[str, Any]:
                return {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                }

            async def execute(self, query: str) -> str:
                return f"ran:{query}"

        reg = ToolRegistry()
        reg.register(StrictTool())
        # `count` is not in the schema nor the signature — must be dropped,
        # not surfaced as "Error executing strict: ... unexpected keyword".
        result = await reg.execute("strict", {"query": "hi", "count": 10})
        assert result == "ran:hi"

    @pytest.mark.asyncio
    async def test_execute_keeps_kwargs_when_execute_accepts_var_keyword(self):
        """Tools whose execute(**kwargs) accepts anything still get every arg."""
        reg = ToolRegistry()
        reg.register(DummyTool("t"))
        result = await reg.execute("t", {"action": "go", "extra": 1})
        assert "extra" in result
