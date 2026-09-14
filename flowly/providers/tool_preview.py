"""Bounded display snapshots, separate from executable provider tool calls."""

from dataclasses import dataclass
from typing import Any

MAX_PREVIEW_ARGUMENTS = 16_384
MAX_PREVIEW_CALLS = 128


@dataclass(frozen=True)
class ToolCallPreview:
    index: int
    id: str
    name: str
    arguments: str
    arguments_truncated: bool = False


class ToolPreviewSnapshots:
    """Emit changed snapshots only; a very large argument cannot flood the UI."""

    def __init__(self) -> None:
        self._last: dict[int, ToolCallPreview] = {}

    def update(self, index: int, call_id: str, name: str, arguments: str) -> ToolCallPreview | None:
        if type(index) is not int or not 0 <= index < MAX_PREVIEW_CALLS or not name:
            return None
        preview = ToolCallPreview(
            index=index, id=call_id[:256], name=name[:128],
            arguments=arguments[:MAX_PREVIEW_ARGUMENTS],
            arguments_truncated=len(arguments) > MAX_PREVIEW_ARGUMENTS,
        )
        if self._last.get(index) == preview:
            return None
        self._last[index] = preview
        return preview


class ResponseToolPreviews:
    """Only function-call output events enter this display projection.

    Reasoning, opaque replay fields and text are never copied here. Canonical
    completed output remains owned by each provider's existing result parser.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}
        self._item_indices: dict[str, int] = {}
        self._snapshots = ToolPreviewSnapshots()

    def consume(self, event: dict[str, Any]) -> ToolCallPreview | None:
        kind = event.get("type")
        item_event = kind in {"response.output_item.added", "response.output_item.done"}
        item = event.get("item") if item_event else None
        if item_event:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                return None
        elif kind not in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            return None

        item_id = (item or {}).get("id") or event.get("item_id")
        item_id = item_id[:256] if isinstance(item_id, str) else ""
        index = event.get("output_index", self._item_indices.get(item_id))
        if index is None and item_event:
            index = max(self._calls, default=-1) + 1
        if type(index) is not int or not 0 <= index < MAX_PREVIEW_CALLS:
            return None
        if item_id and len(self._item_indices) < MAX_PREVIEW_CALLS:
            self._item_indices[item_id] = index
        call = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if item_event:
            for source, target in (("call_id", "id"), ("name", "name")):
                if isinstance(item.get(source), str):
                    call[target] = item[source][:256]
            if isinstance(item.get("arguments"), str):
                call["arguments"] = item["arguments"][:MAX_PREVIEW_ARGUMENTS + 1]
        elif kind == "response.function_call_arguments.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                remaining = MAX_PREVIEW_ARGUMENTS + 1 - len(call["arguments"])
                call["arguments"] += delta[:remaining]
        elif isinstance(event.get("arguments"), str):
            call["arguments"] = event["arguments"][:MAX_PREVIEW_ARGUMENTS + 1]
        return self._snapshots.update(index, call["id"], call["name"], call["arguments"])
