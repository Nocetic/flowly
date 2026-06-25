"""sessions_list tool — list, cancel, and retry background tasks."""

import json
import time
from typing import Any, TYPE_CHECKING

from flowly.agent.tools.base import Tool

if TYPE_CHECKING:
    from flowly.agent.subagent import SubagentManager
    from flowly.agent.subagent_registry import SubagentRegistry


# Anti-polling window — if the LLM calls `list` more than
# _POLL_HINT_THRESHOLD times within _POLL_WINDOW_S, append a hint to
# the result steering it away from polling. Subagent completion is
# push-based (announce via InboundMessage) so polling is always wrong.
_POLL_WINDOW_S = 60.0
_POLL_HINT_THRESHOLD = 3


class SessionsListTool(Tool):
    """Manage background tasks — list, cancel, retry."""

    def __init__(
        self,
        registry: "SubagentRegistry",
        manager: "SubagentManager | None" = None,
    ):
        self._registry = registry
        self._manager = manager
        # Sliding-window timestamps of recent `list` calls. Pruned on
        # every call — no separate cleanup task needed.
        self._list_call_times: list[float] = []

    @property
    def name(self) -> str:
        return "sessions_list"

    @property
    def description(self) -> str:
        return (
            "Manage background tasks. Actions: "
            "list (show running/completed/failed tasks), "
            "cancel (stop a running task by run_id)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "cancel"],
                    "description": "Action to perform. Default: list.",
                },
                "status": {
                    "type": "string",
                    "enum": ["running", "completed", "failed", "all"],
                    "description": "Filter by status (for list action). Default: all.",
                },
                "run_id": {
                    "type": "string",
                    "description": "Task ID to cancel (first 8 chars sufficient).",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        action: str = "list",
        status: str = "all",
        run_id: str = "",
        **kwargs: Any,
    ) -> str:
        if action == "cancel":
            return await self._cancel(run_id)
        return self._list(status)

    def _list(self, status: str) -> str:
        # Record this call and prune timestamps older than the window.
        now = time.time()
        self._list_call_times = [
            t for t in self._list_call_times if now - t < _POLL_WINDOW_S
        ]
        self._list_call_times.append(now)
        recent_calls = len(self._list_call_times)

        runs = self._registry.all()

        if not runs:
            return self._maybe_append_poll_hint("No background tasks found.", recent_calls)

        filtered = []
        for r in runs:
            if status == "running" and r.ended_at is not None:
                continue
            if status == "completed" and r.outcome != "ok":
                continue
            if status == "failed" and r.outcome not in ("error", "timeout"):
                continue
            filtered.append(r)

        if not filtered:
            return self._maybe_append_poll_hint(
                f"No tasks with status '{status}'.", recent_calls,
            )

        lines = [f"Background tasks ({len(filtered)}):"]
        for r in sorted(filtered, key=lambda x: x.created_at, reverse=True):
            if r.ended_at is None:
                state = "⏳ running"
            elif r.outcome == "ok":
                state = "✓ completed"
            elif r.outcome == "timeout":
                state = "⏰ timed out"
            elif r.outcome == "cancelled":
                state = "🛑 cancelled"
            else:
                state = "✗ failed"

            duration = ""
            if r.started_at and r.ended_at:
                secs = int(r.ended_at - r.started_at)
                duration = f" ({secs}s)"

            lines.append(f"  {state}{duration} — {r.label} [{r.run_id[:8]}]")
            if r.error:
                lines.append(f"    error: {r.error}")

        return self._maybe_append_poll_hint("\n".join(lines), recent_calls)

    def _maybe_append_poll_hint(self, body: str, recent_calls: int) -> str:
        """Append an anti-polling hint if the LLM is re-listing in a loop."""
        if recent_calls < _POLL_HINT_THRESHOLD:
            return body
        return (
            f"{body}\n\n"
            f"⚠️ sessions_list has been called {recent_calls} times in "
            f"the last {int(_POLL_WINDOW_S)}s. Background task completion "
            "is push-based — when a task finishes, its result is delivered "
            "automatically as a system message. Do not keep polling; "
            "continue with other work or respond to the user."
        )

    async def _cancel(self, run_id: str) -> str:
        if not run_id:
            return "Error: run_id is required to cancel a task."

        if not self._manager:
            return "Error: Task cancellation not available."

        result = await self._manager.cancel(run_id)
        return result
