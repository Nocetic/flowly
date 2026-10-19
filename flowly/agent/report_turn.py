"""A subagent's report, run as its conversation's own live turn.

When subagents finish, the conversation that started them takes a turn of its
own to answer the owner with their results (the *report turn*). Nobody typed
it, so no client started it with ``chat.send``. To stream like a typed turn it
goes through the surface's own chat runner — the direct gateway's
``_run_chat`` or the relay channel's ``chat.send`` machinery — exactly like a
standing goal's next turn: run id, command record, in-flight entry, live text,
live tool steps, one final, Stop.

This module holds the small contract those runners and the agent share, so
neither has to import the other.

Wire (docs/chat-wire-protocol.md):

* ``{"event": "chat", "data": {"state": "run_started", "runId", "sessionKey",
  "turnOrigin": "subagent", "hiddenPrompt": true, "steerable": false}}`` once
  the turn holds its conversation's turn lock;
* every event of the run (``agent`` deltas, ``iteration_step``,
  ``tool_progress``, ``final``, ``error``) carries ``turnOrigin`` and
  ``sessionKey``, so a client that missed ``run_started`` still knows whose
  run it is.

The notice that starts the turn is never shown: the transcript keeps it as a
hidden row and the in-flight entry has no user text.
"""

from __future__ import annotations

from typing import Any

# Metadata marker on the inbound turn: ``{"sender_id", "chat_id"}`` of the
# system notice it carries. Its presence means a runner owns the delivery.
REPORT_TURN = "_subagent_report_turn"

# ``turnOrigin`` of a report turn on the wire and in the command record.
REPORT_TURN_ORIGIN = "subagent"


def report_turn_fields(session_key: str) -> dict[str, Any]:
    """Fields every event of a report run carries."""
    return {"turnOrigin": REPORT_TURN_ORIGIN, "sessionKey": session_key}


def run_started_event(run_id: str, session_key: str) -> dict[str, Any]:
    """The ``chat`` event data that opens a report run on a client."""
    return {
        "state": "run_started",
        "runId": run_id,
        **report_turn_fields(session_key),
        # The trigger is the agent's own notice; there is no user row.
        "hiddenPrompt": True,
        # What the owner writes meanwhile is their next turn, not guidance
        # into this one (see AgentLoop._process_system_message).
        "steerable": False,
    }


def report_notice(sender_id: str, chat_id: str) -> dict[str, str]:
    """The marker value: who sent the notice and where the reply goes."""
    return {"sender_id": sender_id, "chat_id": chat_id}
