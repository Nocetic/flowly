"""Agent tools backed by the primary runtime's authenticated shared services.

Named profiles deliberately keep their sessions, memory and workspace local.
User-facing Board cards and artifacts are installation-level resources,
however, so these adapters preserve the normal tool schemas while routing the
actual operation through the profile owner's reverse-RPC connection.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from flowly.agent.tools.artifact import ArtifactTool
from flowly.agent.tools.board import (
    BoardAddTool,
    BoardGetTool,
    BoardListTool,
    BoardRunTool,
    BoardUpdateTool,
)
from flowly.artifacts.context import INTERNAL_CONTEXT_TAGS


class _SharedServiceMixin:
    _gateway: Any
    _shared_service: str

    async def _invoke_shared(self, arguments: dict[str, Any]) -> str:
        try:
            result = await self._gateway.send_shared_service_request(
                request_id=str(uuid.uuid4()),
                service=self._shared_service,
                tool=str(self.name),
                arguments=arguments,
            )
        except Exception as exc:  # transport failures must remain tool results
            return json.dumps({"ok": False, "error": str(exc)})
        if not isinstance(result, dict):
            return json.dumps({"ok": False, "error": "Shared service returned an invalid result"})
        if result.get("error"):
            return json.dumps({
                "ok": False,
                "error": str(result.get("error")),
                "error_code": str(result.get("error_code") or "SHARED_SERVICE_FAILED"),
            })
        output = result.get("output")
        if isinstance(output, str):
            return output
        return json.dumps({"ok": False, "error": "Shared service returned no tool output"})


class SharedArtifactTool(_SharedServiceMixin, ArtifactTool):
    """The normal artifact surface, persisted in the primary Library."""

    _shared_service = "artifacts"

    def __init__(self, gateway_server: Any, local_store: Any):
        # The local profile store remains attached to AgentLoop for automatic
        # internal context spills; only the model-facing tool is replaced.
        super().__init__(store=local_store)
        self._gateway = gateway_server

    async def execute(self, action: str = "", **kwargs: Any) -> str:
        artifact_id = str(kwargs.get("artifact_id") or "")
        local = self._store.get(artifact_id) if artifact_id and self._store else None
        if local:
            if action in {"get", "update", "delete", "get_versions", "export"}:
                # Automatic large-context spills and artifacts created by an
                # older profile runtime remain addressable from existing chat
                # history. New user-facing creates go to the shared Library.
                return await super().execute(action=action, **kwargs)
            if action in {"promote", "pin"}:
                # Promotion crosses the boundary explicitly: copy the private
                # payload into the shared Library, leaving the original spill
                # intact for session continuity and rollback.
                tags = [
                    tag for tag in (local.get("tags") or [])
                    if tag not in INTERNAL_CONTEXT_TAGS
                ]
                if "promoted" not in tags:
                    tags.append("promoted")
                return await self._invoke_shared({
                    "action": "create",
                    "type": local.get("type") or "markdown",
                    "title": kwargs.get("title") or local.get("title") or "Saved artifact",
                    "content": local.get("content") or "",
                    "tags": tags,
                    "pinned": bool(kwargs.get("pinned", action == "pin")),
                    "dashboard_size": kwargs.get("dashboard_size") or local.get("dashboard_size") or "medium",
                })
        return await self._invoke_shared({"action": action, **kwargs})

    def set_on_change(self, callback: Any) -> None:
        # Shared mutations are broadcast by the primary runtime.  Retaining
        # this method keeps gateway bootstrap wiring compatible.
        del callback


class SharedBoardAddTool(_SharedServiceMixin, BoardAddTool):
    _shared_service = "board"

    def __init__(self, gateway_server: Any):
        super().__init__(store=None, orchestrator=None)  # type: ignore[arg-type]
        self._gateway = gateway_server

    async def execute(self, **kwargs: Any) -> str:
        return await self._invoke_shared(kwargs)


class SharedBoardListTool(_SharedServiceMixin, BoardListTool):
    _shared_service = "board"

    def __init__(self, gateway_server: Any):
        super().__init__(store=None, orchestrator=None)  # type: ignore[arg-type]
        self._gateway = gateway_server

    async def execute(self, **kwargs: Any) -> str:
        return await self._invoke_shared(kwargs)


class SharedBoardGetTool(_SharedServiceMixin, BoardGetTool):
    _shared_service = "board"

    def __init__(self, gateway_server: Any):
        super().__init__(store=None, orchestrator=None)  # type: ignore[arg-type]
        self._gateway = gateway_server

    async def execute(self, **kwargs: Any) -> str:
        return await self._invoke_shared(kwargs)


class SharedBoardUpdateTool(_SharedServiceMixin, BoardUpdateTool):
    _shared_service = "board"

    def __init__(self, gateway_server: Any):
        super().__init__(store=None, orchestrator=None)  # type: ignore[arg-type]
        self._gateway = gateway_server

    async def execute(self, **kwargs: Any) -> str:
        return await self._invoke_shared(kwargs)


class SharedBoardRunTool(_SharedServiceMixin, BoardRunTool):
    _shared_service = "board"

    def __init__(self, gateway_server: Any):
        super().__init__(store=None, orchestrator=None)  # type: ignore[arg-type]
        self._gateway = gateway_server

    async def execute(self, **kwargs: Any) -> str:
        return await self._invoke_shared(kwargs)


def build_shared_service_tools(gateway_server: Any, *, local_artifact_store: Any) -> list[Any]:
    """Return the complete shared user-data surface for a named profile."""
    return [
        SharedArtifactTool(gateway_server, local_artifact_store),
        SharedBoardAddTool(gateway_server),
        SharedBoardListTool(gateway_server),
        SharedBoardGetTool(gateway_server),
        SharedBoardUpdateTool(gateway_server),
        SharedBoardRunTool(gateway_server),
    ]
