"""Noticing a turn that keeps asking for what it already has.

A model can fall into calling the same tool again and again and getting the
same answer each time — re-reading three finished subagent results thirty
times in a row, polling something that has stopped changing. Nothing failed,
so error-based checks see a healthy turn and say "keep going".

The guard watches results, not arguments: a result identical to one this
turn has already received is marked as such for the model, and once several
results in a row bring nothing new, the next model call is made without
tools so the turn ends with an answer instead of another lap.
"""

from __future__ import annotations

import hashlib

# Results in a row that repeat an earlier one before the turn must answer.
STALL_AFTER = 4

ANSWER_NOW = (
    "Your last {count} tool calls returned results you already had in this "
    "turn — nothing new is coming from repeating them. Tools are off for this "
    "reply: answer the owner now with what you have. If something is still "
    "missing or still running, say what and stop there."
)


class RepetitionGuard:
    """Per-turn record of tool results, by tool and exact content."""

    def __init__(self, stall_after: int = STALL_AFTER) -> None:
        self._stall_after = stall_after
        self._seen: set[tuple[str, str]] = set()
        self._in_a_row = 0

    def observe(self, tool_name: str, result: object) -> bool:
        """Record one result; True when this turn already received it."""
        if not isinstance(result, str):
            self._in_a_row = 0
            return False
        key = (tool_name, hashlib.sha256(result.encode("utf-8", "replace")).hexdigest())
        if key in self._seen:
            self._in_a_row += 1
            return True
        self._seen.add(key)
        self._in_a_row = 0
        return False

    @property
    def stalled(self) -> bool:
        return self._in_a_row >= self._stall_after

    @property
    def repeats_in_a_row(self) -> int:
        return self._in_a_row


def mark_repeated(tool_name: str, content: str) -> str:
    """The result as the model sees it when it has already had it."""
    return (
        f"[Identical to an earlier {tool_name} result in this turn — nothing "
        "has changed. Don't call it again for the same thing; use what you "
        "have.]\n" + content
    )
