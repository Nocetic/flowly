"""Agent-facing tools for the Flowly Board.

Four small tools over a shared :class:`BoardStore`:

* ``board_add``    — capture a card (origin channel/chat is taken from the
                     live session context, so a card dropped from Telegram
                     remembers where to report back).
* ``board_list``   — list cards, optionally filtered by status.
* ``board_get``    — fetch one card with its notes.
* ``board_update`` — move a card, edit it, or append a note.

These tools only ever go through ``BoardStore`` (the single writer). They do
not spawn or execute work themselves — running a card is the orchestrator's
job (see ``flowly/board/orchestrator.py``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.board.store import (
    BoardError,
    BoardStore,
    VALID_STATUSES,
)


class _BoardToolBase(Tool):
    """Shared context + store for board tools."""

    def __init__(self, store: BoardStore):
        self._store = store
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Record the active channel/chat so captured cards remember origin."""
        self._channel = channel or ""
        self._chat_id = chat_id or ""

    @staticmethod
    def _err(message: str) -> str:
        return json.dumps({"ok": False, "error": message})


class BoardAddTool(_BoardToolBase):
    name = "board_add"
    description = (
        "Add a card to the user's task board. Use this to capture a task, "
        "reminder, or follow-up the user mentions. The card remembers which "
        "channel it was created from so results can be reported back there. "
        "Returns the created card as JSON."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short, action-oriented card title.",
                },
                "body": {
                    "type": "string",
                    "description": "Optional longer description or context.",
                },
            },
            "required": ["title"],
        }

    async def execute(self, **kwargs: Any) -> str:
        title = kwargs.get("title", "")
        body = kwargs.get("body", "") or ""
        try:
            card = self._store.add_card(
                title,
                body=body,
                origin_channel=self._channel,
                origin_chat_id=self._chat_id,
                created_by="user",
            )
        except BoardError as e:
            return self._err(str(e))
        except Exception as e:  # pragma: no cover - defensive
            logger.error(f"[board_add] {e}")
            return self._err("failed to add card")
        return json.dumps({"ok": True, "card": card.to_dict()})


class BoardListTool(_BoardToolBase):
    name = "board_list"
    description = (
        "List cards on the user's task board. Optionally filter by status "
        "(todo, in_progress, waiting, done, cancelled). Returns cards as JSON."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": sorted(VALID_STATUSES),
                    "description": "Filter to one status. Omit for all cards.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        status = kwargs.get("status")
        try:
            cards = self._store.list_cards(status=status)
        except BoardError as e:
            return self._err(str(e))
        return json.dumps(
            {"ok": True, "cards": [c.to_dict() for c in cards], "count": len(cards)}
        )


class BoardGetTool(_BoardToolBase):
    name = "board_get"
    description = "Fetch a single board card by id, including its notes. Returns JSON."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Card id, e.g. c_a1b2c3d4."},
            },
            "required": ["card_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        card_id = kwargs.get("card_id", "")
        card = self._store.get_card(card_id)
        if card is None:
            return self._err(f"card not found: {card_id}")
        return json.dumps({"ok": True, "card": card.to_dict()})


class BoardUpdateTool(_BoardToolBase):
    name = "board_update"
    description = (
        "Update a board card: move it to a new status, edit its title/body, "
        "and/or append a note. Provide card_id plus any fields to change. "
        "Returns the updated card as JSON."
    )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Card id to update."},
                "status": {
                    "type": "string",
                    "enum": sorted(VALID_STATUSES),
                    "description": "New status for the card.",
                },
                "title": {"type": "string", "description": "New title."},
                "body": {"type": "string", "description": "New body/description."},
                "note": {"type": "string", "description": "Append a note to the card."},
                "result": {
                    "type": "string",
                    "description": "Result summary (typically set when moving to done).",
                },
            },
            "required": ["card_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        card_id = kwargs.get("card_id", "")
        status = kwargs.get("status")
        title = kwargs.get("title")
        body = kwargs.get("body")
        note = kwargs.get("note")
        result = kwargs.get("result")

        if self._store.get_card(card_id) is None:
            return self._err(f"card not found: {card_id}")

        try:
            if title is not None or body is not None:
                self._store.update_card(card_id, title=title, body=body)
            if note:
                self._store.add_note(card_id, author="agent", text=note)
            if status is not None:
                self._store.set_status(card_id, status, result=result)
            elif result is not None:
                # result without a status change — record it as a note
                self._store.add_note(card_id, author="agent", text=f"result: {result}")
            card = self._store.get_card(card_id)
        except BoardError as e:
            return self._err(str(e))
        except Exception as e:  # pragma: no cover - defensive
            logger.error(f"[board_update] {e}")
            return self._err("failed to update card")
        return json.dumps({"ok": True, "card": card.to_dict() if card else None})


class BoardRunTool(_BoardToolBase):
    name = "board_run"
    description = (
        "Execute work on the board. Either run an existing card by id, OR "
        "split a goal into subtasks and run them in PARALLEL as child cards. "
        "Runs in the background; the board updates live and the FINISHED RESULT "
        "is delivered to the user on their channel automatically when done — "
        "exactly like a chat reply. Acknowledge briefly to the user and end "
        "your turn; do NOT call this again or try to fetch the result. Use the "
        "parallel form when a goal breaks into independent pieces (e.g. 'fix "
        "these 5 tests')."
    )

    def __init__(self, store: BoardStore, orchestrator: Any):
        super().__init__(store)
        self._orch = orchestrator
        self._is_subagent = False

    def set_context(self, channel: str, chat_id: str, is_subagent: bool = False) -> None:
        super().set_context(channel, chat_id)
        self._is_subagent = is_subagent

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "description": "Run this existing card. Mutually exclusive with goal/subtasks.",
                },
                "goal": {
                    "type": "string",
                    "description": "A goal to split into parallel subtasks (creates a parent card).",
                },
                "subtasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Independent subtasks to run in parallel under the goal. You decompose.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        if self._orch is None:
            return self._err("board execution is not available")
        if self._is_subagent:
            return self._err("board_run cannot be called from inside a subagent")

        card_id = kwargs.get("card_id")
        goal = kwargs.get("goal")
        subtasks = kwargs.get("subtasks")

        if card_id and (goal or subtasks):
            return self._err("provide either card_id OR goal+subtasks, not both")

        if card_id:
            card = self._store.get_card(card_id)
            if card is None:
                return self._err(f"card not found: {card_id}")
            # Background (async): the orchestrator delivers the finished result
            # straight to the origin channel (deliver=True) — TUI, Telegram,
            # etc. — the same way a chat reply is delivered. No second agent
            # turn, no relay prompt.
            self._spawn_bg(self._orch.run_card(card_id), f"run_card {card_id}")
            return json.dumps({
                "ok": True,
                "status": "started",
                "mode": "single",
                "card_id": card_id,
                "message": (
                    f"Running '{card.title}' in the background. Acknowledge "
                    "briefly to the user and end your turn — the result will be "
                    "delivered to them on this channel when it finishes."
                ),
            })

        if goal and subtasks:
            if not isinstance(subtasks, (list, tuple)) or not subtasks:
                return self._err("subtasks must be a non-empty list")
            self._spawn_bg(
                self._orch.run_goal(
                    goal,
                    list(subtasks),
                    origin_channel=self._channel,
                    origin_chat_id=self._chat_id,
                ),
                f"run_goal {goal[:30]}",
            )
            return json.dumps({
                "ok": True,
                "status": "started",
                "mode": "parallel",
                "subtasks": len(subtasks),
                "message": (
                    f"Decomposed into {len(subtasks)} parallel cards and started "
                    "them. Acknowledge briefly and end your turn — the user will "
                    "be notified on this channel when all finish."
                ),
            })

        return self._err("provide card_id, or goal with subtasks")

    @staticmethod
    def _spawn_bg(coro: Any, label: str) -> None:
        task = asyncio.ensure_future(coro)

        def _done(t: "asyncio.Task") -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(f"[board_run] {label} failed: {exc}")

        task.add_done_callback(_done)


def build_board_tools(
    store: BoardStore, orchestrator: Optional[Any] = None
) -> list[_BoardToolBase]:
    """Construct the board tools sharing one store.

    ``board_run`` is only included when an orchestrator is supplied (it needs
    a way to execute work).
    """
    tools: list[_BoardToolBase] = [
        BoardAddTool(store),
        BoardListTool(store),
        BoardGetTool(store),
        BoardUpdateTool(store),
    ]
    if orchestrator is not None:
        tools.append(BoardRunTool(store, orchestrator))
    return tools
