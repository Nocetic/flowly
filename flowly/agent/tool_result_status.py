"""Recognize MCP error envelopes without interpreting successful user data."""

import json


def mcp_tool_result_failed(name: str, result: str) -> bool:
    if not name.startswith("mcp_") or not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("isError") is True:
        return True
    if payload.get("isError") is False:
        return False
    if name == "mcp_connection":
        return payload.get("status") == "failed" or bool(payload.get("error"))
    return bool(payload.get("error")) and set(payload) <= {"error", "code", "hint"}
