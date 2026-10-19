"""Noticing a turn that keeps asking for what it already has.

A model can fall into calling the same tool again and again and getting the
same answer each time — re-reading three finished subagent results thirty
times in a row, polling something that has stopped changing. Nothing failed,
so error-based checks see a healthy turn and say "keep going".

The guard watches results, not arguments: a result that says the same as one
this turn has already received — identical once clock readings, dates and
elapsed times are set aside — is marked as such for the model, and once
several results in a row bring nothing new, the next model call is made
without tools so the turn ends with an answer instead of another lap.
"""

from __future__ import annotations

import hashlib
import re

# Results in a row that repeat an earlier one before the turn must answer.
STALL_AFTER = 4

# What changes on every call without the result saying anything new: clock
# readings, dates, elapsed times. Replaced before results are compared, so a
# poll that only reports "checked at 14:02:31 (1.2s)" again is a repeat.
_VOLATILE = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s?[AP]M)?\b", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|milliseconds?|s|secs?|seconds?|mins?|minutes?)\b", re.IGNORECASE),
    re.compile(r"\b1[6-9]\d{8}(?:\.\d+)?\b"),  # Unix time in seconds
    re.compile(r"\b1[6-9]\d{11}\b"),  # ... and in milliseconds
]


def _comparable(text: str) -> str:
    for pattern in _VOLATILE:
        text = pattern.sub("\u2027", text)
    return text


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
        comparable = _comparable(result)
        key = (tool_name, hashlib.sha256(comparable.encode("utf-8", "replace")).hexdigest())
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
