"""Transport-safe projection of canonical tool protocol messages for UIs.

The agent must retain the provider-facing ``tool_call`` wrapper in its working
history: strict providers require the assistant call name and the following
tool-result name to match exactly.  Product surfaces, however, should identify
the tool that actually ran rather than expose that routing implementation
detail.

This module is the single boundary between those two contracts.  Every helper
returns fresh dictionaries and never mutates canonical session/provider data.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Iterable, Mapping

TOOL_ACTIVITY_SCHEMA_VERSION = 1
_DEFERRED_CALL_PROTOCOL_NAME = "tool_call"


def _object_arguments(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _encoded_arguments(value: Any) -> str:
    """Return the effective call arguments in the established wire shape."""

    if isinstance(value, str):
        return value
    if value is None:
        value = {}
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def project_tool_call_for_ui(call: Mapping[str, Any]) -> dict[str, Any]:
    """Project one OpenAI-shaped tool call without touching the source.

    Direct calls retain their existing shape.  A valid deferred ``tool_call``
    wrapper is flattened so legacy clients immediately display the effective
    tool, while additive metadata keeps the protocol identity and projection
    version explicit for newer clients and diagnostics.

    Malformed wrappers fail open to their original display.  Execution still
    performs its own authoritative registry/policy validation; presentation is
    never used to authorize a call.
    """

    projected = deepcopy(dict(call))
    function = projected.get("function")
    if not isinstance(function, Mapping):
        return projected
    if function.get("name") != _DEFERRED_CALL_PROTOCOL_NAME:
        return projected

    wrapper_arguments = _object_arguments(function.get("arguments", "{}"))
    if wrapper_arguments is None:
        return projected
    effective_name = wrapper_arguments.get("name")
    if not isinstance(effective_name, str) or not effective_name.strip():
        return projected

    effective_name = effective_name.strip()
    projected["function"] = {
        **dict(function),
        "name": effective_name,
        "arguments": _encoded_arguments(wrapper_arguments.get("arguments", {})),
    }
    projected["tool_activity"] = {
        "schema_version": TOOL_ACTIVITY_SCHEMA_VERSION,
        "protocol_name": _DEFERRED_CALL_PROTOCOL_NAME,
        "effective_name": effective_name,
        "resolution_mode": "catalog_bridge",
    }
    return projected


def project_tool_messages_for_ui(
    messages: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project a tool-message sequence, preserving call/result linkage.

    Result messages do not contain the wrapper arguments, so the assistant
    call's stable id is used to give the corresponding result the same
    effective name.  Orphans and non-tool messages remain losslessly copied.
    """

    projected_messages: list[dict[str, Any]] = []
    effective_name_by_call_id: dict[str, str] = {}

    for source in messages:
        # Shallow-copy the message envelope. History DTOs may contain large
        # immutable attachment previews; duplicating those bytes merely to
        # rewrite a tiny tool_calls field would add avoidable peak memory. Every
        # nested value we modify below is replaced with its own fresh copy.
        projected = dict(source)
        calls = source.get("tool_calls")
        if isinstance(calls, list):
            projected_calls: list[Any] = []
            for call in calls:
                if not isinstance(call, Mapping):
                    projected_calls.append(deepcopy(call))
                    continue
                projected_call = project_tool_call_for_ui(call)
                projected_calls.append(projected_call)
                call_id = projected_call.get("id")
                function = projected_call.get("function")
                activity = projected_call.get("tool_activity")
                if (
                    isinstance(call_id, str)
                    and isinstance(function, Mapping)
                    and isinstance(activity, Mapping)
                    and activity.get("protocol_name") == _DEFERRED_CALL_PROTOCOL_NAME
                ):
                    effective_name = function.get("name")
                    if isinstance(effective_name, str) and effective_name:
                        effective_name_by_call_id[call_id] = effective_name
            projected["tool_calls"] = projected_calls

        tool_call_id = source.get("tool_call_id")
        effective_name = (
            effective_name_by_call_id.get(tool_call_id) if isinstance(tool_call_id, str) else None
        )
        if effective_name and source.get("name") == _DEFERRED_CALL_PROTOCOL_NAME:
            projected["name"] = effective_name
            projected["tool_activity"] = {
                "schema_version": TOOL_ACTIVITY_SCHEMA_VERSION,
                "protocol_name": _DEFERRED_CALL_PROTOCOL_NAME,
                "effective_name": effective_name,
                "resolution_mode": "catalog_bridge",
            }

        projected_messages.append(projected)

    return projected_messages
