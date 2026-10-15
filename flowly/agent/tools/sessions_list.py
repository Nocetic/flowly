"""sessions_list tool — list, cancel, and retry background tasks."""

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
            "Manage this conversation's background tasks. Actions: "
            "list (show this conversation's subagents — open or closed, working "
            "or idle, how many tasks, the latest result — and its running/"
            "completed/failed tasks), "
            "cancel (stop a running task by run_id, or run_id=\"all\" to stop "
            "every task this conversation has running, in one call; the subagent "
            "stays open). Results reach you as a message; don't list repeatedly "
            "to wait for them."
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
                    "description": (
                        "Task ID to cancel (first 8 chars sufficient), or \"all\"."
                    ),
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

        # Only this conversation's tasks: another chat's work is not this
        # conversation's business, and listing it confuses the agent.
        runs = [r for r in self._registry.all() if self._is_own(r)]

        if not runs:
            return self._maybe_append_poll_hint("No background tasks found.", recent_calls)
        subagents = self._subagent_lines(runs) if status == "all" else []

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

        lines = [*subagents, f"Background tasks ({len(filtered)}):"]
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

            task = f", task {r.turn}" if r.thread_id and r.turn > 1 else ""
            lines.append(f"  {state}{duration} — {r.display_name or r.label} [{r.run_id[:8]}{task}]")
            if r.error:
                lines.append(f"    error: {r.error}")

        return self._maybe_append_poll_hint("\n".join(lines), recent_calls)

    def _subagent_lines(self, runs: list[Any]) -> list[str]:
        """This conversation's subagents (threads): one line each, newest first."""
        threads_of = getattr(self._registry, "threads", None)
        if threads_of is None:
            return []
        parents = {r.parent_session_key for r in runs}
        threads = [t for t in threads_of() if t.parent_session_key in parents]
        if not threads:
            return []
        by_id = {r.run_id: r for r in runs}
        lines = [f"Subagents ({len(threads)}):"]
        for thread in sorted(threads, key=lambda t: t.created_at, reverse=True):
            if thread.status == "open":
                state = "open, working" if thread.active_run_id else "open, idle"
            else:
                state = f"closed ({thread.closed_reason or 'closed'})"
            count = len(thread.runs)
            line = (f"  '{thread.title}' [agent_id {thread.thread_id[:8]}] — {state}, "
                    f"{count} task{'s' if count != 1 else ''}")
            ended = [by_id[rid] for rid in thread.runs if rid in by_id and by_id[rid].ended_at is not None]
            if ended:
                last = ended[-1]
                if last.outcome == "cancelled" and getattr(last, "stopped_by", "") == "owner":
                    line += "; its latest task was stopped by the owner, with no result"
                else:
                    preview = " ".join((last.result_preview or last.error or "").split())[:160]
                    if preview:
                        line += f"; latest result: {preview}"
            lines.append(line)
        lines.append("")
        return lines

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

        if run_id.strip().lower() == "all":
            running = [r for r in self._registry.all()
                       if r.ended_at is None and r.origin != "system" and self._is_own(r)]
            if not running:
                return "No task of this conversation is running."
            stopped = []
            for record in running:
                await self._manager.cancel(record.run_id)
                stopped.append(record.display_name or record.label)
            return f"Stopped {len(stopped)} task(s): " + ", ".join(stopped)

        matches = [r for r in self._registry.all() if r.run_id.startswith(run_id.strip())]
        if matches and not any(self._is_own(r) for r in matches):
            return f"Error: No task {run_id} in this conversation."
        result = await self._manager.cancel(run_id)
        return result

    @staticmethod
    def _is_own(record: Any) -> bool:
        from flowly.agent.tool_context import current_tool_origin

        origin = current_tool_origin()
        if origin is None:
            return True  # an owner-level caller (CLI) sees every task
        return record.parent_session_key == origin.session_key
