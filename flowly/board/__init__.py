"""Flowly Board — cross-channel personal task board.

A single-writer SQLite board. Cards are captured from any channel, moved
through a simple lifecycle, and (optionally) executed by the agent. The
store is the only component that writes ``board.db``; subagents never
touch it (their results flow back through the normal subagent completion
path and are written here by the orchestrator / gateway). That invariant
is what lets the board stay lock-free — see ``BOARD_PLAN.md``.
"""

from flowly.board.store import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_TODO,
    STATUS_WAITING,
    VALID_STATUSES,
    BoardError,
    BoardStore,
    Card,
    CardNote,
)

__all__ = [
    "BoardStore",
    "Card",
    "CardNote",
    "VALID_STATUSES",
    "STATUS_TODO",
    "STATUS_IN_PROGRESS",
    "STATUS_WAITING",
    "STATUS_DONE",
    "STATUS_CANCELLED",
    "BoardError",
]
