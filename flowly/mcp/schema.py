"""MCP tool name + JSON schema normalization.

Two responsibilities:

1. :func:`sanitize_mcp_name_component` — make server / tool name components
   safe for inclusion in OpenAI-style function names. Hyphens and other
   non-``[A-Za-z0-9_]`` characters become underscores. Tools register as
   ``mcp_{server}_{tool}``.

2. :func:`normalize_mcp_input_schema` — repair MCP server input schemas
   so they validate across providers. The MCP spec allows draft-07-style
   ``definitions`` references and nullable unions; OpenAI-compatible
   providers each reject a different subset. We collapse the common
   trouble shapes in one pass:

   * ``definitions`` / ``#/definitions/...`` → ``$defs`` / ``#/$defs/...``
     (Kimi/Moonshot).
   * Missing or null ``type`` on an object-shaped node → ``"object"``.
   * Object nodes missing ``properties`` get an empty dict so
     ``required`` entries don't dangle.
   * ``required`` arrays pruned to names that actually exist in
     ``properties`` (Gemini 400s on dangling required).
   * Nullable unions (``anyOf: [{...}, {"type": "null"}]``) collapsed to
     the non-null branch (Anthropic rejects nullable in tool input).
"""

from __future__ import annotations

import re
from typing import Any


_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]")


def sanitize_mcp_name_component(value: str) -> str:
    """Return a tool/server name component safe for provider validation.

    Non-empty input always returns a non-empty string of ``[A-Za-z0-9_]``.
    ``None`` and empty input both return ``""``.
    """
    return _NAME_SAFE.sub("_", str(value or ""))


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Build the ``mcp_{server}_{tool}`` registry name for an MCP tool."""
    return f"mcp_{sanitize_mcp_name_component(server_name)}_{sanitize_mcp_name_component(tool_name)}"


def normalize_mcp_input_schema(schema: dict | None) -> dict:
    """Normalize an MCP tool input schema for cross-provider tool calling.

    See module docstring for the full list of repairs. The output is
    always a dict with ``type: "object"`` at the top level.
    """
    if not schema:
        return {"type": "object", "properties": {}}

    normalized = _rewrite_local_refs(schema)
    normalized = _strip_nullable_union(normalized)
    normalized = _repair_object_shape(normalized)

    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized = {**normalized, "properties": {}}
    return normalized


def _rewrite_local_refs(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            out_key = "$defs" if key == "definitions" else key
            out[out_key] = _rewrite_local_refs(value)
        ref = out.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            out["$ref"] = "#/$defs/" + ref[len("#/definitions/"):]
        return out
    if isinstance(node, list):
        return [_rewrite_local_refs(item) for item in node]
    return node


def _is_null_branch(branch: Any) -> bool:
    return isinstance(branch, dict) and branch.get("type") == "null"


def _strip_nullable_union(node: Any) -> Any:
    """Collapse ``anyOf: [X, {type: null}]`` to X, keep ``nullable: true`` hint.

    Recurses into nested objects/arrays so deeply-nested optional fields
    are reachable.
    """
    if isinstance(node, list):
        return [_strip_nullable_union(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        out[key] = _strip_nullable_union(value)

    for union_key in ("anyOf", "oneOf"):
        branches = out.get(union_key)
        if not isinstance(branches, list) or len(branches) < 2:
            continue
        null_branches = [b for b in branches if _is_null_branch(b)]
        non_null = [b for b in branches if not _is_null_branch(b)]
        if not null_branches or not non_null:
            continue
        # Collapse: keep the first non-null branch, merge into parent,
        # drop the union key, set nullable hint.
        survivor = non_null[0] if len(non_null) == 1 else {union_key: non_null}
        if isinstance(survivor, dict):
            # Merge survivor onto the parent (parent wins on overlap so
            # caller-specified title/description survive).
            for sk, sv in survivor.items():
                out.setdefault(sk, sv)
        out.pop(union_key, None)
        out["nullable"] = True
    return out


def _repair_object_shape(node: Any) -> Any:
    if isinstance(node, list):
        return [_repair_object_shape(item) for item in node]
    if not isinstance(node, dict):
        return node

    repaired = {k: _repair_object_shape(v) for k, v in node.items()}

    if not repaired.get("type") and (
        "properties" in repaired or "required" in repaired
    ):
        repaired["type"] = "object"

    if repaired.get("type") == "object":
        props = repaired.get("properties")
        if not isinstance(props, dict):
            repaired["properties"] = {}

        required = repaired.get("required")
        if isinstance(required, list):
            props = repaired.get("properties") or {}
            valid = [r for r in required if isinstance(r, str) and r in props]
            if not valid:
                repaired.pop("required", None)
            elif len(valid) != len(required):
                repaired["required"] = valid

    return repaired
