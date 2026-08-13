from __future__ import annotations

import json
from copy import deepcopy

from flowly.tool_activity import (
    TOOL_ACTIVITY_SCHEMA_VERSION,
    project_tool_call_for_ui,
    project_tool_messages_for_ui,
)


def _call(name: str, arguments: object, *, call_id: str = "call-1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": (arguments if isinstance(arguments, str) else json.dumps(arguments)),
        },
    }


def test_direct_tool_call_is_copied_without_projection_metadata() -> None:
    source = _call("web_search", {"query": "Flowly"})

    projected = project_tool_call_for_ui(source)

    assert projected == source
    assert projected is not source
    assert projected["function"] is not source["function"]


def test_deferred_call_projects_effective_identity_without_mutating_protocol() -> None:
    source = _call(
        "tool_call",
        {
            "name": "mcp_context7_query_docs",
            "arguments": {"libraryId": "/reactjs/react.dev", "query": "useEffect"},
        },
    )
    canonical = deepcopy(source)

    projected = project_tool_call_for_ui(source)

    assert source == canonical
    assert projected["id"] == source["id"]
    assert projected["function"]["name"] == "mcp_context7_query_docs"
    assert json.loads(projected["function"]["arguments"]) == {
        "libraryId": "/reactjs/react.dev",
        "query": "useEffect",
    }
    assert projected["tool_activity"] == {
        "schema_version": TOOL_ACTIVITY_SCHEMA_VERSION,
        "protocol_name": "tool_call",
        "effective_name": "mcp_context7_query_docs",
        "resolution_mode": "catalog_bridge",
    }


def test_deferred_call_preserves_string_encoded_effective_arguments() -> None:
    nested = '{"query":"React cleanup"}'
    source = _call(
        "tool_call",
        {"name": "mcp_context7_query_docs", "arguments": nested},
    )

    projected = project_tool_call_for_ui(source)

    assert projected["function"]["arguments"] == nested


def test_malformed_deferred_call_fails_open_to_protocol_display() -> None:
    malformed = _call("tool_call", "{not json")
    missing_name = _call("tool_call", {"arguments": {"query": "x"}})

    assert project_tool_call_for_ui(malformed) == malformed
    assert project_tool_call_for_ui(missing_name) == missing_name


def test_message_projection_gives_call_and_result_the_same_effective_name() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                _call(
                    "tool_call",
                    {
                        "name": "mcp_context7_query_docs",
                        "arguments": {"query": "useEffect"},
                    },
                )
            ],
        },
        {
            "role": "tool",
            "content": "docs",
            "tool_call_id": "call-1",
            "name": "tool_call",
        },
    ]
    canonical = deepcopy(messages)

    projected = project_tool_messages_for_ui(messages)

    assert messages == canonical
    assert projected[0]["tool_calls"][0]["function"]["name"] == ("mcp_context7_query_docs")
    assert projected[1]["name"] == "mcp_context7_query_docs"
    assert projected[1]["tool_activity"]["protocol_name"] == "tool_call"


def test_orphan_result_and_unknown_entries_remain_lossless() -> None:
    messages = [
        {"role": "assistant", "tool_calls": ["future-shape"]},
        {
            "role": "tool",
            "content": "orphan",
            "tool_call_id": "missing",
            "name": "tool_call",
        },
    ]

    assert project_tool_messages_for_ui(messages) == messages


def test_live_iteration_event_projects_deferred_call_for_ui() -> None:
    import asyncio

    from flowly.agent.loop import AgentLoop

    received: list[dict] = []

    async def capture(event: dict) -> None:
        received.append(event)

    agent = AgentLoop.__new__(AgentLoop)
    asyncio.run(
        agent._emit_iteration_event(
            outbound_channel="desktop",
            outbound_chat_id="chat-1",
            outbound_run_id="run-1",
            iteration_idx=0,
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    _call(
                        "tool_call",
                        {
                            "name": "mcp_context7_query_docs",
                            "arguments": {"query": "useEffect"},
                        },
                    )
                ],
            },
            on_iteration=capture,
        )
    )

    assert received[0]["tool_calls"][0]["function"]["name"] == ("mcp_context7_query_docs")
