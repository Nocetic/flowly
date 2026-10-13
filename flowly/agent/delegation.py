"""When the agent may hand work to a subagent, and what the child is told.

The agent does its work itself. It starts a subagent only when

* the owner asked for one — the agent quotes the owner's own words and the
  runtime checks the quote against what the owner actually wrote this turn, so
  a tool result or a guess can never stand in for the owner; or
* the work splits into independent parts — two or more ``spawn`` calls in the
  same response, one per part.

A single unrequested ``spawn`` is refused with a message telling the agent to
do the task itself. There is no word list: the model decides what the owner
means, the runtime only checks evidence it can verify.

The turn facts live in context variables bound by the runtime around a turn,
never in tool arguments the model controls, and never on shared tool objects
(one loop serves many conversations at once).
"""

from __future__ import annotations

import contextvars
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Literal

Origin = Literal["owner", "agent", "system"]

# Children a single conversation may have running at once (owner- or
# agent-started). System work (self-review, Board, cron) is not counted here.
MAX_CHILDREN_PER_CONVERSATION = 3

# A quote shorter than this proves nothing ("ver", "ok").
_MIN_QUOTE_CHARS = 6

# Conversation handed to a child: the most recent exchange, bounded.
_CONTEXT_MAX_CHARS = 24_000
_CONTEXT_MESSAGE_MAX_CHARS = 4_000


@dataclass(frozen=True)
class DelegationTurn:
    """What the runtime knows about the turn a ``spawn`` call belongs to."""

    owner_text: str = ""
    # (role, text) pairs, oldest first: the owner's messages and the agent's
    # replies — no tool calls, tool output or reasoning.
    conversation: tuple[tuple[str, str], ...] = ()
    model: str | None = None
    # False when the turn was not written by the owner (a subagent's result
    # arriving, a system notice): nothing in it can count as an owner request.
    owner_authored: bool = True


@dataclass(frozen=True)
class DelegationDecision:
    allowed: bool
    origin: Origin = "agent"
    reason: str = ""


_turn: contextvars.ContextVar[DelegationTurn | None] = contextvars.ContextVar(
    "flowly_delegation_turn", default=None,
)
_batch: contextvars.ContextVar[int] = contextvars.ContextVar(
    "flowly_spawn_batch", default=0,
)


def current_turn() -> DelegationTurn | None:
    return _turn.get()


def current_batch_size() -> int:
    return _batch.get()


@contextmanager
def delegation_turn_scope(turn: DelegationTurn) -> Iterator[None]:
    token = _turn.set(turn)
    try:
        yield
    finally:
        _turn.reset(token)


@contextmanager
def spawn_batch_scope(size: int) -> Iterator[None]:
    """Bind how many ``spawn`` calls the current model response holds."""
    token = _batch.set(max(0, int(size)))
    try:
        yield
    finally:
        _batch.reset(token)


def count_spawn_calls(tool_calls: Any) -> int:
    return sum(1 for call in tool_calls or () if getattr(call, "name", "") == "spawn")


def begin_spawn_batch(tool_calls: Any) -> None:
    """Record how many ``spawn`` calls the response being executed holds.

    Called by the loop before it runs a response's tool calls; each response
    overwrites the previous value, so it never leaks into the next one.
    """
    _batch.set(count_spawn_calls(tool_calls))


def _normalize(text: str) -> str:
    # Turkish dotted/dotless i fold to one letter: "LUNA'YA" and "Luna'ya",
    # "AGENT'INA" and "agent'ına" are the same words to the owner.
    text = (text or "").replace("İ", "i").replace("I", "i").replace("ı", "i")
    text = unicodedata.normalize("NFKC", text).casefold()
    # Quotes the model wraps around a citation, and smart punctuation, are not
    # part of what the owner wrote.
    text = re.sub(r"[\"'“”‘’«»`]", "", text)
    return re.sub(r"\s+", " ", text).strip(" .,:;!?-—")


def owner_quote_is_genuine(quote: str | None, turn: DelegationTurn | None) -> bool:
    """True when ``quote`` is something the owner really wrote this turn."""
    if not quote or turn is None or not turn.owner_authored:
        return False
    needle = _normalize(quote)
    if len(needle) < _MIN_QUOTE_CHARS:
        return False
    return needle in _normalize(turn.owner_text)


def decide(owner_request: str | None, *, batch_size: int, turn: DelegationTurn | None) -> DelegationDecision:
    """Apply the delegation rule to one ``spawn`` call."""
    if owner_quote_is_genuine(owner_request, turn):
        return DelegationDecision(True, "owner")
    if batch_size >= 2:
        return DelegationDecision(True, "agent")
    if owner_request:
        reason = (
            "Not started: the owner_request you gave is not in the owner's message "
            "this turn. Quote their words exactly, or do the task yourself."
        )
    else:
        reason = (
            "Not started — do this task yourself. Start a subagent only when the "
            "owner asked for one (pass their exact words as owner_request), or for "
            "two or more independent parts of the work, one spawn call each in the "
            "same response."
        )
    return DelegationDecision(False, reason=reason)


def conversation_from_history(
    history: list[dict[str, Any]] | None, current_owner_text: str = "",
) -> tuple[tuple[str, str], ...]:
    """The owner/agent exchange a child may read, oldest first.

    Keeps what was said and answered; drops tool calls, tool output and
    reasoning, the way a forked conversation should look to a newcomer.
    """
    pairs: list[tuple[str, str]] = []
    for message in history or ():
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "assistant" and message.get("tool_calls"):
            continue
        pairs.append((role, content.strip()))
    if current_owner_text.strip():
        pairs.append(("user", current_owner_text.strip()))
    return tuple(pairs)


def render_conversation(conversation: tuple[tuple[str, str], ...]) -> str:
    """Bounded, newest-kept text of the exchange for a child's first message."""
    lines: list[str] = []
    used = 0
    for role, text in reversed(conversation):
        if len(text) > _CONTEXT_MESSAGE_MAX_CHARS:
            text = text[:_CONTEXT_MESSAGE_MAX_CHARS] + " […]"
        speaker = "Owner" if role == "user" else "Agent"
        line = f"**{speaker}:** {text}"
        if used + len(line) > _CONTEXT_MAX_CHARS and lines:
            lines.append("[earlier messages left out]")
            break
        lines.append(line)
        used += len(line)
    return "\n\n".join(reversed(lines))


# ── Marks: the shape and colour a subagent wears in every client ─────────────

MARK_GLYPHS = (
    "hex", "tri", "diamond", "target", "spark", "orbit",
    "trio", "wave", "leaf", "plus", "penta", "knot",
)
MARK_HUES = (212, 262, 330, 8, 28, 45, 88, 142, 168, 190, 238, 296)


def _fnv1a(text: str) -> int:
    value = 0x811C9DC5
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


def assign_mark(run_id: str, sibling_glyphs: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """A stable shape and hue for ``run_id``.

    Siblings running in the same conversation never share a shape while the
    set has room: when the hashed shape is taken the next free one is used.
    """
    digest = _fnv1a(f"subagent:{run_id}")
    index = digest % len(MARK_GLYPHS)
    for _ in range(len(MARK_GLYPHS)):
        if MARK_GLYPHS[index] not in sibling_glyphs:
            break
        index = (index + 1) % len(MARK_GLYPHS)
    return {"glyph": MARK_GLYPHS[index], "hue": MARK_HUES[(digest >> 4) % len(MARK_HUES)]}

