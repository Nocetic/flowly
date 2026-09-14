"""Run-scoped display state; never a source of executable tool requests."""

import json
import time
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.providers.tool_preview import MAX_PREVIEW_ARGUMENTS, ToolCallPreview
from flowly.tool_activity import project_tool_call_for_ui

_TERMINAL = frozenset({"completed", "failed", "stopped"})
_MAX_RESULT = 32_768


class ToolProgress:
    def __init__(self, run_id: str, publish: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self.run_id = run_id
        self.publish = publish
        self.iteration = -1
        self.revision = 0
        self.calls: dict[str, dict[str, Any]] = {}

    async def _batch(self, iteration: int) -> None:
        if iteration != self.iteration:
            await self.settle("stopped")
            self.calls = {}
            self.iteration = iteration

    async def _emit(self, call: dict[str, Any]) -> None:
        self.revision += 1
        # Every field is a bounded primitive. A subscriber keeps an immutable
        # historical snapshot even when the next input fragment arrives.
        event = {"state": "tool_progress", "runId": self.run_id,
                 "iterationIdx": self.iteration, "revision": self.revision, "call": dict(call)}
        try:
            await self.publish(event)
        except Exception:
            logger.debug("Tool progress delivery failed (non-fatal)", exc_info=True)

    async def preview(self, iteration: int, preview: ToolCallPreview) -> None:
        if not preview.id or not preview.name:
            return
        await self._batch(iteration)
        previous = self.calls.get(preview.id)
        if previous and previous["state"] != "preparing":
            return
        call = {"id": preview.id, "index": preview.index, "name": preview.name,
                "arguments": preview.arguments, "argumentsTruncated": preview.arguments_truncated,
                "state": "preparing"}
        if call != previous:
            self.calls[preview.id] = call
            await self._emit(call)

    async def announce(self, iteration: int, calls: list[dict[str, Any]]) -> None:
        await self._batch(iteration)
        accepted = {call.get("id") for call in calls}
        for call in self.calls.values():
            if call["id"] not in accepted and call["state"] not in _TERMINAL:
                call["state"] = "stopped"
                await self._emit(call)
        for index, raw in enumerate(calls):
            projected = project_tool_call_for_ui(raw)
            call_id = projected.get("id")
            if not isinstance(call_id, str) or not call_id:
                continue
            previous = self.calls.get(call_id)
            if previous and previous["state"] != "preparing":
                continue
            function = projected.get("function") or {}
            arguments = str(function.get("arguments") or "{}")
            call = {"id": call_id, "index": index, "name": str(function.get("name") or "")[:128],
                    "arguments": arguments[:MAX_PREVIEW_ARGUMENTS],
                    "argumentsTruncated": len(arguments) > MAX_PREVIEW_ARGUMENTS, "state": "queued"}
            self.calls[call_id] = call
            await self._emit(call)

    async def start(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        call = self.calls.get(call_id)
        if call is None or call["state"] in _TERMINAL or call["state"] == "running":
            return
        encoded = json.dumps(arguments, ensure_ascii=False, default=str)
        call.update(state="running", name=name[:128], startedAt=int(time.time() * 1000),
                    arguments=encoded[:MAX_PREVIEW_ARGUMENTS],
                    argumentsTruncated=len(encoded) > MAX_PREVIEW_ARGUMENTS)
        await self._emit(call)

    async def result(self, call_id: str, result: str, *, failed: bool, stopped: bool = False) -> None:
        call = self.calls.get(call_id)
        if call is None or call["state"] in _TERMINAL:
            return
        call.update(state="stopped" if stopped else "failed" if failed else "completed",
                    result=result[:_MAX_RESULT], resultTruncated=len(result) > _MAX_RESULT,
                    completedAt=int(time.time() * 1000))
        await self._emit(call)

    async def settle(self, state: str) -> None:
        if state not in {"failed", "stopped"}:
            raise ValueError("Unfinished calls cannot be declared successful")
        for call in self.calls.values():
            if call["state"] not in _TERMINAL:
                call.update(state=state, completedAt=int(time.time() * 1000))
                await self._emit(call)
