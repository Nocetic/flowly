"""Current compaction state per session, for clients that arrive mid-run.

Compaction is announced with events. A client that was not connected when the
event fired — a chat reopened, a device that just woke, a socket that
reconnected — has no way to learn that the agent is busy summarising, so it
shows an idle chat while the turn is visibly stalled.

``chat.inflight`` is the re-entry handshake that already restores the
streaming bubble and the plan snapshot. This module is what lets it restore
the compaction notice too: the loop records state here as it emits each
phase, and the handshake reads it back.

In-process and deliberately small. Compaction state is live, not history — a
finished compaction is remembered only long enough for a client re-entering
moments later to catch up, and the durable record is the session itself.
"""

from typing import Any

# session_key -> the last phase payload, minus anything a client can't use.
_STATE: dict[str, dict[str, Any]] = {}

# A completed/failed notice this old is no longer news. Long enough to cover a
# reconnect or a tab switch, short enough that reopening a chat later doesn't
# resurrect a stale banner.
TERMINAL_TTL_SECONDS = 60.0

# A "started" that is never closed would otherwise be reported forever, so a
# client re-entering hours later would find a compaction still running. Every
# path is supposed to emit a terminal — this is the backstop for the one that
# does not, e.g. a process killed mid-summary. Generous: a staged summary of a
# huge history is minutes of real work.
RUNNING_MAX_AGE_SECONDS = 900.0

MAX_TRACKED = 200


def record(
    session_key: str,
    phase: str,
    tokens_before: int,
    tokens_after: int,
    messages_removed: int,
    now: float,
    compaction_id: str = "",
) -> None:
    """Note the phase a session's compaction just reached.

    ``compaction_id`` travels with it so a client restoring state through
    ``chat.inflight`` can tell whether a later ``completed`` closes the cycle
    it is showing or belongs to a different pass.
    """
    if not session_key:
        return
    _STATE[session_key] = {
        "phase": phase,
        "tokensBefore": int(tokens_before or 0),
        "tokensAfter": int(tokens_after or 0),
        "messagesRemoved": int(messages_removed or 0),
        "sessionKey": session_key,
        "at": float(now),
        **({"compactionId": compaction_id} if compaction_id else {}),
    }
    if len(_STATE) > MAX_TRACKED:
        for key in list(_STATE)[: len(_STATE) - MAX_TRACKED]:
            _STATE.pop(key, None)


def get(session_key: str, now: float) -> dict[str, Any] | None:
    """The state a re-entering client should render, or None.

    A run still in progress is always reported. A terminal phase is reported
    only while it is recent — otherwise every reopened chat would greet the
    user with a summarisation that happened yesterday.
    """
    state = _STATE.get(session_key)
    if not state:
        return None
    if state["phase"] == "started":
        if now - state["at"] > RUNNING_MAX_AGE_SECONDS:
            # Nobody closed it. Reporting it forever means a chat opened
            # tomorrow still shows a spinner for work that died today.
            _STATE.pop(session_key, None)
            return None
        return {k: v for k, v in state.items() if k != "at"}
    if now - state["at"] <= TERMINAL_TTL_SECONDS:
        return {k: v for k, v in state.items() if k != "at"}
    _STATE.pop(session_key, None)
    return None


def clear(session_key: str) -> None:
    _STATE.pop(session_key, None)
