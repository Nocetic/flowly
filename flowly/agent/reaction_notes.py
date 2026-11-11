"""What the agent is told of the person's reaction on one of its messages.

The Flowly server words it for a chat it keeps (server ``reactions.ts``,
chat-one-log.md §13). For a chat it keeps no copy of (server PROTOCOL A11)
the agent keeps the reaction itself (``flowly/session/reactions.py``), and,
when the server asks it to (``chat.react`` ``tell``), what it is to be told of
it as well: nothing of either is left on the server (2026-11-11). The two
word it the same, so the agent hears the same in either kind of chat.

* A reaction that says something went wrong, or was not understood, starts a
  turn of its own (``wakes``): its note is all the agent is told.
* Any other is held beside the chat and told with the next message the agent
  takes in it, from any device; a newer reaction on the message replaces it
  and taking it off withdraws it.
"""

from __future__ import annotations

from typing import Any

#: The ``hello`` feature of a Core that keeps and tells reactions' notes itself.
FEATURE = "chat-reaction-notes-v1"

#: The quoted message's length in a note (the server's ``EXCERPT``).
EXCERPT = 200

#: Something went wrong, or was not understood: these start a turn (Hakan, 2026-10-27).
_WAKING = frozenset({"👎", "😕", "❌", "🤔", "❓", "❔"})
#: What the wake rule reads past: skin tones and the presentation selector.
_DRESSING = frozenset({"️", *(chr(c) for c in range(0x1F3FB, 0x1F400))})


def _bare(emoji: str) -> str:
    return "".join(ch for ch in emoji if ch not in _DRESSING)


def wakes(emoji: str) -> bool:
    """The reaction asks the agent to answer now."""
    return _bare(emoji) in _WAKING


def reaction_note(emoji: str, text: str) -> str:
    """What the agent reads of the reaction: one sentence, the message it is on quoted short."""
    words = " ".join(text.split()) if isinstance(text, str) else ""
    quoted = f"{words[:EXCERPT - 1]}…" if len(words) > EXCERPT else words
    return f'The user reacted {emoji} to your message: "{quoted}"' if quoted else f"The user reacted {emoji} to your message."


def held_for_turn(sessions: Any, session_key: str, own: list[str]) -> tuple[list[str], list[str]]:
    """The notes a message's turn tells: the chat's held ones (oldest first), then its own.

    ``own``: the message's own notes, validated (``turn_notes``). Answers the
    notes and the held ones' ids, to drop once the turn is taken
    (``reactions.drop_held``); the newest fit when there are more than a
    turn takes (``turn_notes.MAX_NOTES``).
    """
    from flowly.agent.turn_notes import MAX_NOTES
    from flowly.session.reactions import held_notes

    try:
        held = held_notes(sessions, session_key)
    except Exception:  # noqa: BLE001 - a message is never refused for what it would have told
        return own, []
    room = max(0, MAX_NOTES - len(own))
    told = held[-room:] if room else []
    return [h["note"] for h in told] + own, [h["id"] for h in told]
