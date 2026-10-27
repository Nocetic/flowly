"""Notes the Flowly app adds to a turn: what the agent should know that the
person did not say (``chat.send`` ``notes``; flowly-server chat-one-log.md §13).

The person's reaction on one of the agent's messages is the first: "the user
reacted 👍 to your message …". It used to be glued in front of the person's
words, and everything that reads the turn back — the stored user row, the
in-flight entry a re-entering app restores, the history an app shows —
showed it as something the person wrote (2026-10-27, a purple bubble of
``[The user reacted ❤️ …]`` lines in Rocket Scientist's chat).

So notes travel apart from the words and stay apart:

* with the person's message, they ride on the model's current message as a
  sidecar (like ``turn_clock``) and are never stored as the person's words;
* alone (a reaction that asks the agent to answer, with nothing typed), they
  are the turn's prompt, app-authored like an agent's introduction: kept in
  the model's context as a hidden row, never shown, and the in-flight entry
  carries no user text.

A message that carries notes keeps its own turn: joining a running one
(``run_steering``) would pass on only the words.
"""

from __future__ import annotations

from typing import Any

#: Inbound metadata: the notes for this turn, validated (``turn_notes``).
TURN_NOTES = "_turn_notes"

#: Inbound metadata: the turn has notes and no words of the person's.
NOTES_ONLY_TURN = "_notes_only_turn"

#: ``system.capabilities`` field: this Core takes ``chat.send`` ``notes``.
CHAT_NOTES_VERSION = 1

MAX_NOTES = 20
MAX_NOTE_CHARS = 1000


def turn_notes(params: dict[str, Any]) -> list[str]:
    """The ``notes`` of a ``chat.send``: non-empty strings, at most
    ``MAX_NOTES`` of ``MAX_NOTE_CHARS`` each. Absent is none; any other shape
    is refused (ValueError), never half-read."""
    raw = params.get("notes")
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_NOTES:
        raise ValueError(f"notes must be a list of at most {MAX_NOTES} strings.")
    notes: list[str] = []
    for note in raw:
        if not isinstance(note, str) or not note.strip() or len(note) > MAX_NOTE_CHARS:
            raise ValueError(f"Each note must be a non-empty string of at most {MAX_NOTE_CHARS} characters.")
        notes.append(note.strip())
    return notes


def notes_block(notes: list[str]) -> str:
    """What the model reads of the notes: from the app, unseen by the person."""
    lines = "\n".join(f"- {note}" for note in notes)
    return (
        "<app_notes>\n"
        "From the Flowly app, not from the user; the user does not see these notes. "
        "Never quote or repeat them.\n"
        f"{lines}\n"
        "</app_notes>"
    )


def inbound_metadata(params: dict[str, Any], message: str, has_files: bool) -> dict[str, Any]:
    """The inbound metadata a ``chat.send``'s notes add (empty without notes)."""
    notes = turn_notes(params)
    if not notes:
        return {}
    if not message.strip() and not has_files:
        return {TURN_NOTES: notes, NOTES_ONLY_TURN: True}
    return {TURN_NOTES: notes}


def model_prompt(metadata: dict[str, Any]) -> str:
    """The notes as the model reads them, or "" when the turn has none."""
    notes = metadata.get(TURN_NOTES)
    return notes_block(notes) if isinstance(notes, list) and notes else ""


def is_notes_only(metadata: dict[str, Any]) -> bool:
    return bool(metadata.get(NOTES_ONLY_TURN))
