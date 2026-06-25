"""Tests for the cross-provider MCP JSON-schema repair pass.

We test the four repair categories from ``flowly/mcp/schema.py``:

  1. ``definitions`` / ``#/definitions/...`` → ``$defs`` / ``#/$defs/...``
     so the schema round-trips through providers that follow newer
     JSON Schema drafts (Kimi / Moonshot).
  2. Missing ``type`` on an object-shaped node coerces to ``"object"``
     so OpenAI's tool validator accepts it.
  3. ``required`` arrays are pruned to names that exist in
     ``properties`` so Gemini doesn't 400 on dangling required.
  4. Nullable unions collapse to the non-null branch with a
     ``nullable: true`` hint so Anthropic accepts them.

We also cover the name-sanitization helpers that build the
``mcp_{server}_{tool}`` registry name.
"""

from __future__ import annotations

from flowly.mcp.schema import (
    mcp_tool_name,
    normalize_mcp_input_schema,
    sanitize_mcp_name_component,
)


def test_sanitize_replaces_hyphens_dots_unicode():
    assert sanitize_mcp_name_component("foo-bar.baz") == "foo_bar_baz"
    assert sanitize_mcp_name_component("café_naïve") == "caf__na_ve"
    assert sanitize_mcp_name_component("") == ""
    assert sanitize_mcp_name_component(None) == ""  # type: ignore[arg-type]


def test_mcp_tool_name_uses_sanitized_components():
    assert mcp_tool_name("context7", "get-library-docs") == "mcp_context7_get_library_docs"
    assert mcp_tool_name("my.srv", "do/thing") == "mcp_my_srv_do_thing"


def test_definitions_rewritten_to_defs():
    schema = {
        "definitions": {"Foo": {"type": "string"}},
        "properties": {"a": {"$ref": "#/definitions/Foo"}},
        "type": "object",
    }
    out = normalize_mcp_input_schema(schema)
    assert "$defs" in out and "definitions" not in out
    assert out["properties"]["a"]["$ref"] == "#/$defs/Foo"


def test_missing_type_coerced_to_object_when_properties_present():
    schema = {"properties": {"x": {"type": "string"}}}
    out = normalize_mcp_input_schema(schema)
    assert out["type"] == "object"


def test_dangling_required_pruned():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a", "missing_field"],
    }
    out = normalize_mcp_input_schema(schema)
    assert out["required"] == ["a"]


def test_required_emptied_when_no_names_match():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["never_existed"],
    }
    out = normalize_mcp_input_schema(schema)
    assert "required" not in out


def test_nullable_union_collapses_with_hint():
    schema = {
        "type": "object",
        "properties": {
            "x": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }
    out = normalize_mcp_input_schema(schema)
    prop = out["properties"]["x"]
    assert prop.get("type") == "string"
    assert prop.get("nullable") is True
    assert "anyOf" not in prop


def test_empty_schema_returns_object_with_empty_properties():
    out = normalize_mcp_input_schema(None)
    assert out == {"type": "object", "properties": {}}
    out = normalize_mcp_input_schema({})
    assert out == {"type": "object", "properties": {}}


def test_object_with_no_properties_gets_empty_dict():
    out = normalize_mcp_input_schema({"type": "object"})
    assert out["properties"] == {}
