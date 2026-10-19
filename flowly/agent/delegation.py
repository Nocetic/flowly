"""Who started a subagent, and what the child is told.

The agent decides for itself whether a subagent is worth it; the runtime
never pushes it either way. What the runtime does check is who asked: a
subagent counts as the owner's when the agent quotes the owner's own words
and the quote is found in what the owner actually wrote this turn — so a
tool result or a guess can never stand in for the owner. Every other
subagent is the agent's own. Limits on how many may run at once are budget
guards (see ``subagent.py``), not a rule about when to delegate.

The turn facts live in context variables bound by the runtime around a turn,
never in tool arguments the model controls, and never on shared tool objects
(one loop serves many conversations at once).
"""

from __future__ import annotations

import contextvars
import re
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Literal

Origin = Literal["owner", "agent", "system"]

# Children a single conversation may have running at once (owner- or
# agent-started). System work (self-review, Board, cron) is not counted here.
MAX_CHILDREN_PER_CONVERSATION = 20
# Past this many running at once, a start's result says how many run (each
# uses its own model calls), so the agent can tell the owner. A fact, not a limit.
MANY_CHILDREN_RUNNING = 10

# A quote shorter than this proves nothing ("ver", "ok").
_MIN_QUOTE_CHARS = 6

# How the runtime stores a system trigger in a conversation's user slot.
_SYSTEM_ROW_PREFIX = "[System:"

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
    # arriving, a goal continuation, a system notice): nothing in it can
    # count as an owner request.
    owner_authored: bool = True
    # Where a subagent started in this turn reports back. Carried with the
    # turn, never read from the shared tool object, so two conversations
    # running at once cannot swap each other's children.
    origin_channel: str = ""
    origin_chat_id: str = ""
    # The conversation's own key — what its history, turn lock and goal are
    # stored under. A desktop chat's key is not ``channel:chat_id``.
    session_key: str = ""
    # True when the turn should stop waiting and look up: the owner wrote
    # something new into it, or stopped it.
    should_yield: Callable[[], bool] | None = None
    # Whether what the owner writes during the turn reaches it (an owner's
    # turn) or waits for the next one (a turn a finished subagent started).
    steerable: bool = True
    # Subagents started in the same turn report back together, once the
    # last of them has finished.
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Seconds this turn has spent in agent_wait (mutable, the turn's own).
    waited: list[float] = field(default_factory=lambda: [0.0], compare=False)


_turn: contextvars.ContextVar[DelegationTurn | None] = contextvars.ContextVar(
    "flowly_delegation_turn", default=None,
)


def current_turn() -> DelegationTurn | None:
    return _turn.get()


@contextmanager
def delegation_turn_scope(turn: DelegationTurn) -> Iterator[None]:
    token = _turn.set(turn)
    try:
        yield
    finally:
        _turn.reset(token)


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


def spawn_origin(owner_request: str | None, turn: DelegationTurn | None) -> Origin:
    """"owner" when the quoted request is the owner's own words this turn."""
    return "owner" if owner_quote_is_genuine(owner_request, turn) else "agent"


def conversation_from_history(
    history: list[dict[str, Any]] | None, current_owner_text: str = "",
) -> tuple[tuple[str, str], ...]:
    """The owner/agent exchange a child may read, oldest first.

    Keeps what was said and answered; drops tool calls, tool output and
    reasoning, the way a forked conversation should look to a newcomer.
    """
    from flowly.compaction.types import is_summary_message

    pairs: list[tuple[str, str]] = []
    for message in history or ():
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if is_summary_message(message):
            pairs.append(("summary", content.strip()))
            continue
        if role not in ("user", "assistant"):
            continue
        if role == "assistant" and message.get("tool_calls"):
            continue
        if role == "user" and (
            message.get("_display_hidden") or content.lstrip().startswith(_SYSTEM_ROW_PREFIX)
        ):
            # A subagent's report or a system notice the runtime stored in
            # the user slot: not the owner's words, and possibly web-derived.
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
        speaker = {"user": "Owner", "assistant": "Agent"}.get(role, "Summary of earlier conversation")
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

