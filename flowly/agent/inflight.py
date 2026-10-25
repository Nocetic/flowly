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

Every piece of a run (a delta, a tool step, a tool's progress) gets the next
``seq`` of that run here, where it is recorded, and the transport sends it
with the piece. The snapshot names the last piece it holds (``seq``), so a
client that shows the snapshot adds only pieces with a higher ``seq`` and
never shows one twice (docs/chat-wire-protocol.md §8).
"""

from __future__ import annotations

import time
from contextvars import ContextVar

from flowly.session.control_access import SessionControlScope

# session_key → {"runId", "text", "user", "iterations": list, "updatedAt"}
_runs: dict[str, dict] = {}
_control_scopes: dict[str, tuple[str, SessionControlScope]] = {}

# Cap the stored tool-turn events per run so a marathon turn can't grow the
# registry without bound. Each iteration emits ~2 events (the assistant call +
# the tool result); a few hundred covers any realistic run, and the cap drops
# the OLDEST so the tail (most recent panel state) always survives.
_MAX_ITERATIONS = 600


# What a run's entry may describe beyond the stream (``begin(fields=...)``):
# a subagent's turn names its task and thread (flowly.agent.subagent_stream).
_EXTRA_FIELDS = ("prompt", "promptAuthor", "subagent")


def _fresh(run_id: str, user: str = "", goal_run: bool | None = None,
           turn_origin: str | None = None, fields: dict | None = None) -> dict:
    extra = {key: fields[key] for key in _EXTRA_FIELDS if fields and key in fields}
    return {
        "runId": run_id, "text": "", "user": user or "",
        "iterations": [], "updatedAt": time.time(), "seq": 0,
        **({'goalRun': goal_run} if goal_run is not None else {}),
        **({'turnOrigin': turn_origin} if turn_origin else {}),
        **({'extra': extra} if extra else {}),
    }


def begin(session_key: str, run_id: str, user: str = "", *, goal_run: bool | None = None,
          turn_origin: str | None = None, fields: dict | None = None) -> None:
    """Start tracking a run for this session, clearing any stale entry.

    ``user`` is the message that triggered the run. We keep it so a client that
    re-enters mid-stream can restore the FULL turn — the user's own bubble plus
    the partial assistant reply — not just the assistant text. Without it, the
    triggering message (which isn't persisted to history until the turn
    settles) would vanish from the reopened chat. Mirrors the reference's
    ``inflight_turn`` which holds both ``user`` and ``assistant``.

    ``turn_origin`` names a run the server started on its own (``"subagent"``
    for a subagent's report): a client re-entering mid-run adopts it as that
    kind of run (no user row, no guidance into it).

    ``fields`` describe the run beyond its stream, returned by ``get`` as
    they are: ``prompt`` / ``promptAuthor`` (a task someone other than the
    owner gave, shown instead of a user row) and ``subagent`` (the thread a
    subagent's turn belongs to). Other keys are ignored.
    """
    if not session_key or not run_id:
        return
    scope = SessionControlScope.capture(session_key)
    _runs[session_key] = _fresh(run_id, user, goal_run, turn_origin, fields)
    _control_scopes[session_key] = (run_id, scope)


# The ``seq`` of the delta being sent, for a transport whose stream callback
# only receives the text (gateway/server.py ``_run_chat``): set around the call.
_stream_seq: ContextVar[int] = ContextVar("inflight_stream_seq", default=0)


def stream_seq_field() -> dict:
    """``{"seq": n}`` for the delta being sent, or ``{}`` when none was recorded."""
    seq = _stream_seq.get()
    return {"seq": seq} if seq else {}


def _next_seq(cur: dict) -> int:
    cur["seq"] = cur.get("seq", 0) + 1
    return cur["seq"]


def append(session_key: str, run_id: str, delta: str) -> int:
    """Accumulate a streamed delta and return its ``seq`` (0: not recorded).
    Tolerates a missing ``begin`` (e.g. a run whose first delta arrives
    before tracking was set up)."""
    if not session_key or not delta:
        return 0
    cur = _runs.get(session_key)
    if cur is None or cur.get("runId") != run_id:
        begin(session_key, run_id)
        cur = _runs[session_key]
    cur["text"] += delta
    cur["updatedAt"] = time.time()
    return _next_seq(cur)


def append_iteration(session_key: str, run_id: str, event: dict) -> int:
    """Accumulate one tool-turn (``iteration_step``) event so a client that
    re-enters mid-stream can rebuild the live tool-call panel — not just the
    assistant text. The event is stored as the transport already forwards it
    (``{runId, iterationIdx, role, content, tool_calls?, ...}``), so the client
    replays it through the same iteration→tool-turn mapping it uses live.

    Returns the step's ``seq`` (0: not recorded); the stored step carries it."""
    if not session_key or not isinstance(event, dict):
        return 0
    cur = _runs.get(session_key)
    if cur is None or cur.get("runId") != run_id:
        begin(session_key, run_id)
        cur = _runs[session_key]
    seq = _next_seq(cur)
    iters = cur.setdefault("iterations", [])
    iters.append({**event, "seq": seq})
    if len(iters) > _MAX_ITERATIONS:
        del iters[: len(iters) - _MAX_ITERATIONS]
    # A tool-call boundary MOVES the streamed preamble out of the live
    # bubble: the event we just stored carries that text as the panel
    # group's preamble, and every live client clears its bubble on the
    # matching iteration_step. Reset the partial here too, so a client
    # restoring mid-run gets only the text streamed AFTER the last
    # boundary — not every preamble re-glued into one bubble. Guarded on
    # tool_calls: an assistant event without them has no panel entry the
    # text could have moved into. An empty list is a step whose calls were
    # all hidden (the agent's own bookkeeping): still a boundary.
    if event.get("role") == "assistant" and isinstance(event.get("tool_calls"), list):
        cur["text"] = ""
    cur["updatedAt"] = time.time()
    return seq


def append_tool_progress(session_key: str, run_id: str, event: dict) -> int:
    """Retain latest per-call display snapshots without resetting streamed text.

    Returns the snapshot's ``seq``, or 0 when it was not kept (another run,
    or not newer than the one held): a client applies it as it does today."""
    cur = _runs.get(session_key)
    if cur is None or cur.get("runId") != run_id:
        return 0
    call = event.get("call")
    if not isinstance(call, dict) or not call.get("id"):
        return 0
    progress = cur.setdefault("toolProgress", {})
    key = (event.get("iterationIdx"), call["id"])
    previous = progress.get(key)
    if previous and previous.get("revision", 0) >= event.get("revision", 0):
        return 0
    seq = _next_seq(cur)
    progress[key] = {**event, "call": dict(call), "seq": seq}
    while len(progress) > _MAX_ITERATIONS:
        del progress[next(iter(progress))]
    cur["updatedAt"] = time.time()
    return seq


def clear_text(session_key: str, run_id: str, steering_sequence: int = 0) -> None:
    """A persisted steering checkpoint owns the text before the new segment."""
    cur = _runs.get(session_key)
    if cur and cur.get("runId") == run_id:
        cur["text"] = ""
        if steering_sequence:
            cur["steeringSequence"] = steering_sequence
        cur["updatedAt"] = time.time()


def text(session_key: str, run_id: str) -> str:
    """The run's live text since its last step boundary ("" for another run or none)."""
    cur = _runs.get(session_key)
    return cur["text"] if cur and cur.get("runId") == run_id else ""


def replace_text(session_key: str, run_id: str, text: str) -> None:
    """A streamed reply was taken back: the live text is ``text`` from here on."""
    cur = _runs.get(session_key)
    if cur and cur.get("runId") == run_id:
        cur["text"] = text
        cur["updatedAt"] = time.time()


def finish(session_key: str, run_id: str) -> None:
    """Drop the run once it settles (final / aborted / error). Only clears
    if the stored run still matches — a newer run for the same session
    must not be wiped by a late finish from the previous one."""
    cur = _runs.get(session_key)
    if cur and cur.get("runId") == run_id:
        _runs.pop(session_key, None)
        _control_scopes.pop(session_key, None)


def control_scope(run_id: str) -> SessionControlScope | None:
    """Look up a live run's immutable control target without exposing text."""
    from flowly.session.ownership import SessionAccessError

    keys = [key for key, row in _runs.items() if row.get('runId') == run_id]
    if not keys:
        return None
    if len(keys) != 1:
        raise SessionAccessError()
    binding = _control_scopes.get(keys[0])
    if binding is None or binding[0] != run_id:
        raise SessionAccessError()
    return binding[1]


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
        # The last piece this snapshot holds: a client adds only higher ones.
        "seq": cur.get("seq", 0),
        "user": cur.get("user", ""),
        "iterations": list(cur.get("iterations", [])),
        **({"toolProgress": [{**event, "call": dict(event["call"])}
                             for event in cur["toolProgress"].values()]}
           if cur.get("toolProgress") else {}),
        **({"steeringSequence": cur["steeringSequence"]} if cur.get("steeringSequence") else {}),

        **({'goalRun': cur['goalRun']} if 'goalRun' in cur else {}),
        **({'turnOrigin': cur['turnOrigin']} if cur.get('turnOrigin') else {}),
        **{key: (dict(value) if isinstance(value, dict) else value)
           for key, value in (cur.get('extra') or {}).items()},
    }
