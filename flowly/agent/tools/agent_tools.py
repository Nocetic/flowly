"""Tools the agent uses to work with the subagents it started."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from flowly.agent.tool_context import current_tool_origin
from flowly.agent.tools.base import Tool

if TYPE_CHECKING:
    from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord

_READ_DEFAULT_CHARS = 8_000
_READ_MAX_CHARS = 20_000
_MIN_ID_PREFIX = 8


def _status(record: "SubagentRunRecord") -> str:
    return "running" if record.ended_at is None else (record.outcome or "unknown")


def resolve_own_run(
    registry: "SubagentRegistry", run_id: str, session_key: str | None,
) -> tuple["SubagentRunRecord | None", str]:
    """Find a run this conversation started, by full id or unique prefix.

    Another conversation's run reads as "not found": its existence is not
    this conversation's business.
    """
    wanted = (run_id or "").strip()
    if len(wanted) < _MIN_ID_PREFIX:
        return None, "Give the run_id from the subagent's start or completion message."
    candidates = [
        record for record in registry.all()
        if record.run_id == wanted or record.run_id.startswith(wanted)
    ]
    if session_key is not None:
        candidates = [r for r in candidates if r.parent_session_key == session_key]
    if not candidates:
        return None, f"No subagent run {wanted} in this conversation."
    if len(candidates) > 1:
        return None, f"run_id {wanted} matches several runs; give the full id."
    return candidates[0], ""


class AgentReadTool(Tool):
    """Read a subagent's status and full result."""

    def __init__(self, registry: "SubagentRegistry"):
        self._registry = registry

    @property
    def name(self) -> str:
        return "agent_read"

    @property
    def description(self) -> str:
        return (
            "Read a subagent's status and its full result, page by page. Use it "
            "when the completion message says more text is available, or to "
            "check on a subagent you started. Don't poll in a loop: completion "
            "reaches you as a message."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The subagent's run_id."},
                "offset": {
                    "type": "integer",
                    "description": "Character offset to read from (default 0).",
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": f"Characters to read (default {_READ_DEFAULT_CHARS}).",
                    "minimum": 1,
                    "maximum": _READ_MAX_CHARS,
                },
            },
            "required": ["run_id"],
        }

    async def execute(
        self, run_id: str, offset: int = 0, limit: int = _READ_DEFAULT_CHARS, **kwargs: Any,
    ) -> str:
        origin = current_tool_origin()
        record, problem = resolve_own_run(
            self._registry, run_id, origin.session_key if origin else None,
        )
        if record is None:
            return json.dumps({"error": problem})
        try:
            offset = max(0, int(offset))
            limit = max(1, min(int(limit), _READ_MAX_CHARS))
        except (TypeError, ValueError):
            return json.dumps({"error": "offset and limit must be whole numbers."})

        view: dict[str, Any] = {
            "run_id": record.run_id,
            "title": record.display_name or record.label,
            "status": _status(record),
            "model": record.model,
        }
        if record.error:
            view["error"] = record.error
        if record.result_available:
            try:
                page = self._registry.read_result(record.run_id, offset, limit)
            except (FileNotFoundError, ValueError) as exc:
                view["result_error"] = str(exc)
            else:
                view.update(
                    content=page["content"],
                    offset=page["offset"],
                    next_offset=page["nextOffset"],
                    total_chars=page["totalChars"],
                )
        elif record.ended_at is None:
            view["note"] = "Still running; its result will reach you as a message."
        return json.dumps(view, ensure_ascii=False)
