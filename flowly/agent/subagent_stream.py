"""A subagent's running turn, live: its events and who receives them.

A subagent working for the owner is a thread whose conversation is
``subagent:<threadId>`` (``flowly.agent.subagent_threads``). Its turn is saved
when the run ends; while it runs, the same live events an owner's turn has
stream under that key and the run's id (docs/chat-wire-protocol.md §4.8):

* ``chat {state:"run_started"}`` once, with the task (``prompt``, the brief);
* ``agent {stream:"assistant", data:{text}}`` — what the model said, one
  delta per model reply (``ChildStream.delta`` is the hook token streaming
  will feed);
* ``chat {state:"iteration_step"}`` per reply with tool calls and per tool
  result, in the main loop's shape (``iteration_event_payload``);
* ``chat {state:"tool_progress"}`` as a call is queued, runs and ends;
* ``chat {state:"agent_note"}`` for a message from the main agent the
  subagent read at a step (``noteKind: "task"`` for a task it gave the
  running turn);
* ``compaction`` when a summary fits its context;
* one terminal: ``chat {state:"final"}`` (``aborted: true`` when stopped) or
  ``chat {state:"error"}``, with the run's ``outcome``.

Every event carries ``sessionKey`` and ``turnOrigin: "subagent_task"``.
The in-flight registry (``flowly.agent.inflight``) holds the turn so far for
``chat.inflight`` and ``subagents.watch``, whoever watches.

**Delivery.** Events go only to those watching the conversation
(``subagents.watch``; on the direct gateway also a ``chat.inflight`` binding).
:class:`SubagentStreamHub` asks each transport what it would send (synchronously,
so the partition between a watcher's snapshot and its stream is exact) and
delivers in order, per conversation, from its own task: the subagent never
waits for a socket. With nobody watching, a transport answers from one dict
lookup and nothing is built for the wire beyond what the in-flight registry
keeps anyway.

**Authority.** Events of a subagent whose conversation was started in an
account-owned conversation are that account's (``child_event_access``); the
subscription itself is authorized by who may read the parent conversation.

A failure here never touches the run.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from loguru import logger

# ``turnOrigin`` of a subagent's own turn (``"subagent"`` is the parent's
# report turn, flowly.agent.report_turn).
SUBAGENT_TURN_ORIGIN = "subagent_task"
# Who wrote a task or a note the subagent got.
MAIN_AGENT_AUTHOR = "main_agent"
# A watch holds this long unless renewed (clients renew every ~90 s).
WATCH_LEASE_SECONDS = 120.0
# The relay's envelope: ``{sessionKey, event, data}``. A plain ``chat`` event
# would be written into the parent's conversation by the relay.
RELAY_STREAM_EVENT = "subagent.stream"
# Version of the wire above (system.capabilities ``subagentStreamVersion``).
STREAM_VERSION = 1

# Events waiting for a slow watcher, per conversation. Past it new non-terminal
# events are dropped (``chat.inflight`` still has them); a terminal never is.
_QUEUE_LIMIT = 1024
_DELIVERY_TIMEOUT_SECONDS = 10.0

Delivery = Callable[[], Awaitable[Any]]


def child_event_access(parent_session_key: str):
    """The authority a subagent's events carry: that of the conversation that started it.

    For an ordinary conversation, an open authority (whoever may read the
    parent may watch; the subscription is the gate) — explicit, so no
    recipient check looks for the subagent's conversation among the owner's.
    For an account-owned one (a voice conversation), the events are its
    owner's and no other recipient's; unreadable metadata blocks them.
    """
    from flowly.live_voice.authority import RequestOwner, VoiceAuthorityError
    from flowly.live_voice.events import EventAccess
    from flowly.profile import get_flowly_home
    from flowly.session.ownership import SessionAccessError, is_owned_session, read_session_metadata
    from flowly.utils.helpers import safe_filename

    if not parent_session_key or any(c in parent_session_key for c in ("/", "\\", "\x00")):
        return EventAccess(blocked=True)
    path = get_flowly_home() / "sessions" / (safe_filename(parent_session_key.replace(":", "_")) + ".jsonl")
    try:
        metadata = read_session_metadata(path, parent_session_key, allow_missing_work=True) or {}
        if not is_owned_session(parent_session_key, metadata):
            return EventAccess()
        owner = metadata.get("voiceOwner", {"kind": "host"})
        if owner == {"kind": "host"}:
            return EventAccess(owner=RequestOwner())
        if isinstance(owner, dict) and owner.get("kind") == "account" and isinstance(owner.get("uid"), str):
            return EventAccess(owner=RequestOwner(owner["uid"]))
    except (SessionAccessError, VoiceAuthorityError, OSError, ValueError):
        pass
    return EventAccess(blocked=True)


class SubagentStreamHub:
    """Delivers subagents' live events to the transports' watchers.

    A transport takes part by having two methods:

    * ``subagent_stream_watched(session_key) -> bool`` — cheap;
    * ``prepare_subagent_stream(event, session_key, data) -> Delivery | None`` —
      synchronous: resolves who watches now and builds the frames; the
      returned coroutine function sends them. ``None``: nobody to send to.
    """

    def __init__(self, transports: Callable[[], Iterable[Any]]) -> None:
        self._transports = transports
        self._queues: dict[str, deque[list[Delivery]]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self.dropped = 0

    def _live(self) -> list[Any]:
        try:
            return [t for t in self._transports() if t is not None]
        except Exception:  # noqa: BLE001 — a transport lookup never fails a run
            logger.debug("Subagent stream: transports unavailable", exc_info=True)
            return []

    def watched(self, session_key: str) -> bool:
        for transport in self._live():
            check = getattr(transport, "subagent_stream_watched", None)
            try:
                if check is not None and check(session_key):
                    return True
            except Exception:  # noqa: BLE001
                logger.debug("Subagent stream: watch check failed", exc_info=True)
        return False

    def publish(self, event: str, session_key: str, data: dict[str, Any], *,
                access: Any = None, terminal: bool = False) -> None:
        """Queue one event for whoever watches ``session_key`` now; never blocks."""
        from flowly.live_voice.authority import request_owner_scope
        from flowly.live_voice.events import event_access_scope

        deliveries: list[Delivery] = []
        # Frames are built as trusted in-process work with the subagent's own
        # authority, whatever context the run inherited from its parent's turn.
        with request_owner_scope(None), event_access_scope(access):
            for transport in self._live():
                prepare = getattr(transport, "prepare_subagent_stream", None)
                if prepare is None:
                    continue
                try:
                    delivery = prepare(event, session_key, data)
                except Exception:  # noqa: BLE001
                    logger.debug("Subagent stream: a transport could not prepare an event", exc_info=True)
                    continue
                if delivery is not None:
                    deliveries.append(delivery)
        if not deliveries:
            return
        queue = self._queues.setdefault(session_key, deque())
        if len(queue) >= _QUEUE_LIMIT and not terminal:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 500 == 0:
                logger.warning(f"Subagent stream: a watcher is behind; {self.dropped} live event(s) dropped")
            return
        queue.append(deliveries)
        worker = self._workers.get(session_key)
        if worker is None or worker.done():
            self._workers[session_key] = asyncio.create_task(self._drain(session_key))

    async def _drain(self, session_key: str) -> None:
        queue = self._queues.get(session_key)
        try:
            while queue:
                deliveries = queue.popleft()
                results = await asyncio.gather(
                    *(asyncio.wait_for(delivery(), timeout=_DELIVERY_TIMEOUT_SECONDS) for delivery in deliveries),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        logger.debug(f"Subagent stream: delivery failed ({type(result).__name__})")
        finally:
            # No await between the last check and here: a publish either saw
            # this worker running (and its event was drained) or starts a new one.
            if self._workers.get(session_key) is asyncio.current_task():
                self._workers.pop(session_key, None)
            if queue is not None and not queue:
                self._queues.pop(session_key, None)

    async def drained(self) -> None:
        """Wait until every queued event was handed to its transport (tests, shutdown)."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


class ChildStream:
    """One running subagent turn's live events and in-flight entry.

    Fed from the run's journal (``ChildState.journal``, every message the run
    added, as saved): ``sync`` turns the rows it has not seen into events, so
    the live turn and the saved one are the same rows, and a run continued
    after a restart rebuilds its in-flight entry from the replayed journal
    without sending it again. ``iterationIdx`` is the row's index in the
    journal, stable across a restart.
    """

    def __init__(self, sink: Any, *, run_id: str, session_key: str, thread_id: str,
                 parent_session_key: str, turn: int, trigger: str, title: str,
                 mark: dict[str, Any] | None, model: str, brief: str, with_conversation: bool,
                 access: Any = None, started_at: float | None = None) -> None:
        from flowly.agent.tool_progress import ToolProgress

        self.sink = sink
        self.run_id = run_id
        self.session_key = session_key
        self.model = model
        self._access = access
        self._fields = {"turnOrigin": SUBAGENT_TURN_ORIGIN, "sessionKey": session_key}
        self._subagent = {
            "threadId": thread_id, "turn": turn, "trigger": trigger,
            "parentSessionKey": parent_session_key, "title": title, "mark": dict(mark or {}),
            "model": model, "withConversation": with_conversation,
        }
        self._brief = brief
        self._cursor = 0
        self._internal_call_ids: set[str] = set()
        self._live = False
        self._ended = False
        # When the run began (a continued run keeps its original start).
        self._started_at = started_at or time.time()
        self._compaction_id = ""
        self._compaction_before = 0
        self._progress = ToolProgress(run_id, self._publish_progress)

    # ── wire ────────────────────────────────────────────────────────────

    def _send(self, event: str, data: dict[str, Any], *, terminal: bool = False) -> None:
        sink = self.sink
        if sink is None or not self._live:
            return
        try:
            sink.publish(event, self.session_key, data, access=self._access, terminal=terminal)
        except Exception:  # noqa: BLE001 — the stream never fails the run
            logger.debug(f"[SubagentStream] [{self.run_id[:8]}] publish failed", exc_info=True)

    def _chat(self, data: dict[str, Any], *, terminal: bool = False) -> None:
        self._send("chat", {**data, "runId": self.run_id, **self._fields}, terminal=terminal)

    async def _publish_progress(self, event: dict[str, Any]) -> None:
        from flowly.agent import inflight

        wrapped = {**event, "runId": self.run_id, **self._fields}
        inflight.append_tool_progress(self.session_key, self.run_id, wrapped)
        self._send("chat", wrapped)

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self, journal: list[dict[str, Any]], *, resumed: bool = False) -> None:
        """Open the in-flight entry and announce the turn.

        A continued run first rebuilds what it did before the restart from its
        journal, silently: whoever watches now gets it from ``chat.inflight``.
        """
        from flowly.agent import inflight

        try:
            inflight.begin(self.session_key, self.run_id, "", turn_origin=SUBAGENT_TURN_ORIGIN,
                           fields={"prompt": self._brief, "promptAuthor": MAIN_AGENT_AUTHOR,
                                   "subagent": dict(self._subagent)})
            await self._consume(journal)
        except Exception:  # noqa: BLE001
            logger.debug(f"[SubagentStream] [{self.run_id[:8]}] could not open", exc_info=True)
        self._live = True
        self._chat({
            "state": "run_started",
            "hiddenPrompt": False,
            "steerable": False,
            "prompt": self._brief,
            "promptAuthor": MAIN_AGENT_AUTHOR,
            **self._subagent,
            "startedAt": int(self._started_at * 1000),
            **({"resumed": True} if resumed else {}),
        })

    async def sync(self, journal: list[dict[str, Any]]) -> None:
        """Send what the run added since the last call."""
        if self._ended:
            return
        try:
            await self._consume(journal)
        except Exception:  # noqa: BLE001
            logger.debug(f"[SubagentStream] [{self.run_id[:8]}] sync failed", exc_info=True)

    async def tool_started(self, call_id: str, name: str, arguments: dict[str, Any]) -> None:
        if self._ended:
            return
        try:
            await self._progress.start(call_id, name, arguments)
        except Exception:  # noqa: BLE001
            logger.debug(f"[SubagentStream] [{self.run_id[:8]}] progress failed", exc_info=True)

    def delta(self, text: str) -> None:
        """Assistant text, as an ``agent`` delta (token streaming will call this per chunk)."""
        from flowly.agent import inflight

        if self._ended or not text:
            return
        try:
            inflight.append(self.session_key, self.run_id, text)
        except Exception:  # noqa: BLE001
            logger.debug(f"[SubagentStream] [{self.run_id[:8]}] in-flight text failed", exc_info=True)
        self._send("agent", {"runId": self.run_id, **self._fields, "stream": "assistant",
                             "data": {"text": text}})

    def answer(self, text: str) -> None:
        """The run's closing reply was produced: its text as the turn's last delta."""
        self.delta(text)

    def compaction(self, phase: str, *, tokens_before: int = 0, tokens_after: int = 0,
                   messages_removed: int = 0) -> None:
        """A summary fitting the run's context: ``started`` then ``completed`` | ``failed``."""
        from flowly.agent import compaction_status

        if self._ended and phase == "started":
            return
        if phase == "started":
            if self._compaction_id:
                return
            self._compaction_id = f"subagent-{self.run_id[:8]}-{int(time.time() * 1000)}"
            self._compaction_before = tokens_before
        elif not self._compaction_id:
            return  # nothing was announced
        compaction_id = self._compaction_id
        before = tokens_before or self._compaction_before
        if phase != "started":
            self._compaction_id = ""
        try:
            compaction_status.record(self.session_key, phase, before, tokens_after, messages_removed,
                                     time.time(), compaction_id=compaction_id)
        except Exception:  # noqa: BLE001
            pass
        self._send("compaction", {
            "phase": phase, "sessionKey": self.session_key, "runId": self.run_id,
            "turnOrigin": SUBAGENT_TURN_ORIGIN, "compactionId": compaction_id,
            "tokensBefore": int(before or 0), "tokensAfter": int(tokens_after or 0),
            "messagesRemoved": int(messages_removed or 0),
        }, terminal=phase != "started")

    def end(self, outcome: str, result: str, *, usage: dict[str, Any] | None = None,
            error_code: str | None = None, duration_ms: int | None = None) -> None:
        """The run's one terminal, then the in-flight entry goes.

        ``ok`` → ``final``; ``cancelled`` / ``interrupted`` → ``final`` with
        ``aborted: true``; ``error`` / ``timeout`` → ``error``.
        """

        if self._ended:
            return
        if self._compaction_id:
            self.compaction("failed")
        self._ended = True
        duration = duration_ms if duration_ms is not None else int((time.time() - self._started_at) * 1000)
        common = {"model": self.model, "outcome": outcome, "durationMs": max(0, int(duration)),
                  "completedAt": datetime.now(timezone.utc).isoformat()}
        if outcome in ("error", "timeout"):
            self._chat({
                "state": "error",
                **common,
                "errorCode": str(error_code or ("timeout" if outcome == "timeout" else "subagent_failed")),
                "errorTitle": ("The subagent ran out of time" if outcome == "timeout"
                               else "The subagent couldn't finish"),
                "errorMessage": result or "The subagent couldn't finish.",
                "retryable": False,
                "usage": dict(usage or {}),
            }, terminal=True)
        else:
            aborted = outcome in ("cancelled", "interrupted")
            self._chat({
                "state": "final",
                **common,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "" if aborted else result}],
                            "usage": dict(usage or {})},
                **({"aborted": True} if aborted else {}),
            }, terminal=True)
        self.close()

    def close(self) -> None:
        """Drop the in-flight entry without a terminal (a run suspended for a restart)."""
        from flowly.agent import inflight

        self._ended = True
        try:
            inflight.finish(self.session_key, self.run_id)
        except Exception:  # noqa: BLE001
            pass

    # ── journal → events ────────────────────────────────────────────────

    async def _consume(self, journal: list[dict[str, Any]]) -> None:
        from flowly.agent import inflight
        from flowly.tool_activity import iteration_event_payload

        while self._cursor < len(journal):
            index = self._cursor
            row = journal[index]
            self._cursor += 1
            if not isinstance(row, dict) or row.get("_display_hidden"):
                continue
            role = row.get("role")
            if role == "assistant":
                text = _text(row.get("content"))
                if text.strip():
                    self.delta(text)
                calls = row.get("tool_calls")
                if isinstance(calls, list) and calls:
                    await self._progress.announce(index, calls)
                payload = iteration_event_payload(row, run_id=self.run_id, iteration_idx=index,
                                                  internal_call_ids=self._internal_call_ids)
                if payload is None:
                    continue
                if "tool_calls" not in payload:
                    # A reply without calls mid-run (the main agent wrote
                    # meanwhile): still a step boundary for the text above.
                    payload["tool_calls"] = []
            elif role == "tool":
                status = str(row.get("status") or "ok")
                call_id = str(row.get("tool_call_id") or "")
                await self._progress.result(call_id, _text(row.get("content")),
                                            failed=status not in ("ok",),
                                            stopped=status in ("interrupted", "not_run"))
                payload = iteration_event_payload(row, run_id=self.run_id, iteration_idx=index,
                                                  internal_call_ids=self._internal_call_ids)
                if payload is None:
                    continue
                if status != "ok":
                    payload["status"] = status
            elif role == "user" and row.get("kind") == "agent_note":
                payload = {"runId": self.run_id, "iterationIdx": index, "state": "agent_note",
                           "author": str(row.get("author") or MAIN_AGENT_AUTHOR),
                           "content": _text(row.get("content"))}
                if row.get("noteKind") == "task":
                    payload["noteKind"] = "task"
                wrapped = {**payload, **self._fields}
                inflight.append_iteration(self.session_key, self.run_id, wrapped)
                self._send("chat", wrapped)
                continue
            else:
                continue
            wrapped = {**payload, "state": "iteration_step", "runId": self.run_id, **self._fields}
            inflight.append_iteration(self.session_key, self.run_id, wrapped)
            self._send("chat", wrapped)
