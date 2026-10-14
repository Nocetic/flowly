"""Spawn tool: start a subagent for a task the agent describes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from flowly.agent.delegation import current_turn, render_conversation, spawn_origin
from flowly.agent.tools.base import Tool

if TYPE_CHECKING:
    from flowly.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """Start a subagent that works in the background and reports back.

    Whether a call may start one is decided by :mod:`flowly.agent.delegation`
    from facts the runtime bound for this turn; the model's arguments alone
    can never authorise it. Subagents cannot spawn further subagents.
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
            "Start a subagent: a separate model run that works on a task in the "
            "background and reports back to you. Each one costs a full run of its "
            "own. Whether to use one is your call, or the owner's when they ask "
            "for it — then pass their exact words as owner_request. Subagents "
            "started in the same turn report back together, once all have "
            "finished."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "Short name the owner sees for this subagent, in their "
                        "language (e.g. 'Rakip B fiyatları')."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "The full brief. The subagent sees the conversation so far "
                        "but nothing of your tool work, so state: the goal, what "
                        "you already know or ruled out, the sources or files to "
                        "use, what is out of scope, and what to return. Ask for "
                        "verifiable references (URLs, file paths, ids) for any "
                        "claim or change."
                    ),
                },
                "owner_request": {
                    "type": "string",
                    "description": (
                        "Only when the owner asked for a subagent: their exact "
                        "words asking for it, copied from their message. Omit "
                        "otherwise."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Only when the owner named a model for this task. Omit to "
                        "run on your own model."
                    ),
                },
                "context": {
                    "type": "string",
                    "enum": ["conversation", "none"],
                    "description": (
                        "'conversation' (default): the subagent reads the owner's "
                        "messages and your replies so far. 'none': it starts from "
                        "the brief alone — for work unrelated to this conversation."
                    ),
                },
            },
            "required": ["title", "task"],
        }

    async def execute(
        self,
        task: str,
        title: str | None = None,
        owner_request: str | None = None,
        model: str | None = None,
        context: str = "conversation",
        label: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Start a subagent."""
        turn = current_turn()

        chosen_model = (model or "").strip() or (turn.model if turn else None)
        transcript = None
        if context != "none" and turn and turn.conversation:
            transcript = render_conversation(turn.conversation)

        channel = (turn.origin_channel if turn else "") or self._origin_channel
        chat_id = (turn.origin_chat_id if turn else "") or self._origin_chat_id
        return await self._manager.spawn(
            parent_session_key=(turn.session_key if turn else "") or None,
            task=task,
            label=label,
            display_name=(title or "").strip() or None,
            origin_channel=channel,
            origin_chat_id=chat_id,
            model=chosen_model,
            is_subagent_caller=self._is_subagent,
            origin=spawn_origin(owner_request, turn),
            context=transcript,
            batch_id=turn.turn_id if turn else "",
        )
