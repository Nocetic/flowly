"""Strict primary-runtime broker for installation-level agent data.

This module is intentionally transport independent.  Desktop-owned local
profiles and the remote ProfileHost both authenticate a source runtime, then
call this exact dispatcher on the primary gateway.  The caller chooses from a
closed service/tool vocabulary; arbitrary gateway RPC forwarding is never
exposed to a model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from flowly.agent.tools.artifact import ArtifactTool
from flowly.agent.tools.board import BoardRunTool, build_board_tools


MAX_SHARED_REQUEST_BYTES = 12 * 1024 * 1024
MAX_SHARED_RESULT_BYTES = 16 * 1024 * 1024
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TURN_ORIGINS = frozenset({"user", "profile", "group", "routine", "task"})
_BOARD_TOOLS = frozenset({
    "board_add",
    "board_list",
    "board_get",
    "board_update",
    "board_run",
})


class SharedServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    except (TypeError, ValueError) as exc:
        raise SharedServiceError(
            "SHARED_REQUEST_INVALID",
            "Shared service arguments must be JSON values.",
        ) from exc


def validate_shared_service_request(params: Any) -> dict[str, Any]:
    """Return a bounded canonical request or raise a wire-safe error."""
    if not isinstance(params, dict):
        raise SharedServiceError("SHARED_REQUEST_INVALID", "Shared service request is invalid.")
    if _serialized_size(params) > MAX_SHARED_REQUEST_BYTES:
        raise SharedServiceError("SHARED_REQUEST_TOO_LARGE", "Shared service request is too large.")

    service = str(params.get("service") or "").strip()
    tool = str(params.get("tool") or "").strip()
    source_profile = str(params.get("sourceProfile") or "").strip()
    source_session = str(params.get("sourceSessionKey") or "").strip()
    turn_origin = str(params.get("turnOrigin") or "").strip()
    correlation_id = str(params.get("correlationId") or "").strip()
    arguments = params.get("arguments")

    if service not in {"board", "artifacts"}:
        raise SharedServiceError("SHARED_SERVICE_UNKNOWN", "Shared service is not supported.")
    if service == "board" and tool not in _BOARD_TOOLS:
        raise SharedServiceError("SHARED_TOOL_UNKNOWN", "Shared Board tool is not supported.")
    if service == "artifacts" and tool != "artifact":
        raise SharedServiceError("SHARED_TOOL_UNKNOWN", "Shared artifact tool is not supported.")
    if not _PROFILE_RE.fullmatch(source_profile):
        raise SharedServiceError("SHARED_SOURCE_INVALID", "Shared service source is invalid.")
    if not source_session or len(source_session) > 256 or "\x00" in source_session:
        raise SharedServiceError("SHARED_SOURCE_INVALID", "Shared service session is invalid.")
    if turn_origin not in _TURN_ORIGINS:
        raise SharedServiceError("SHARED_SOURCE_INVALID", "Shared service turn origin is invalid.")
    if not correlation_id or len(correlation_id) > 128 or "\x00" in correlation_id:
        raise SharedServiceError("SHARED_SOURCE_INVALID", "Shared service correlation is invalid.")
    if not isinstance(arguments, dict) or len(arguments) > 64:
        raise SharedServiceError("SHARED_ARGUMENTS_INVALID", "Shared service arguments are invalid.")

    return {
        "service": service,
        "tool": tool,
        "sourceProfile": source_profile,
        "sourceSessionKey": source_session,
        "turnOrigin": turn_origin,
        "correlationId": correlation_id,
        "arguments": dict(arguments),
    }


def _artifact_arguments(
    store: Any,
    request: dict[str, Any],
) -> dict[str, Any]:
    arguments = dict(request["arguments"])
    action = str(arguments.get("action") or "").strip()
    profile = request["sourceProfile"]
    provenance = {
        "sourceProfile": profile,
        "sourceSessionKey": request["sourceSessionKey"],
        "turnOrigin": request["turnOrigin"],
        "correlationId": request["correlationId"],
    }

    if action == "create":
        metadata = arguments.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["flowlyProvenance"] = {
            "createdByProfile": profile,
            **provenance,
        }
        arguments["metadata"] = metadata
        arguments["session_key"] = request["sourceSessionKey"]
    elif action == "update":
        artifact_id = str(arguments.get("artifact_id") or "")
        existing = store.get(artifact_id) if artifact_id else None
        if existing:
            metadata = dict(existing.get("metadata") or {})
            original = metadata.get("flowlyProvenance")
            history = dict(original) if isinstance(original, dict) else {}
            history.update({
                "lastModifiedByProfile": profile,
                "lastModification": provenance,
            })
            metadata["flowlyProvenance"] = history
            arguments["metadata"] = metadata
    return arguments


async def invoke_shared_service(
    params: Any,
    *,
    board_store: Any,
    board_orchestrator: Any,
    artifact_store: Any,
    artifact_on_change: Callable[[str, dict], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Execute one validated tool call against the primary-owned services."""
    request = validate_shared_service_request(params)
    identity = f"profile:{request['sourceProfile']}"

    if request["service"] == "artifacts":
        if artifact_store is None:
            raise SharedServiceError("SHARED_SERVICE_UNAVAILABLE", "Artifacts are unavailable.")
        tool = ArtifactTool(store=artifact_store, on_change=artifact_on_change)
        arguments = _artifact_arguments(artifact_store, request)
    else:
        if board_store is None:
            raise SharedServiceError("SHARED_SERVICE_UNAVAILABLE", "Board is unavailable.")
        tools = {
            item.name: item
            for item in build_board_tools(board_store, board_orchestrator)
        }
        tool = tools.get(request["tool"])
        if tool is None:
            raise SharedServiceError("SHARED_SERVICE_UNAVAILABLE", "Board execution is unavailable.")
        tool.set_context("", "")
        tool.set_identity(
            identity,
            created_by=identity,
            request_id=request["correlationId"],
        )
        if isinstance(tool, BoardRunTool):
            # The result belongs to the shared card and completion push.  A
            # profile conversation is isolated, so never inject it into the
            # primary chat transcript as a second delivery.
            tool.set_delivery(False)
        arguments = dict(request["arguments"])

    output = await tool.execute(**arguments)
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    if len(output.encode()) > MAX_SHARED_RESULT_BYTES:
        raise SharedServiceError("SHARED_RESULT_TOO_LARGE", "Shared service result is too large.")
    return {"ok": True, "output": output}
