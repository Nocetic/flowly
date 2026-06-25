"""Spawn tool for creating background subagents."""

from typing import Any, TYPE_CHECKING

from flowly.agent.tools.base import Tool

if TYPE_CHECKING:
    from flowly.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """
    Tool to spawn a subagent for background task execution.

    The subagent runs asynchronously and announces its result back
    to the main agent when complete.

    Subagents cannot spawn further subagents (loop prevention).
    """

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._is_subagent = False

    def set_context(self, channel: str, chat_id: str, is_subagent: bool = False) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._is_subagent = is_subagent

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent for background tasks. "
            "The subagent runs asynchronously and reports back when done."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task for the subagent to complete",
                },
                "label": {
                    "type": "string",
                    "description": "Short human-readable label for this task (shown in notifications)",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for this subagent (e.g. 'openrouter/anthropic/claude-haiku-4.5')",
                },
                "cleanup": {
                    "type": "string",
                    "enum": ["keep", "delete"],
                    "description": "Whether to keep or delete the task record after completion (default: keep)",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout in seconds. Default: 600 (10 min). For long tasks (research, document creation) use 900-1800. Only short simple tasks should use less than 300.",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self,
        task: str,
        label: str | None = None,
        model: str | None = None,
        cleanup: str = "keep",
        timeout_seconds: int | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task."""
        # Default 10 min timeout if not specified (prevents LLM from setting too low)
        if timeout_seconds is None or timeout_seconds <= 0:
            timeout_seconds = 600
        # Minimum 120s, maximum 1800s (30 min)
        timeout_seconds = max(120, min(timeout_seconds, 1800))

        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel,
            origin_chat_id=self._origin_chat_id,
            model=model,
            cleanup=cleanup,
            timeout_seconds=timeout_seconds,
            is_subagent_caller=self._is_subagent,
        )
