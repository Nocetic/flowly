"""In-flight assistant stream registry.

Streaming is otherwise fire-and-forget: a client only sees deltas that
arrive while it's connected and viewing the chat. Leave mid-stream and
come back and the screen stays blank until the run finishes (the final
message is the first thing that lands). This registry holds the partial
text of each still-streaming run so a (re)connecting client can fetch it
and restore the live bubble.

Keyed by ``session_key``. Fed by the transport stream callbacks (gateway
direct WS today; relay web channel next), read by the ``chat.inflight``
feature RPC — which is served identically over relay and direct gateway,
so one registry + one RPC covers every surface.

In-memory only: if the bot restarts, the run is gone anyway and the
final message (persisted to history) is the fallback.
"""

from __future__ import annotations

import time

# session_key → {"runId", "text", "user", "iterations": list, "updatedAt"}
_runs: dict[str, dict] = {}

# Cap the stored tool-turn events per run so a marathon turn can't grow the
# registry without bound. Each iteration emits ~2 events (the assistant call +
# the tool result); a few hundred covers any realistic run, and the cap drops
# the OLDEST so the tail (most recent panel state) always survives.
_MAX_ITERATIONS = 600


def _fresh(run_id: str, user: str = "") -> dict:
    return {
        "runId": run_id, "text": "", "user": user or "",
        "iterations": [], "updatedAt": time.time(),
    }


def begin(session_key: str, run_id: str, user: str = "") -> None:
    """Start tracking a run for this session, clearing any stale entry.

    ``user`` is the message that triggered the run. We keep it so a client that
    re-enters mid-stream can restore the FULL turn — the user's own bubble plus
    the partial assistant reply — not just the assistant text. Without it, the
    triggering message (which isn't persisted to history until the turn
    settles) would vanish from the reopened chat. Mirrors the reference's
    ``inflight_turn`` which holds both ``user`` and ``assistant``.
    """
    if not session_key or not run_id:
        return
    _runs[session_key] = _fresh(run_id, user)


def append(session_key: str, run_id: str, delta: str) -> None:
    """Accumulate a streamed delta. Tolerates a missing ``begin`` (e.g. a
    run whose first delta arrives before tracking was set up)."""
    if not session_key or not delta:
        return
    cur = _runs.get(session_key)
    if cur is None or cur.get("runId") != run_id:
        cur = _fresh(run_id)
        _runs[session_key] = cur
    cur["text"] += delta
    cur["updatedAt"] = time.time()


def append_iteration(session_key: str, run_id: str, event: dict) -> None:
    """Accumulate one tool-turn (``iteration_step``) event so a client that
    re-enters mid-stream can rebuild the live tool-call panel — not just the
    assistant text. The event is stored as the transport already forwards it
    (``{runId, iterationIdx, role, content, tool_calls?, ...}``), so the client
    replays it through the same iteration→tool-turn mapping it uses live."""
    if not session_key or not isinstance(event, dict):
        return
    cur = _runs.get(session_key)
    if cur is None or cur.get("runId") != run_id:
        cur = _fresh(run_id)
        _runs[session_key] = cur
    iters = cur.setdefault("iterations", [])
    iters.append(event)
    if len(iters) > _MAX_ITERATIONS:
        del iters[: len(iters) - _MAX_ITERATIONS]
    cur["updatedAt"] = time.time()


def finish(session_key: str, run_id: str) -> None:
    """Drop the run once it settles (final / aborted / error). Only clears
    if the stored run still matches — a newer run for the same session
    must not be wiped by a late finish from the previous one."""
    cur = _runs.get(session_key)
    if cur and cur.get("runId") == run_id:
        _runs.pop(session_key, None)


def get(session_key: str) -> dict | None:
    """Return ``{"runId", "text", "user", "iterations"}`` for the in-flight
    run, or None.

    ``text`` is the partial assistant reply; ``user`` is the triggering
    message; ``iterations`` is the list of tool-turn events so the client can
    rebuild the live tool-call panel — together they restore the WHOLE turn.
    """
    cur = _runs.get(session_key)
    if not cur:
        return None
    return {
        "runId": cur["runId"],
        "text": cur["text"],
        "user": cur.get("user", ""),
        "iterations": list(cur.get("iterations", [])),
    }
