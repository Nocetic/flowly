"""Subagent manager for background task execution."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from flowly.bus.events import ConversationInboundMessage
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMProvider
from flowly.agent.repetition import mark_repeated
from flowly.agent.tools.base import Tool
from flowly.agent.tools.discovery import (
    annotate_search_repetition,
    build_tool_disclosure,
)
from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, MemoryAppendTool
from flowly.agent.tools.shell import ExecTool
from flowly.exec.types import ExecConfig
from flowly.agent.tools.web import WebSearchTool, WebFetchTool, WebExtractTool
from flowly.agent.subagent_metrics import append_metric, metrics_path
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord, ThreadCapReached, add_usage
from flowly.agent.subagent_threads import (
    MAX_OPEN_THREADS_PER_CONVERSATION,
    THREAD_IDLE_CLOSE_SECONDS,
    SubagentThreadRecord,
)
from flowly.agent import subagent_owner
from flowly.agent.subagent_checkpoint import (
    ADDED_TASK_LABEL,
    MESSAGE_LABEL,
    NOT_STARTED_CALL_TEXT,
    STOPPED_CALL_TEXT,
    TASK_LABEL,
    ChildState,
    GRACE_PROMPT,
    ResumeRefusal,
    SubagentCheckpoint,
    append_message,
    apply_compacted,
    apply_microcompact,
    apply_step,
    history_digest,
    interrupted_result,
    open_call_ids,
    read_checkpoint,
    restore_child_state,
    task_index,
    tool_class,
)
from flowly.agent.error_classifier import ErrorCategory, backoff_for, classify_response, extract_input_limit_tokens
from flowly.compaction.pruning import iteration_strip_passes, microcompact_tool_results
from flowly.compaction.request_guard import (
    ProviderRequestCoordinator,
    RequestFit,
    request_fingerprint,
    request_too_large_response,
)
from flowly.compaction.service import CompactionService
from flowly.compaction.summarizer import collect_summary_usage
from flowly.providers.base import LLMErrorInfo, LLMResponse
from flowly.providers.streamed_reply import StreamedReply
from flowly.agent.delegation import MAX_CHILDREN_PER_CONVERSATION, assign_mark
from flowly.agent.tools.agent_tools import ChildAgentMessageTool
from flowly.agent.subagent_observation import TaskEvents, run_view
from flowly.agent.subagent_stream import ChildStream, child_event_access
from flowly.agent.subagent_announce_queue import AnnounceItem, get_or_create_queue


# ---------------------------------------------------------------------------
# Subagent tool blocklist
# ---------------------------------------------------------------------------
#
# Tools that must NEVER be available inside a subagent, grouped by the reason
# they are dangerous. The subagent currently builds its tool registry by
# explicit allowlist (see _do_run below), so in practice none of these make it
# in today — but that's a happy coincidence of the current code, not a
# guarantee. SubagentToolRegistry turns the invariant into code: registering
# any of these names raises, so a regression shows up at test time instead of
# silently giving the subagent LLM user-facing side effects.
#
# Rationale per group:
#   * recursion          — child would spawn grandchildren, bypassing the
#                          `is_subagent_caller` guard and concurrency cap.
#   * persistent side FX — `cron` would let a child install jobs the user
#                          never authorised; survives the parent session.
#   * user-facing I/O    — messaging/voice/email must only originate from
#                          the main agent; a subagent impersonating is a
#                          serious UX and trust bug.
#   * external writes    — third-party APIs with real-world blast radius
#                          (calendar events, Linear tickets, Trello cards,
#                          tweets). Subagents shouldn't touch them.
#   * gateway/desktop    — these tools require the gateway process and a
#                          live user (screenshot permission, browser tab
#                          focus, computer control). A background subagent
#                          can't use them sensibly anyway.
#   * cross-session      — `sessions_list`/`session_search` expose other
#                          users' conversations; privacy boundary.
#   * unbounded system   — `system` / `docker` are wide-open admin shims.
#
# Flowly builds a child's tools from an explicit allowlist in _do_run AND
# keeps this blocklist as a defense-in-depth backstop. If the allowlist ever
# expands to cover a dangerous tool by accident, the blocklist still refuses
# the obviously-dangerous names regardless.
_BLOCKED_SUBAGENT_TOOLS: frozenset[str] = frozenset({
    # recursion / privilege escalation
    "spawn", "builtin_agent", "delegate_to", "agent_read", "agent_wait", "agent_followup",
    "agent_close",
    # persistent scheduling / background side effects
    "cron",
    # user-facing communication (only main agent may speak for the user)
    "message", "voice_call", "email",
    # external write-scope integrations
    "google_calendar", "google_drive", "google_tasks", "google_contacts",
    "linear", "trello", "x",
    # gateway / desktop-only (require live user + GUI)
    "screenshot", "browser_tab", "computer",
    # cross-session privacy — includes memory read (entry search / key
    # lookup) so a subagent cannot mine other turns' memory for secrets.
    # memory_append stays allowed: self-review subagents legitimately
    # write new facts, but random builtin children don't need to query
    # the user's history.
    "sessions_list", "session_search",
    "memory_search", "memory_get",
    # unbounded system control
    "system", "docker",
    # background subprocess registry — long-running side effects survive
    # past the subagent's turn and would route completion events to the
    # parent's session_key, so only the main agent owns this.
    "process",
})


# A subagent is another Flowly working on one task: it gets the main agent's
# own iteration limit (AgentLoop.max_iterations) and, like the main agent, no
# wall clock. What bounds a run is the same as for the main agent: the
# per-tool and per-model-call timeouts, the repetition guard, and the
# iteration limit. A caller may still pass timeout_seconds (tests).
_DEFAULT_MAX_ITERATIONS = 100
# Each model call's timeout when no wall clock divides the run's time.
_MODEL_CALL_TIMEOUT_SECONDS = 300.0
_MAX_CONCURRENT_SUBAGENTS = 8
# A finished run's result that the conversation has not handled in this long
# no longer holds a goal (see pending_for_goal). Counted from the later of its
# end and this process's start, so a result announced again after a long
# downtime still holds the goal while its report turn runs.
_UNDELIVERED_GRACE_SECONDS = 600
# After a restart, a finished run whose result no saved turn handled is
# announced again if it ended within this long — a night or a weekend day
# with Flowly closed still delivers — and the notice says how old it is.
# Older ones are recorded undelivered.
_REANNOUNCE_WITHIN_SECONDS = 86_400
# …and at most this many times: a result whose report turn keeps taking the
# process down is not announced at every start forever.
_MAX_REANNOUNCE = 3
# A run a restart stopped is continued if its last sign of life is this
# recent, and at most this many restarts in a row without it finishing a
# step since (a call that takes the process down every time ends there).
_RESUME_WITHIN_SECONDS = 86_400
_MAX_RESUMES = 3
# A result held for its batch before a restart waits at least this long for
# the siblings that continue, then is delivered anyway.
_RESUMED_BATCH_HOLD_SECONDS = 120
# The conversation's record of the subagent results its saved turns handled:
# session.metadata[SUBAGENT_REPORTS_KEY], newest last, written in the same
# save as the turn. This is the delivery commit point.
SUBAGENT_REPORTS_KEY = "subagentReports"
_REPORTS_KEPT = 256
# How long a child may wait for the main agent's reply (agent_message).
_REPLY_WAIT_SECONDS = 300
# Result text handed to the parent with the completion notice; the rest
# is one agent_read call away.
_ANNOUNCE_RESULT_CHARS = 8_000
# What one notice carries in all when several runs report together.
_ANNOUNCE_BATCH_CHARS = 24_000
# How long a finished run's result is held for siblings still working before
# it is delivered anyway (with the siblings named as still running).
_BATCH_HOLD_SECONDS = 300


class SubagentToolRegistry(ToolRegistry):
    """ToolRegistry that rejects blocked tool names for subagents.

    Any attempt to register a tool whose `name` is in
    `_BLOCKED_SUBAGENT_TOOLS` raises `ValueError`. This is deliberately
    loud (rather than silently skipping) so that a regression — e.g. a
    refactor that shares the main agent's tool loader with the subagent
    loader — surfaces as a failing test, not as a production escalation.
    """

    def register(self, tool: Tool) -> None:  # type: ignore[override]
        if tool.name in _BLOCKED_SUBAGENT_TOOLS:
            raise ValueError(
                f"Tool '{tool.name}' is blocked for subagents. "
                "See flowly.agent.subagent._BLOCKED_SUBAGENT_TOOLS for rationale."
            )
        super().register(tool)


# The header of a notice to the parent: a completion names its run, a message
# also its message id.
_NOTICE_HEADER_RE = re.compile(
    r"^\[(?:Subagent|Message from subagent) '.*?'.*?run_id=(?P<run_id>[0-9a-f-]{36})"
    r"(?:, message_id=(?P<message_id>[0-9a-f]{16}))?",
    re.MULTILINE,
)
# Every run a completion notice reports (several when a batch reports together).
_NOTICE_RUN_ID_RE = re.compile(
    r"^\[Subagent '.*?'.*?\] run_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.MULTILINE,
)


def _created_artifact_id(payload: Any, tool_args: dict[str, Any]) -> str | None:
    """The id of an artifact a ``create`` call actually stored, else None.

    The artifact tool answers ``{"artifact": {"id": …}}``; reading a top-level
    ``id`` never matched, so a child's saved report was never linked to its
    run. Listing or reading artifacts links nothing.
    """
    if str(tool_args.get("action") or "") != "create" or not isinstance(payload, dict):
        return None
    if payload.get("ok") is False or payload.get("error"):
        return None
    artifact = payload.get("artifact")
    candidate = artifact.get("id") if isinstance(artifact, dict) else payload.get("id")
    return candidate if isinstance(candidate, str) and candidate else None


def followup_message(task: str, notes: list[str] | None = None) -> str:
    """A follow-up turn's first message: the messages that waited for it, then the task."""
    parts = [f"{MESSAGE_LABEL}\n{note}" for note in notes or []]
    parts.append(f"{TASK_LABEL}\n{task}")
    return "\n\n".join(parts)


def closed_text(thread: SubagentThreadRecord) -> str:
    """Why a closed subagent takes no new task, as a fact."""
    title = thread.title or "This subagent"
    why = {
        "agent": "was closed with agent_close",
        "owner": "was closed by the owner",
        "idle": f"was closed after {THREAD_IDLE_CLOSE_SECONDS // 86_400} days with no task",
        "cap": (f"was closed to keep at most {MAX_OPEN_THREADS_PER_CONVERSATION} subagents of "
                "this conversation open"),
        "parent_deleted": "belonged to a conversation that was deleted",
    }.get(thread.closed_reason or "", "is closed")
    kept = "" if thread.closed_reason == "parent_deleted" else " Its history stays readable."
    return f"'{title}' {why}; it takes no new task or message.{kept}"


def _with_conversation(record: SubagentRunRecord, state: ChildState) -> bool:
    """Whether the turn's first message carries the forked conversation (a thread's first turn only)."""
    return record.turn == 1 and bool(state.first_message) and state.first_message != record.task


def _blocking_may_succeed(category: ErrorCategory) -> bool:
    """Whether a blocking call may succeed where the stream failed before any text.

    The main loop's rule (a retryable failure: a transient fault, a rate
    limit, a model or route that does not stream), and a rejected key: the
    providers' blocking call moves to a fallback key, their stream does not.
    A request too large, no credits or an image the model cannot read are
    rejected the same either way (``_call_model`` handles the first).
    """
    return category is ErrorCategory.AUTH or backoff_for(category, 1) is not None


def _streams_tokens(provider: Any) -> bool:
    """Whether the provider streams a reply token by token.

    ``LLMProvider.chat_stream`` itself makes one blocking ``chat`` call and
    yields its whole reply; a provider that keeps it is called with ``chat``
    as before (its reply goes out as one delta).
    """
    own = getattr(type(provider), "chat_stream", None)
    return callable(own) and own is not LLMProvider.chat_stream


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f" […{len(text) - limit} more]"


def record_subagent_reports(metadata: dict[str, Any], run_ids: list[str]) -> None:
    """Note in a session's metadata that its turn handled these results.

    Written into the same save as the turn, so "the conversation has this
    result" and "the result is delivered" can never disagree after a crash.
    """
    if not run_ids:
        return
    kept = [rid for rid in metadata.get(SUBAGENT_REPORTS_KEY) or [] if isinstance(rid, str)]
    kept = [rid for rid in kept if rid not in run_ids] + list(dict.fromkeys(run_ids))
    metadata[SUBAGENT_REPORTS_KEY] = kept[-_REPORTS_KEPT:]


def _core_version() -> str:
    try:
        from flowly import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def _age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3_600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 86_400:
        return f"{seconds // 3_600} h {seconds % 3_600 // 60} min"
    return f"{seconds // 86_400} d {seconds % 86_400 // 3_600} h"


def _looks_like_code(s: str) -> bool:
    """True for internal/code-ish labels that must NEVER be shown to a user —
    ``builtin:researcher``, ``subagent:<uuid>``, ``self-review``, bare UUIDs."""
    s = (s or "").strip()
    if not s:
        return True
    head = s.split(":", 1)[0]
    if ":" in s and head in {"builtin", "subagent", "agent", "self"}:
        return True
    if s in {"self-review", "self_review"}:
        return True
    if re.fullmatch(r"[0-9a-fA-F][0-9a-fA-F-]{7,}", s):  # uuid / hex handle
        return True
    return False


def _derive_display_name(task: str) -> str:
    """A short, human, task-derived title for a subagent — never a code/UUID.

    Uses the first meaningful sentence/line of the task, trimmed to a word
    boundary. Strips a leading ``FRESH:`` marker. This is what desktop/iOS and
    the chat announce render, so it must always read as a task, e.g.
    "Research AI in mental health & neurology" rather than "builtin:researcher".
    """
    t = re.sub(r"^\s*FRESH:\s*", "", (task or "").strip(), flags=re.IGNORECASE)
    first = t.split("\n", 1)[0].strip()
    # Prefer a clean first sentence when it's a sensible length.
    m = re.match(r"(.{20,80}?[.!?])(?:\s|$)", first)
    if m:
        first = m.group(1).rstrip(".!?")
    if len(first) > 72:
        first = first[:72].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return first or "task"


# A reply may claim at most this many output tokens, and at most this share of
# the window: asked for 16,384 output tokens, a 16,385-token model has nothing
# left to read the request with. It also stays inside the reserve the
# compaction threshold leaves (CompactionService.effective_reserve_tokens_for),
# so a request fitted to the threshold plus its reply fit one window.
_SUBAGENT_MAX_OUTPUT_TOKENS = 16_384
_SUBAGENT_MAX_OUTPUT_SHARE = 0.25
_SUBAGENT_MIN_OUTPUT_TOKENS = 512
# The grace (summary) call's reply.
_GRACE_MAX_OUTPUT_TOKENS = 4_096
# The brief (the child's first message) is authoritative input: a request
# summary keeps it verbatim after the summary, like the system prompt.
_PINNED_BRIEF = 1

# What a summary of a subagent's own work keeps. The brief stays verbatim
# beside it, so the summary is the record of the work itself.
CHILD_SUMMARY_INSTRUCTIONS = (
    "This is the working record of a subagent doing one task for the main "
    "agent; its brief is kept verbatim beside this record and is not part of "
    "it. Record the work so far: each action and its outcome (files read or "
    "written, commands run, pages fetched), the evidence found with the exact "
    "URLs, paths, identifiers, numbers and quotes a final report needs, what "
    "failed and why, and what is not done yet. Refer to the task and the main "
    "agent, never to a user."
)


def subagent_output_budget(window: int, reserve: int) -> int:
    """Output tokens one subagent reply may claim in ``window``.

    A constant 16,384 was sent to every model. On a 16,385-token model that
    leaves one token for the entire request — the provider rejects it, and no
    amount of compacting the message list can help, because the overflow is
    the reply budget itself.
    """
    return max(_SUBAGENT_MIN_OUTPUT_TOKENS, min(
        _SUBAGENT_MAX_OUTPUT_TOKENS, int(window * _SUBAGENT_MAX_OUTPUT_SHARE), reserve,
    ))


class SubagentManager:
    """
    Manages background subagent execution.

    Subagents are lightweight agent instances that run in the background
    to handle specific tasks. They share the same LLM provider but have
    isolated context and a focused system prompt.
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        brave_api_key: str | None = None,
        exec_config: ExecConfig | None = None,
        registry: SubagentRegistry | None = None,
        on_event: Any | None = None,
        state_dir: Path | None = None,
        web_search_proxy_url: str | None = None,
        web_search_server_id: str | None = None,
        web_search_auth_token: str | None = None,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        compaction: CompactionService | None = None,
    ):
        self.provider = provider
        # The main agent's own limit; AgentLoop passes it in.
        self.max_iterations = max(1, int(max_iterations))
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        # The main agent's context management (AgentLoop passes its own
        # service): windows, the proxy cap and provider-learned input
        # ceilings, the threshold, microcompact and summaries are the same,
        # and a ceiling one learns every other request uses. Without one
        # (tests, embeddings) a stateless service of the same defaults.
        self.compaction = compaction or CompactionService(provider=provider, model=self.model)
        self._requests = ProviderRequestCoordinator(self.compaction)
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecConfig()
        self._registry = registry or SubagentRegistry()
        # This manager's identity as an owner of runs (subagent_owner).
        self._instance = uuid.uuid4().hex
        subagent_owner.register_instance(self._instance)
        self._started_at = time.time()
        # Results agent_wait handed to a turn not yet saved: run_id → (the
        # conversation's key, that turn's id). Saving the turn delivers them;
        # a turn that ends without saving gives back its own (release_taken).
        # Keyed by turn: an agenda or Board turn of the same conversation can
        # run alongside the owner's and must never commit or release theirs.
        self._taken: dict[str, tuple[str, str | None]] = {}
        # Reads the run ids a conversation's saved turns handled (the
        # session's SUBAGENT_REPORTS_KEY). AgentLoop sets it; None = unknown.
        self.committed_reports: Callable[[str], set[str]] | None = None
        # Runs a notice as its conversation's own streamed turn on the
        # surface the owner is watching (AgentLoop sets it). Called with the
        # conversation's key, the delivery address and the notice; False
        # means no surface took it and the notice goes over the bus.
        self.report_submitter: Callable[[str, str, str, str], Awaitable[bool]] | None = None
        self._shutting_down = False
        # Set by a clean shutdown: resumable runs are suspended, not ended.
        self._suspending = False
        self._suspend_reason = "shutdown"
        # Whether this process continues its runs after a restart. The
        # gateway turns it on; a CLI session has nobody to continue them.
        self.resume_enabled = False
        # Checkpoints of this process's resumable runs (subagent_checkpoint),
        # and the tool call each run is in (a reply it reads is that call's).
        self._checkpoints: dict[str, SubagentCheckpoint] = {}
        self._current_call: dict[str, str] = {}
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        # Pending deliveries of held batch results, by parent and batch.
        self._batch_flushes: dict[str, asyncio.Task[None]] = {}
        # parent_session_key -> set of run_ids currently spawned by that
        # parent. Used by cancel_children() so AIAgent.interrupt(session_key)
        # can cascade into background subagents without killing everyone
        # (other sessions / other cron jobs keep running).
        self._children_by_parent: dict[str, set[str]] = {}
        self._busy_sessions: set[str] = set()
        # Messages from the main agent ({"id", "text"}) waiting for a
        # running child's next step, and a wake-up for a child waiting on a
        # reply.
        self._inboxes: dict[str, list[dict[str, str]]] = {}
        self._inbox_events: dict[str, asyncio.Event] = {}
        # Messages children sent to a conversation, kept so a waiting main
        # agent can take them directly, and a wake-up for that wait.
        self._parent_mail: dict[str, list[dict[str, str]]] = {}
        self._parent_wake: dict[str, asyncio.Event] = {}
        self._consumed_mail: dict[str, None] = {}
        self._on_event = on_event  # async callback(event_name, data)
        self._events = TaskEvents(self._deliver_event)
        self._cancel_outcomes: dict[str, str] = {}
        self._state_dir = state_dir or (workspace / ".flowly_state")
        self._web_search_proxy_url = web_search_proxy_url
        self._web_search_server_id = web_search_server_id
        self._web_search_auth_token = web_search_auth_token
        self._artifact_on_change: Any | None = None  # Broadcast callback for S3 sync
        # Builds the artifact tool a child uses. A named profile sets this to
        # the same shared-Library tool its main agent uses, so what a child
        # saves lands where the owner looks, not in the profile's own store.
        self.artifact_tool_factory: Callable[[], Tool] | None = None
        # P1.1 — optional bridge for subagent→parent activity heartbeat. Parent
        # wires this to `AIAgent._touch_activity` so the cron inactivity
        # poller doesn't timeout the main turn while a background subagent
        # is making steady progress. None = no-op (tests, headless runs).
        self.parent_activity_touch: Callable[[str, str | None], None] | None = None
        # Parent-set post_tool_call hook so a subagent's memory_append /
        # knowledge_graph writes also flow into the governance layer (the
        # background self-review writer otherwise bypasses it — different
        # tool registry). None = governance disabled.
        self.governance_post_tool: Callable[[Any], None] | None = None
        # Where a thread's running turn streams its live events
        # (flowly.agent.subagent_stream.SubagentStreamHub; the gateway sets
        # it). None: the turn still keeps its in-flight entry for
        # chat.inflight, and nothing is sent.
        self.stream_sink: Any | None = None
        # The live stream of each running thread turn, by run id.
        self._streams: dict[str, ChildStream] = {}

    # ------------------------------------------------------------------
    # Busy tracking (called by AgentLoop around message processing)
    # ------------------------------------------------------------------

    @property
    def registry(self) -> SubagentRegistry:
        """The run registry (run records + per-run tool_trace). Read by the board
        task-detail/audit view to surface a card's run trace."""
        return self._registry

    def mark_busy(self, session_key: str) -> None:
        self._busy_sessions.add(session_key)

    def mark_idle(self, session_key: str) -> None:
        self._busy_sessions.discard(session_key)

    def is_busy(self, session_key: str) -> bool:
        return session_key in self._busy_sessions

    def pending_for_goal(self, parent_session_key: str) -> list[str]:
        """Titles of this conversation's subagents a goal must still wait for.

        A run counts until it has ended and its result has been handled by the
        conversation (not merely published), so a goal never continues ahead
        of the results it is waiting for. System runs are not the goal's work.
        """
        titles: list[str] = []
        now = time.time()
        for record in self._registry.all():
            if record.parent_session_key != parent_session_key or record.origin == "system":
                continue
            waiting = record.ended_at is None or (
                record.delivery_state in ("pending", "queued", "taken")
                # A result that was never handled within the grace period
                # (a crash between publishing and handling) stops holding
                # the goal rather than parking it for good. The period
                # starts again with this process: a result announced again
                # after a restart is handled now, not when it ended.
                and now - max(record.ended_at, self._started_at) < _UNDELIVERED_GRACE_SECONDS
            )
            if waiting:
                titles.append(record.display_name or record.label)
        return titles

    def turn_reports(self, session_key: str, notice_run_ids: list[str] | None = None,
                     turn_id: str | None = None) -> list[str]:
        """The results turn ``turn_id`` of ``session_key`` handled, to commit with its save.

        The runs a notice reported plus those agent_wait handed to that turn.
        The caller records them in the session (record_subagent_reports),
        saves, then calls mark_delivered.
        """
        taken = [rid for rid, owner in self._taken.items() if owner == (session_key, turn_id)]
        return list(dict.fromkeys([*(notice_run_ids or []), *taken]))

    def mark_delivered(self, parent_session_key: str, run_ids: list[str]) -> None:
        """Record that the conversation has saved a turn handling these results."""
        for run_id in dict.fromkeys(run_ids):
            if self._taken.get(run_id, ("",))[0] == parent_session_key:
                del self._taken[run_id]
            record = self._registry.get(run_id)
            if (
                record is not None
                and record.parent_session_key == parent_session_key
                and record.ended_at is not None
                and record.delivery_state in ("pending", "queued", "taken")
            ):
                self._registry.update(run_id, delivery_state="delivered")

    def release_taken(self, session_key: str, turn_id: str | None = None) -> None:
        """Turn ``turn_id`` of ``session_key`` ended: results it took but never saved go back.

        agent_wait handed them to the turn, but the turn was not saved (it
        failed, or was dropped), so the conversation does not have them:
        they are announced again instead of being lost. Another turn's
        results are left alone.
        """
        for run_id, owner in list(self._taken.items()):
            if owner != (session_key, turn_id):
                continue
            del self._taken[run_id]
            record = self._registry.get(run_id)
            if record is None or record.delivery_state != "taken":
                continue
            self._registry.update(run_id, delivery_state="pending", announced=False)
            self._spawn_announce(run_id, self._stored_result(record), record.outcome or "unknown")

    def _spawn_announce(self, run_id: str, result: str, status: str) -> None:
        task = asyncio.ensure_future(self._announce(run_id, result, status))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

    def _committed(self, parent_session_key: str) -> set[str]:
        reader = self.committed_reports
        if reader is None:
            return set()
        try:
            return set(reader(parent_session_key))
        except Exception:
            logger.exception("Could not read which subagent results a conversation saved")
            return set()

    def _adopt(self, record: SubagentRunRecord, owners: dict[str, str], **changes: Any) -> SubagentRunRecord | None:
        """Take ``record`` over when its owner is provably gone; else None."""
        key = json.dumps(record.owner, sort_keys=True)
        if key not in owners:
            owners[key] = subagent_owner.owner_state(record.owner, self._instance)
        if owners[key] == subagent_owner.ALIVE:
            return None  # another live process is working on it
        if owners[key] == subagent_owner.OURS:
            return record
        return self._registry.claim(
            record.run_id, subagent_owner.process_owner(self._instance), record.owner, **changes,
        )

    def _reconcile_undelivered(self, records: list[SubagentRunRecord], owners: dict[str, str]) -> None:
        for record in records:
            try:
                self._reconcile_one(record, owners)
            except Exception:
                logger.exception(f"[SubagentManager] [{record.run_id[:8]}] delivery could not be recovered")

    def _reconcile_one(self, record: SubagentRunRecord, owners: dict[str, str]) -> None:
        """Deliver, once, a finished result no saved turn handled (see resume_pending)."""
        now = time.time()
        if record.ended_at is None or record.delivery_state not in ("pending", "queued", "taken"):
            return
        if record.kind != "subagent":
            # A delegated task reports through its own message, not a
            # subagent notice; it is never announced again here.
            if now - record.ended_at >= _REANNOUNCE_WITHIN_SECONDS:
                self._registry.update(record.run_id, delivery_state="undelivered")
            return
        if self._adopt(record, owners) is None:
            return
        if record.run_id in self._committed(record.parent_session_key):
            # The turn that handled it was saved; the process stopped
            # before recording that here.
            self._registry.update(record.run_id, delivery_state="delivered")
            return
        recent = now - record.ended_at < _REANNOUNCE_WITHIN_SECONDS
        if recent and record.reannounced < _MAX_REANNOUNCE and record.outcome != "cancelled":
            # Announced before the restart but never handled: it must be
            # claimable again by its batch's notice. A result only held for
            # its batch was never announced: holding it again across
            # restarts does not count against it.
            told = record.announced or record.delivery_state != "pending"
            self._registry.update(
                record.run_id, announced=False, delivery_state="pending",
                reannounced=record.reannounced + int(told),
            )
            self._spawn_announce(record.run_id, self._stored_result(record), record.outcome or "unknown")
        else:
            self._registry.update(record.run_id, delivery_state="undelivered")

    def _running_siblings(self, parent_session_key: str) -> list[SubagentRunRecord]:
        """Owner/agent-started runs still going for one conversation."""
        records = []
        for run_id in self._children_by_parent.get(parent_session_key, ()):
            record = self._registry.get(run_id)
            if record and record.ended_at is None and record.origin != "system":
                records.append(record)
        return records

    def resume_pending(self) -> None:
        """Called on startup: continue or settle what a stopped process left.

        Only runs whose owner is provably gone are touched (subagent_owner):
        a live process's runs are never settled from another one, and of two
        processes recovering one home only one takes each run (registry
        claim).

        A run that was still going is continued from its checkpoint
        (_resume) when it can be: resumable, its checkpoint readable and of
        this format, its last sign of life within _RESUME_WITHIN_SECONDS and
        fewer than _MAX_RESUMES restarts in a row without a finished step.
        Past those bounds it ends as interrupted and its conversation is
        told once; a run with no checkpoint (from before checkpoints, or not
        resumable) is finished as failed and announced, as before. A
        process that does not continue runs (resume_enabled off: the CLI)
        leaves resumable runs to the gateway.

        Finished results no saved turn handled are announced again (with
        their age) when they ended within a day; a result held for its
        batch waits again for siblings that continue.
        """
        try:
            records = self._registry.all()
        except (OSError, ValueError):
            logger.exception("Subagent history recovery unavailable")
            return
        try:
            # Threads idle past their time close, expired ones are removed.
            self._registry.maintain()
        except Exception:
            logger.exception("Subagent thread upkeep unavailable")
        owners: dict[str, str] = {}
        now = time.time()
        continued: list[tuple[SubagentRunRecord, ChildState]] = []
        settled: list[tuple[SubagentRunRecord, str, str | None, str | None]] = []
        for record in records:
            try:
                self._recover_running(record, owners, now, continued, settled)
            except Exception:
                # One unreadable or unwritable run never keeps the agent from starting.
                logger.exception(f"[SubagentManager] [{record.run_id[:8]}] could not be recovered")
        try:
            self._reconcile_undelivered(records, owners)
        except (OSError, ValueError):
            logger.exception("Subagent delivery recovery unavailable")
        if settled:
            logger.warning(f"[SubagentManager] Settling {len(settled)} task(s) a restart stopped")
        for record, outcome, code, text in settled:
            try:
                self._settle(record, outcome, code, text)
            except Exception:
                logger.exception(f"[SubagentManager] [{record.run_id[:8]}] could not be settled")
        if self.resume_enabled:
            # Tasks kept for idle subagents before the restart start now.
            try:
                self._schedule_pending_tasks(None)
            except RuntimeError:
                logger.debug("No event loop to start kept subagent tasks")
        for record, state in continued:
            try:
                self._resume(record, state)
            except Exception:
                logger.exception(f"[SubagentManager] [{record.run_id[:8]}] could not continue")
                try:
                    self._settle(record, "error", "process_restarted", None)
                except Exception:
                    logger.exception(f"[SubagentManager] [{record.run_id[:8]}] could not be settled")

    def _settle(self, record: SubagentRunRecord, outcome: str, code: str, text: str | None) -> None:
        """End a run a restart stopped and could not continue; tell its conversation."""
        self._save_settled_turn(record, text or "The process restarted before this task completed.",
                                outcome if text is not None else "error")
        if text is None:
            self._registry.finish(
                record.run_id, "error", error_code="process_restarted",
                error="Process restarted before task completed",
            )
        else:
            self._registry.finish(record.run_id, outcome, error=text, error_code=code,
                                  report=record.delivery_state != "not_required")
        self._record_metric(record.run_id)
        if record.delivery_state == "not_required":
            return
        self._spawn_announce(
            record.run_id, text or "The process restarted before this task completed.",
            outcome,
        )

    def _save_settled_turn(self, record: SubagentRunRecord, text: str, outcome: str) -> None:
        """A turn a restart ended without continuing it still takes its place in the thread.

        What it did is not known any more (no usable checkpoint): the turn is
        its task and a closing row saying how it ended, marked stopped, so a
        later task's history says it was asked and did not finish.
        """
        if not record.thread_id:
            return
        thread = self._registry.get_thread(record.thread_id)
        if thread is None or thread.closed_reason == "parent_deleted":
            return
        first = followup_message(record.task, record.notes) if record.trigger == "followup" else record.task
        try:
            self._registry.thread_store.commit_turn(
                thread, run_id=record.run_id, turn=record.turn, trigger=record.trigger,
                system_prompt="", first_message=first, brief=record.task, journal=[],
                result=text, outcome=outcome, usage=dict(record.usage), duration_ms=None,
                aborted=True, with_conversation=False,
            )
        except Exception:
            logger.exception(f"[SubagentManager] [{record.run_id[:8]}] could not save the stopped turn")

    def _recover_running(self, record: SubagentRunRecord, owners: dict[str, str], now: float,
                         continued: list[tuple[SubagentRunRecord, ChildState]],
                         settled: list[tuple[SubagentRunRecord, str, str, str | None]]) -> None:
        """Decide what becomes of one run still marked running (see resume_pending)."""
        if record.ended_at is not None or record.run_id in self._running_tasks:
            return
        if record.resumable and not self.resume_enabled:
            return  # a gateway on this home continues it
        # Its last sign of life, read before the claim updates it.
        last_seen = record.suspended_at or record.updated_at or record.created_at
        adopted = self._adopt(record, owners)
        if adopted is None:
            return
        state, refusal = self._restorable(adopted)
        # A result already decided is recorded whatever the bounds: that
        # takes no model call and loses nothing.
        decided = state is not None and state.final is not None
        if state is not None and not decided and now - last_seen > _RESUME_WITHIN_SECONDS:
            settled.append((adopted, "interrupted", "suspended_too_long", (
                f"Flowly was not running for {_age_text(now - last_seen)} while this task ran, "
                "longer than a subagent is continued after a restart (24 h). It was stopped "
                "where it was; nothing it had started was run again."
            )))
        elif state is not None and not decided and adopted.resume_count >= _MAX_RESUMES:
            settled.append((adopted, "interrupted", "resume_failed", (
                f"Flowly restarted {adopted.resume_count} times while this task ran without it "
                "finishing another step, so it was stopped where it was; nothing it had "
                "started was run again."
            )))
        elif state is not None:
            claimed = self._registry.claim(
                adopted.run_id, adopted.owner, adopted.owner,
                resume_count=adopted.resume_count + 1,
                suspended_seconds=adopted.suspended_seconds + max(0.0, now - last_seen),
                suspended_at=None, resumed_at=now, activity={"phase": "resuming"},
            )
            if claimed is not None:
                continued.append((claimed, state))
        elif refusal == "version":
            settled.append((adopted, "interrupted", "resume_unsupported", (
                "Flowly was updated while this task ran, and this version cannot continue "
                "work saved by the other one. It was stopped where it was; nothing it had "
                "started was run again."
            )))
        elif refusal == "base":
            settled.append((adopted, "interrupted", "resume_unsupported", (
                "The subagent's saved conversation no longer matches the one this task started "
                "from, so it cannot be continued. It was stopped where it was; nothing it had "
                "started was run again."
            )))
        else:
            settled.append((adopted, "error", "process_restarted", None))

    def _restorable(self, record: SubagentRunRecord) -> tuple[ChildState | None, str | None]:
        """The state a run's checkpoint rebuilds, or why there is none."""
        if not record.resumable:
            return None, "not_resumable"
        try:
            records = read_checkpoint(self._registry.checkpoint_path(record.run_id))
        except OSError:
            logger.exception(f"[SubagentManager] [{record.run_id[:8]}] checkpoint unreadable")
            return None, "unreadable"
        base = None
        if records and int(records[0].get("baseCount") or 0) > 0 and record.thread_id:
            # A later turn: the thread's history it started from, as its
            # model gets it now (checked against the header's digest).
            try:
                base = self._registry.thread_store.turn_context(record.thread_id).history
            except Exception:  # noqa: BLE001 — an unreadable history refuses the replay
                logger.exception(f"[SubagentManager] [{record.run_id[:8]}] thread history unreadable")
        restored = restore_child_state(records, annotate=annotate_search_repetition, base=base)
        if isinstance(restored, ResumeRefusal):
            if restored.reason != "missing":
                logger.warning(
                    f"[SubagentManager] [{record.run_id[:8]}] checkpoint cannot be continued: "
                    f"{restored.reason} {restored.detail}"
                )
            return None, restored.reason
        return restored, None

    def _resume(self, record: SubagentRunRecord, state: ChildState) -> None:
        """Continue a run a restart stopped, from its rebuilt state.

        Calls of its last reply that have no result are answered — never
        run again: a call that had not started is "not run", one that had
        started gets a factual note for its kind of tool (subagent_checkpoint
        .interrupted_result). Messages from the main agent it had not read
        are given back. The transcript gets those calls and a ``resumed``
        item; the checkpoint a ``resumed`` line.
        """
        run_id = record.run_id
        now = time.time()
        after = "shutdown" if state.suspended else "crash"
        self._checkpoints[run_id] = SubagentCheckpoint(self._registry.checkpoint_path(run_id))
        interrupted: list[str] = []
        not_run: list[str] = []
        for call in state.open_calls:
            status, text = interrupted_result(call)
            (not_run if status == "not_run" else interrupted).append(call.id)
            self._answer_tool(run_id, state, call.id, call.name, text, status=status)
            self._transcribe(run_id, {
                "kind": "tool", "toolCallId": call.id, "name": call.effective or call.name,
                "status": status, "content": text,
            })
        if any(step.get("status") == "running" for step in record.tool_trace):
            trace = [dict(step, status="interrupted") if step.get("status") == "running" else step
                     for step in record.tool_trace]
            self._registry.update(run_id, tool_trace=trace)
        state.open_calls = []
        attempt = record.resume_count
        self._log(run_id, "resumed", durable=True, after=after, attempt=attempt,
                  interrupted=interrupted, notRun=not_run)
        self._transcribe(run_id, {"kind": "resumed", "at": now, "attempt": attempt, "after": after})
        if state.unread_mail:
            self._inboxes[run_id] = list(state.unread_mail)
            self._inbox_events.setdefault(run_id, asyncio.Event()).set()
        state.suspended = False
        logger.info(
            f"[SubagentManager] [{run_id[:8]}] continuing after a {after} (attempt {attempt}; "
            f"{len(interrupted)} call(s) interrupted, {len(not_run)} not run)"
        )
        self._start_run_task(run_id, record.parent_session_key, self._run_subagent(
            run_id, record.task, record.label, record.parent_channel, record.parent_chat_id,
            record.model or self.model, None, restored=state,
        ))
        self._publish_event("subagent.progress", run_id)

    # ------------------------------------------------------------------
    # Public spawn
    # ------------------------------------------------------------------

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        model: str | None = None,
        cleanup: str = "keep",
        timeout_seconds: int | None = None,
        is_subagent_caller: bool = False,
        wait: bool = False,
        silent: bool = False,
        display_name: str | None = None,
        origin: str = "system",
        context: str | None = None,
        parent_session_key: str | None = None,
        batch_id: str = "",
    ) -> str:
        """
        Spawn a subagent to execute a task in the background.

        Args:
            task: The task description for the subagent.
            label: Optional human-readable label for the task.
            origin_channel: The channel to announce results to.
            origin_chat_id: The chat ID to announce results to.
            model: Optional model override for this subagent.
            cleanup: "keep" or "delete" (registry cleanup policy).
            timeout_seconds: Optional execution timeout.
            is_subagent_caller: True if spawner is itself a subagent (loop prevention).
            silent: If True, suppress the completion announce to the parent
                session. Used for fire-and-forget internal work (self-review
                memory/KG writes) where the child's output is not meant
                for the user and waking the parent LLM with an announce
                triggers a wasteful extra turn.
            origin: Who started it — "owner" (the owner asked), "agent" (the
                agent split its work) or "system" (self-review, Board…).
                Clients show owner and agent runs in the conversation.
            context: The conversation the child may read before its task.
            parent_session_key: The starting conversation's own key when it
                is not ``origin_channel:origin_chat_id`` (a desktop chat);
                results and messages return to that conversation.
            batch_id: Runs started in the same turn share it; their results
                reach the conversation together, once the last has finished.

        Returns:
            JSON status string.
        """
        # Loop prevention: subagents cannot spawn subagents
        if is_subagent_caller:
            logger.warning("[SubagentManager] Blocked recursive spawn attempt")
            return json.dumps({
                "status": "forbidden",
                "error": "Subagents cannot spawn other subagents",
            })

        parent_session_key = parent_session_key or f"{origin_channel}:{origin_chat_id}"
        refused = self._admission_error(parent_session_key, origin)
        if refused:
            return json.dumps({"status": "rejected", "error": refused})
        siblings = self._running_siblings(parent_session_key)

        run_id = str(uuid.uuid4())
        child_session_key = f"subagent:{run_id}"
        # ``label`` is the internal/dedup key (may be a code like
        # "builtin:researcher"). ``display_name`` is what users see and is ALWAYS
        # task-derived — never a code/UUID. Priority: explicit display_name →
        # a non-code label → a title derived from the task.
        display_label = label or _derive_display_name(task)
        resolved_display = (
            (display_name or "").strip()
            or (label if label and not _looks_like_code(label) else "")
            or _derive_display_name(task)
        )
        resolved_model = model or self.model

        record = SubagentRunRecord(
            run_id=run_id,
            child_session_key=child_session_key,
            parent_session_key=parent_session_key,
            parent_channel=origin_channel,
            parent_chat_id=origin_chat_id,
            task=task,
            label=display_label,
            display_name=resolved_display,
            model=resolved_model,
            cleanup=cleanup,
            created_at=time.time(),
            activity={"phase": "queued"},
            delivery_state="not_required" if wait or silent else "pending",
            origin=origin if origin in ("owner", "agent", "system") else "system",
            mark=assign_mark(run_id, {
                str(rec.mark.get("glyph")) for rec in siblings if rec.mark
            }),
            batch_id=batch_id,
            owner=subagent_owner.process_owner(self._instance),
            # Continued after a restart: work a conversation waits for, in a
            # process that continues runs. A run waited for inline, a silent
            # one, system bookkeeping or one with a wall clock is not.
            resumable=bool(
                self.resume_enabled and origin in ("owner", "agent")
                and not (wait or silent) and not timeout_seconds
            ),
        )
        # Every subagent working for the owner is a thread (subagent_threads):
        # its own conversation, readable by every client, written only by its
        # runs. System bookkeeping (self-review, Board) holds the owner's
        # conversation and memory and gets none.
        thread = None
        if record.origin != "system":
            record.thread_id = run_id
            thread = SubagentThreadRecord(
                thread_id=run_id, parent_session_key=parent_session_key,
                child_session_key=child_session_key, created_at=record.created_at,
                origin=record.origin, title=resolved_display, mark=dict(record.mark),
                model=resolved_model,
            )
        try:
            made_room = self._registry.register(
                record, thread=thread,
                open_cap=MAX_OPEN_THREADS_PER_CONVERSATION if thread is not None else None,
            )
        except ThreadCapReached:
            return json.dumps({
                "status": "rejected",
                "error": (
                    f"{MAX_OPEN_THREADS_PER_CONVERSATION} subagents of this conversation are "
                    "open and all of them are working; one must finish, or be closed with "
                    "agent_close, before another can start."
                ),
            })
        # Closed to make room: their runs' views now say so.
        self._publish_thread_runs(made_room)

        logger.info(f"[SubagentManager] Spawned [{run_id[:8]}]: {resolved_display}")

        await self._emit_event("subagent.started", run_id, resolved_display, "running")
        bg_task = self._start_run_task(run_id, parent_session_key, self._run_subagent(
            run_id, task, display_label, origin_channel, origin_chat_id,
            resolved_model, timeout_seconds,
            skip_announce=wait or silent, context=context,
        ))
        if wait:
            return await bg_task

        # The parent keeps working; the result arrives as a message. The
        # wording stops a parent from answering from memory meanwhile.
        message = (
            f"'{resolved_display}' is running in the background. When everything "
            "you started in this turn has finished, the results reach you "
            "together as a new message and you continue from there. Don't wait "
            "for them, don't redo their tasks and don't guess their results: do "
            "any work that does not depend on them, then tell the owner briefly "
            "what is running and end your turn."
        )
        if made_room:
            # A fact about the conversation's subagents, not advice.
            names = ", ".join(f"'{t.title}'" for t in made_room)
            whose = "Its" if len(made_room) == 1 else "Their"
            message += (
                f" This conversation keeps at most {MAX_OPEN_THREADS_PER_CONVERSATION} "
                f"subagents open; starting this one closed {names}, idle the longest. "
                f"{whose} history stays readable. agent_close closes a subagent that is "
                "no longer needed."
            )
        started: dict[str, Any] = {"status": "started", "run_id": run_id}
        if record.thread_id:
            # The subagent itself: agent_followup and agent_close take it.
            started["agent_id"] = record.thread_id
        started.update(title=resolved_display, model=resolved_model, message=message)
        return json.dumps(started)

    # ------------------------------------------------------------------
    # A subagent's later turns (Codex followup_task), closing, mail
    # ------------------------------------------------------------------

    def resolve_thread(self, target: str, parent_session_key: str | None
                       ) -> tuple[SubagentThreadRecord | None, str]:
        """One of this conversation's subagents, by ``agent_id`` or a run id of any of its tasks."""
        from flowly.agent.tools.agent_tools import resolve_own_thread

        return resolve_own_thread(self._registry, target, parent_session_key)

    def _thread_runs(self, thread: SubagentThreadRecord) -> list[SubagentRunRecord]:
        return self._registry.runs_of_thread(thread)

    def _publish_thread_runs(self, threads: list[SubagentThreadRecord] | None) -> None:
        """A thread's status changed: its ended runs' views (``threadStatus``) go out again."""
        for thread in threads or []:
            for run in self._thread_runs(thread):
                if run.ended_at is not None:
                    self._publish_event("subagent.completed", run.run_id)

    async def followup(
        self,
        target: str,
        task: str,
        *,
        parent_session_key: str | None,
        origin: str = "agent",
        batch_id: str = "",
        model: str | None = None,
        origin_channel: str = "",
        origin_chat_id: str = "",
    ) -> dict[str, Any]:
        """Give one of this conversation's subagents a new task (Codex ``followup_task``).

        * **Nothing running on it:** a new turn of its thread starts — a run
          with ``trigger: "followup"`` and the next ``turn`` — from its own
          history: the model gets the thread's conversation (its earlier tasks,
          tool calls, results and reports), then this task. It reports like a
          spawn, together with whatever else this parent turn started
          (``batch_id``), and counts against the same limits.
        * **A task running on it:** the running turn reads this task at its
          next step, labeled ``[Another task from the main agent]``; its report covers
          it. A task that arrives too late to be read starts the next turn
          when that one ends.
        * **Closed:** refused, with the reason (``closed_text``).

        ``origin`` labels the run (``owner`` when the agent quoted the owner
        asking, see delegation); ``model`` is the owner's named model, else
        the conversation's own (``self.model``).
        """
        text = (task or "").strip()
        if not text:
            return {"status": "error", "error": "The task is empty."}
        thread, problem = self.resolve_thread(target, parent_session_key)
        if thread is None:
            return {"status": "error", "error": problem}
        agent_id = thread.thread_id
        title = thread.title or "subagent"
        if thread.status != "open":
            return {"status": "closed", "agent_id": agent_id, "title": title, "error": closed_text(thread)}
        origin = origin if origin in ("owner", "agent") else "agent"
        if thread.active_run_id:
            current = self._registry.get(thread.active_run_id)
            if current is not None and current.ended_at is None and current.run_id in self._running_tasks:
                sent = await self.send_to_child(current.run_id, text, kind="task", batch_id=batch_id,
                                                origin=origin)
                if sent.get("status") == "delivered":
                    return {
                        "status": "added_to_running_task", "run_id": current.run_id,
                        "agent_id": agent_id, "turn": current.turn, "title": title,
                        "message": (
                            f"'{title}' is working on its current task; it reads this task at its "
                            "next step, and the report of the task it is on covers this one too."
                        ),
                    }
            refreshed = self._registry.get_thread(agent_id)
            if refreshed is None or refreshed.status != "open":
                return {"status": "closed", "agent_id": agent_id, "title": title,
                        "error": closed_text(refreshed or thread)}
            if refreshed.active_run_id:
                return {"status": "busy", "agent_id": agent_id, "title": title, "error": (
                    f"'{title}' is recorded as working on a task that no process here is running "
                    "(it may be about to continue after a restart); it takes a new task once "
                    "that one reports."
                )}
            thread = refreshed
        refused = self._admission_error(thread.parent_session_key, origin)
        if refused:
            return {"status": "rejected", "agent_id": agent_id, "error": refused}
        runs = self._thread_runs(thread)
        last = runs[-1] if runs else None
        resolved_model = (model or "").strip() or self.model
        now = time.time()
        run_id = str(uuid.uuid4())
        record = SubagentRunRecord(
            run_id=run_id,
            child_session_key=thread.child_session_key,
            parent_session_key=thread.parent_session_key,
            parent_channel=origin_channel or (last.parent_channel if last else "cli"),
            parent_chat_id=origin_chat_id or (last.parent_chat_id if last else "direct"),
            task=text,
            label=title,
            display_name=title,
            model=resolved_model,
            cleanup="keep",
            created_at=now,
            activity={"phase": "queued"},
            delivery_state="pending",
            origin=origin,
            # The mark is the thread's: every turn of it wears the same one.
            mark=dict(thread.mark),
            batch_id=batch_id,
            owner=subagent_owner.process_owner(self._instance),
            resumable=bool(self.resume_enabled),
            thread_id=agent_id,
            turn=len(thread.runs) + 1,
            trigger="followup",
        )
        try:
            self._registry.register(record)
        except ValueError:
            current = self._registry.get_thread(agent_id)
            if current is not None and current.status != "open":
                return {"status": "closed", "agent_id": agent_id, "title": title, "error": closed_text(current)}
            return {"status": "busy", "agent_id": agent_id, "title": title,
                    "error": f"'{title}' has just started another task; give it this one again."}
        record = self._registry.get(run_id) or record
        logger.info(f"[SubagentManager] Follow-up [{run_id[:8]}] task {record.turn} of [{agent_id[:8]}]: {title}")
        await self._emit_event("subagent.started", run_id, title, "running")
        self._start_run_task(run_id, record.parent_session_key, self._run_subagent(
            run_id, text, title, record.parent_channel, record.parent_chat_id, resolved_model, None,
        ))
        return {
            "status": "started", "run_id": run_id, "agent_id": agent_id, "turn": record.turn,
            "title": title, "model": resolved_model,
            "message": (
                f"'{title}' is working on this task in the background (task {record.turn}), with "
                "its earlier work in its history. When everything you started in this turn has "
                "finished, the results reach you together as a new message and you continue "
                "from there. Don't wait for them, don't redo their tasks and don't guess their "
                "results: do any work that does not depend on them, then tell the owner briefly "
                "what is running and end your turn."
            ),
        }

    async def close_threads(self, targets: list[str] | str, *, parent_session_key: str | None,
                            reason: str = "agent") -> dict[str, Any]:
        """Close subagents of this conversation (``agent_close``): ids, or ``"all"`` open ones.

        A closed subagent takes no new task or message; its history stays
        readable. A task running on it is stopped (outcome ``cancelled``, no
        report — the caller knows).
        """
        if parent_session_key is None:
            return {"error": "No conversation owns this call, so it has no subagents."}
        if targets == "all":
            chosen = [t for t in self._registry.threads()
                      if t.parent_session_key == parent_session_key and t.status == "open"]
            if not chosen:
                return {"closed": [], "note": "No subagent of this conversation is open."}
        else:
            chosen = []
            for target in ([targets] if isinstance(targets, str) else list(targets or [])):
                thread, problem = self.resolve_thread(str(target), parent_session_key)
                if thread is None:
                    return {"error": problem}
                if thread.thread_id not in {t.thread_id for t in chosen}:
                    chosen.append(thread)
        results: list[dict[str, Any]] = []
        for thread in chosen:
            entry: dict[str, Any] = {"agent_id": thread.thread_id, "title": thread.title}
            closed = self._registry.close_thread(thread.thread_id, reason)
            if closed is None:
                current = self._registry.get_thread(thread.thread_id) or thread
                entry.update(status="already_closed", reason=current.closed_reason)
                results.append(entry)
                continue
            entry["status"] = "closed"
            running = closed.active_run_id
            if running and running in self._running_tasks:
                await self.cancel(running)
                entry["stopped_run_id"] = running
            elif running:
                # Recorded running with no process here: ended now.
                await self.cancel(running)
            self._publish_thread_runs([closed])
            logger.info(f"[SubagentManager] Closed [{thread.thread_id[:8]}] ({reason}): {thread.title}")
            results.append(entry)
        return {"closed": results}

    async def message_thread(self, thread: SubagentThreadRecord, text: str) -> dict[str, Any]:
        """A message from the main agent to one of its subagents.

        Running: its running task reads it at its next step (``delivered``).
        Open with nothing running: kept for its next task, which starts with
        it (``queued``) — no task starts. Closed: refused.
        """
        if thread.status != "open":
            return {"agent_id": thread.thread_id, "status": "closed", "error": closed_text(thread)}
        if thread.active_run_id and thread.active_run_id in self._running_tasks:
            sent = await self.send_to_child(thread.active_run_id, text)
            if sent.get("status") == "delivered":
                return sent
        kept, why = self._registry.queue_mail(thread.thread_id, text)
        if kept is not None:
            return {"agent_id": thread.thread_id, "status": "queued", "note": (
                f"'{thread.title}' has no task running; it reads this when it gets its next "
                "task (agent_followup). No task was started."
            )}
        if why == "closed":
            current = self._registry.get_thread(thread.thread_id) or thread
            return {"agent_id": thread.thread_id, "status": "closed", "error": closed_text(current)}
        if why == "running":
            current = self._registry.get_thread(thread.thread_id)
            if current is not None and current.active_run_id in self._running_tasks:
                return await self.send_to_child(current.active_run_id, text)
            return {"agent_id": thread.thread_id, "status": "busy", "error": (
                f"'{thread.title}' is recorded as working on a task that no process here is running; "
                "it takes messages once that one reports."
            )}
        if why == "full":
            return {"agent_id": thread.thread_id, "status": "full", "error": (
                f"'{thread.title}' already keeps the most messages it can for its next task."
            )}
        return {"agent_id": thread.thread_id, "status": "not_found", "error": "Subagent not found."}

    def _stash_unread(self, run_id: str) -> None:
        """What reached a turn too late to be read is kept for the thread's next turn — durably.

        Called once the run's end is recorded and before its checkpoint goes
        (whose ``mail`` lines were the only durable record of it): messages
        and tasks the run never read move to the thread's ``pending_mail``.
        A task is kept only when the turn ended on its own (ok, error,
        timeout): a turn stopped — on request, by the owner stopping the
        conversation, by a shutdown that could not keep it — had its work
        stopped too. Nothing is kept for a closed thread or a run with none.
        Entries that cannot be kept yet (the run's end is not recorded) stay
        in the inbox for the run's cleanup to try again.
        """
        entries = self._inboxes.pop(run_id, None) or []
        event = self._inbox_events.get(run_id)
        if event is not None:
            event.clear()
        if not entries:
            return
        record = self._registry.get(run_id)
        thread = self._registry.get_thread(record.thread_id) if record is not None and record.thread_id else None
        if record is None or thread is None or thread.status != "open":
            logger.info(f"[SubagentManager] [{run_id[:8]}] {len(entries)} unread message(s) dropped: "
                        "the subagent has no open thread")
            return
        keep_tasks = record.ended_at is not None and record.outcome in ("ok", "error", "timeout")
        left: list[dict[str, Any]] = []
        for entry in entries:
            task = entry.get("kind") == "task"
            if task and record.ended_at is not None and not keep_tasks:
                logger.info(f"[SubagentManager] [{run_id[:8]}] an unread task dropped: the task was stopped")
                continue
            kept, why = self._registry.queue_mail(
                thread.thread_id, str(entry.get("text") or ""),
                kind="task" if task else "message", batch=str(entry.get("batch") or ""),
                origin=str(entry.get("origin") or "agent"),
            )
            if kept is None and why == "running":
                left.append(entry)
            elif kept is None:
                logger.warning(f"[SubagentManager] [{run_id[:8]}] an unread message could not be kept ({why})")
        if left:
            self._inboxes[run_id] = left

    def _schedule_pending_tasks(self, parent_session_key: str) -> None:
        task = asyncio.ensure_future(self._start_pending_tasks(parent_session_key))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

    async def _start_pending_tasks(self, parent_session_key: str | None = None) -> None:
        """Start the tasks kept for idle subagents (``_stash_unread``) as their next turns.

        The first kept task of a thread starts its turn (a follow-up with the
        batch and origin it was given under); the rest join it. A task that
        cannot start now (the limits, a shutdown) is kept again and tried when
        a task of the conversation ends, or at the next start.
        """
        if self._shutting_down:
            return
        for thread in self._registry.threads():
            if parent_session_key is not None and thread.parent_session_key != parent_session_key:
                continue
            if thread.status != "open" or thread.active_run_id or not any(
                m.get("kind") == "task" for m in thread.pending_mail
            ):
                continue
            tasks = self._registry.take_pending_tasks(thread.thread_id)
            if not tasks:
                continue
            last = self._thread_runs(thread)[-1:] or [None]
            first = tasks[0]
            outcome = await self.followup(
                thread.thread_id, str(first.get("text") or ""),
                parent_session_key=thread.parent_session_key,
                origin=str(first.get("origin") or "agent"), batch_id=str(first.get("batch") or ""),
                origin_channel=last[0].parent_channel if last[0] else "",
                origin_chat_id=last[0].parent_chat_id if last[0] else "",
            )
            if outcome.get("status") != "started":
                self._registry.requeue_tasks(thread.thread_id, tasks)
                logger.info(f"[SubagentManager] [{thread.thread_id[:8]}] kept task(s) wait: "
                            f"{outcome.get('error') or outcome.get('status')}")
                continue
            for entry in tasks[1:]:
                await self.send_to_child(outcome["run_id"], str(entry.get("text") or ""), kind="task",
                                         batch_id=str(entry.get("batch") or ""),
                                         origin=str(entry.get("origin") or "agent"))

    # ------------------------------------------------------------------
    # Internal execution (core loop preserved from original)
    # ------------------------------------------------------------------

    def _admission_error(self, parent_session_key: str, origin: str) -> str | None:
        """Why no new task (a spawn or a follow-up) can start now, or None.

        Budget guards, the same for both: a shutdown, the process's limit and
        the conversation's (system work is not counted against the latter).
        """
        if self._shutting_down:
            return "Flowly is shutting down; no new subagent can start now."
        # Concurrency limits — a runaway parent can't pin the event loop, and
        # one conversation can't take every slot from the others.
        if len(self._running_tasks) >= _MAX_CONCURRENT_SUBAGENTS:
            logger.warning(f"[SubagentManager] Concurrent limit reached ({_MAX_CONCURRENT_SUBAGENTS})")
            return (
                f"Maximum {_MAX_CONCURRENT_SUBAGENTS} subagents are running. "
                "Wait for one to finish, or do this part yourself."
            )
        if origin != "system" and len(self._running_siblings(parent_session_key)) >= MAX_CHILDREN_PER_CONVERSATION:
            return (
                f"{MAX_CHILDREN_PER_CONVERSATION} subagents are already running "
                "for this conversation. Wait for one to finish, or do this part "
                "yourself."
            )
        return None

    def _start_run_task(self, run_id: str, parent_session_key: str, run: Any) -> asyncio.Task:
        """Run a child (new or continued) as this process's task for ``run_id``."""
        bg_task = asyncio.create_task(run)
        self._running_tasks[run_id] = bg_task
        self._children_by_parent.setdefault(parent_session_key, set()).add(run_id)

        def _cleanup(_task: asyncio.Task, _rid: str = run_id, _pk: str = parent_session_key) -> None:
            try:
                # A task cancelled before its first instruction has no handler.
                rec = self._registry.get(_rid)
                if rec and rec.ended_at is None and not (_task.cancelled() and self._suspends(_rid)):
                    outcome = self._cancel_outcomes.get(_rid, "interrupted") if _task.cancelled() else "error"
                    if _task.cancelled():
                        self._finish_cancelled(_rid, outcome)
                    else:
                        self._registry.finish(_rid, outcome, error_code="worker_failed",
                                              error="Task could not finish")
                    self._record_metric(_rid)
                    self._publish_event("subagent.completed", _rid)
                    ended = self._registry.get(_rid)
                    # Its siblings may be holding their results for it, and a
                    # failure is still news to the conversation.
                    if ended and (ended.batch_id or ended.delivery_state in ("pending", "queued")):
                        report = asyncio.ensure_future(self._announce(_rid, ended.error or "", outcome))
                        report.add_done_callback(
                            lambda t: t.exception() if not t.cancelled() and t.exception() else None
                        )
            except Exception:
                logger.exception("Could not persist task cleanup")
            finally:
                if not _task.cancelled():
                    _task.exception()
                suspended = _task.cancelled() and self._suspends(_rid)
                self._settle_stream(_rid, suspended=suspended)
                self._cancel_outcomes.pop(_rid, None)
                self._running_tasks.pop(_rid, None)
                if suspended:
                    # Its unread mail is in its checkpoint; the restart reads it.
                    self._inboxes.pop(_rid, None)
                else:
                    # Anything still unread (a run that ended before its
                    # handler could keep it) is kept now; kept tasks start.
                    try:
                        self._stash_unread(_rid)
                    except Exception:
                        logger.exception("Could not keep a subagent's unread messages")
                    self._inboxes.pop(_rid, None)
                    self._schedule_pending_tasks(_pk)
                self._inbox_events.pop(_rid, None)
                self._checkpoints.pop(_rid, None)
                self._wake(_pk)
                siblings = self._children_by_parent.get(_pk)
                if siblings is not None:
                    siblings.discard(_rid)
                    if not siblings:
                        self._children_by_parent.pop(_pk, None)

        bg_task.add_done_callback(_cleanup)
        return bg_task

    def _finish_cancelled(self, run_id: str, outcome: str) -> None:
        """Record a run stopped by a cancel, a cascade or a shutdown.

        A run stopped on request needs no report. One a gateway's shutdown
        interrupted although its conversation waits for it (its checkpoint
        could not be kept, so it cannot be continued) is reported at the
        next start instead of vanishing.
        """
        record = self._registry.get(run_id)
        lost = (
            outcome == "interrupted" and self._shutting_down and self.resume_enabled
            and record is not None and record.origin != "system"
            and record.delivery_state != "not_required"
        )
        if lost:
            self._registry.finish(
                run_id, outcome, error_code="shutdown", report=True,
                error="Flowly shut down while this task ran, and it could not be continued.",
            )
        else:
            self._registry.finish(run_id, outcome, error="Task interrupted", error_code="cancelled")

    def _suspends(self, run_id: str) -> bool:
        """True while a clean shutdown stops this run to continue it later."""
        if not self._suspending or self._cancel_outcomes.get(run_id) == "cancelled":
            return False  # stopped on request: that wins over the shutdown
        record = self._registry.get(run_id)
        return record is not None and record.ended_at is None and record.resumable

    async def _run_subagent(
        self,
        run_id: str,
        task: str,
        label: str,
        origin_channel: str,
        origin_chat_id: str,
        model: str,
        timeout_seconds: int | None,
        skip_announce: bool = False,
        context: str | None = None,
        restored: ChildState | None = None,
    ) -> str:
        """Execute the subagent task and optionally announce the result.

        If ``skip_announce`` is True, the caller gets the final result text
        back directly and `_announce` (which enqueues an InboundMessage to
        the parent session so the parent agent can summarise the child's
        output) is skipped. Sync-mode spawn uses this so the cron agent
        receives the child's response inline instead of as a separate
        system message on a later turn.

        ``restored`` continues a run a restart stopped, from the state its
        checkpoint rebuilt (see resume_pending); the run keeps its start.
        """
        logger.info(f"[SubagentManager] [{run_id[:8]}] {'continuing' if restored else 'starting'}: {label}")
        if restored is None:
            self._registry.update(run_id, started_at=time.time())
        record = self._registry.get(run_id)
        if record is not None and record.resumable and run_id not in self._checkpoints:
            self._checkpoints[run_id] = SubagentCheckpoint(self._registry.checkpoint_path(run_id))

        final_result: str = ""
        status: str = "ok"
        error_str: str | None = None
        error_code: str | None = None
        # P1.2 — structured tool trace, one dict per call
        # (args_bytes / result_bytes / status / duration_ms) so the
        # parent LLM and registry audit know exactly what happened —
        # not just which tools fired. A continued run keeps its own.
        tool_trace: list[dict[str, Any]] = (
            [dict(step) for step in record.tool_trace] if restored is not None and record else []
        )
        suspended = False
        # The run's state, for saving its turn into its thread however it ends.
        turn: dict[str, Any] = {}

        try:
            run = self._child_run(
                run_id, task, label, origin_channel, origin_chat_id, model,
                timeout_seconds, context, tool_trace, restored, turn,
            )
            # Wall-clock guard is opt-in: positive timeout_seconds enforces
            # a hard cap; 0 / None runs unbounded (rely on max_iter +
            # per-call + heartbeat).
            if timeout_seconds and timeout_seconds > 0:
                final_result, status, error_str, error_code = await asyncio.wait_for(
                    run, timeout=timeout_seconds,
                )
            else:
                final_result, status, error_str, error_code = await run

            durable_result = final_result

            # Classify the outcome. The child loop signals LLM-error
            # exhaustion by returning final_result starting with "Error: Task
            # failed" — that's not an exception, so without this check the
            # registry would record outcome="ok" even when every LLM call hit
            # a 504 and the parent receives a bare error string.
            if status == "error" or (final_result and final_result.startswith("Error: Task failed")):
                status = "error"
                error_str = final_result
                logger.warning(
                    f"[SubagentManager] [{run_id[:8]}] completed with errors"
                )
            else:
                logger.info(f"[SubagentManager] [{run_id[:8]}] completed successfully")
            # The result is in the checkpoint before the registry records it:
            # a crash in between finishes the run with it, no model call.
            self._log(run_id, "final", durable=True, status=status, error=error_str,
                      errorCode=error_code, result=durable_result)
            # The thread's conversation has the turn before the registry has
            # the result: a crash in between records the result again from the
            # checkpoint, and the turn is not saved twice.
            self._save_turn(run_id, turn, result=durable_result, outcome=status)
            self._registry.finish(run_id, status, result=durable_result,
                                  error=error_str, error_code=error_code)
            # Watchers get the terminal once the turn is saved: a reload of
            # the conversation then has it.
            self._end_stream(run_id, status, durable_result, error_code)

        except asyncio.TimeoutError:
            logger.warning(f"[SubagentManager] [{run_id[:8]}] timed out")
            self._save_turn(run_id, turn, result="(task timed out)", outcome="timeout")
            self._registry.finish(run_id, "timeout", error="Task timed out", error_code="timeout")
            self._end_stream(run_id, "timeout", "Task timed out", "timeout")
            final_result = "(task timed out)"
            status = "timeout"

        except asyncio.CancelledError:
            if self._suspends(run_id):
                # A clean shutdown: the run is stopped to be continued after
                # the restart, not finished. Nothing is announced.
                suspended = True
                logger.info(f"[SubagentManager] [{run_id[:8]}] suspended for the restart")
                self._log(run_id, "suspended", durable=True, reason=self._suspend_reason)
                # It goes on after the restart (its watchers re-enter then).
                self._settle_stream(run_id, suspended=True)
                raise
            # Cancelled via cancel_children (parent interrupt cascade) or
            # cancel() (explicit user cancel). Persist the outcome and
            # announce to the parent so the user sees what happened —
            # then re-raise so asyncio considers the task properly
            # cancelled and doesn't treat it as a "completed" future.
            logger.info(f"[SubagentManager] [{run_id[:8]}] cancelled")
            outcome = self._cancel_outcomes.get(run_id, "interrupted")
            self._save_turn(run_id, turn, result="", outcome=outcome, aborted=True)
            self._finish_cancelled(run_id, outcome)
            self._end_stream(run_id, outcome, "")
            self._transcribe(run_id, {"kind": "result", "status": outcome, "content": ""})
            if not skip_announce:
                try:
                    await self._announce(run_id, "(task was interrupted)", outcome)
                except Exception:
                    logger.exception(f"[SubagentManager] [{run_id[:8]}] announce on cancel failed")
            try:
                await self._emit_event("subagent.completed", run_id, label, outcome, None)
            except Exception:
                pass
            raise

        except Exception as e:
            logger.error(f"[SubagentManager] [{run_id[:8]}] failed: {e}")
            self._save_turn(run_id, turn, result="Error: Task could not finish.", outcome="error")
            self._registry.finish(run_id, "error", error="Task could not finish", error_code="execution_failed")
            self._end_stream(run_id, "error", "Error: Task could not finish.", "execution_failed")
            final_result = "Error: Task could not finish."
            status = "error"
            error_str = final_result

        finally:
            if not suspended:
                # What it never read is kept for the thread's next turn before
                # the checkpoint (its only durable record) goes.
                try:
                    self._stash_unread(run_id)
                except Exception:
                    logger.exception(f"[SubagentManager] [{run_id[:8]}] could not keep its unread messages")
                # The result (or the end) is durable in the registry now.
                self._drop_checkpoint(run_id)

        self._transcribe(run_id, {
            "kind": "result", "status": status, "content": _clip(final_result or "", 8_000),
        })
        try:
            if not skip_announce:
                await self._announce(run_id, final_result, status)
        finally:
            await self._emit_event("subagent.completed", run_id, label, status, error_str)
        return final_result

    # ------------------------------------------------------------------
    # A thread turn's live events (flowly.agent.subagent_stream)
    # ------------------------------------------------------------------

    async def _open_stream(self, run_id: str, state: ChildState, *, resumed: bool) -> ChildStream | None:
        """Start the live events of a thread's turn; None for a run with no thread.

        System work and delegated tasks have no conversation, and a run whose
        conversation was deleted has nothing left to show.
        """
        record = self._registry.get(run_id)
        if record is None or not record.thread_id:
            return None
        thread = self._registry.get_thread(record.thread_id)
        if thread is None or thread.closed_reason == "parent_deleted":
            return None
        try:
            access = child_event_access(record.parent_session_key)
        except Exception:  # noqa: BLE001 — never stream without knowing whose it is
            from flowly.live_voice.events import EventAccess

            access = EventAccess(blocked=True)
        stream = ChildStream(
            self.stream_sink, run_id=run_id, session_key=thread.child_session_key,
            thread_id=thread.thread_id, parent_session_key=record.parent_session_key,
            turn=record.turn, trigger=record.trigger, title=record.display_name or thread.title,
            mark=record.mark, model=record.model or self.model, brief=record.task,
            with_conversation=_with_conversation(record, state),
            access=access, started_at=record.started_at,
        )
        self._streams[run_id] = stream
        await stream.start(state.journal, resumed=resumed)
        return stream

    async def _stream_sync(self, run_id: str, state: ChildState) -> None:
        stream = self._streams.get(run_id)
        if stream is not None:
            await stream.sync(state.journal)

    def _end_stream(self, run_id: str, outcome: str, result: str, error_code: str | None = None) -> None:
        """The turn's one terminal event, once its result is recorded."""
        stream = self._streams.pop(run_id, None)
        if stream is None:
            return
        record = self._registry.get(run_id)
        duration_ms = None
        if record is not None and record.started_at is not None:
            end = record.ended_at or time.time()
            duration_ms = int(max(0.0, end - record.started_at - (record.suspended_seconds or 0.0)) * 1000)
        try:
            stream.end(outcome, result, usage=dict(record.usage) if record is not None else {},
                       error_code=error_code, duration_ms=duration_ms)
        except Exception:  # noqa: BLE001 — the stream never fails the run
            logger.debug(f"[SubagentManager] [{run_id[:8]}] stream terminal failed", exc_info=True)

    def _settle_stream(self, run_id: str, *, suspended: bool = False) -> None:
        """A run's task is over: a stream it left open ends now.

        Suspended for a restart: the entry goes, no terminal (the run goes
        on). Otherwise the run ended some way its own path did not report
        (a worker failure): its recorded outcome is the terminal.
        """
        stream = self._streams.get(run_id)
        if stream is None:
            return
        if suspended:
            self._streams.pop(run_id, None)
            stream.close()
            return
        record = self._registry.get(run_id)
        outcome = (record.outcome if record is not None and record.outcome else "error")
        text = ((record.result_preview if outcome == "ok" else record.error)
                if record is not None else None) or ""
        self._end_stream(run_id, outcome, text, record.error_code if record is not None else None)

    def _save_turn(self, run_id: str, turn: dict[str, Any], *, result: str, outcome: str,
                   aborted: bool = False) -> None:
        """Save what the run did as a turn of its thread's conversation.

        Called once the run's result is decided, before the registry records
        it, whatever the outcome; a stopped run is saved as ``aborted``, and
        a call it left without a result gets a factual one (it may have run,
        or it did not start), so the conversation stays well formed. A summary
        the turn made of its context is kept for the next turn
        (``persist_context``). A run with no thread (system work, delegated
        tasks) or whose conversation was deleted saves nothing. Saving never
        fails the run.
        """
        state = turn.get("state")
        record = self._registry.get(run_id)
        if state is None or record is None or not record.thread_id:
            return
        thread = self._registry.get_thread(record.thread_id)
        if thread is None or thread.closed_reason == "parent_deleted":
            return
        duration_ms = None
        if record.started_at is not None:
            duration_ms = int(max(0.0, time.time() - record.started_at
                                  - (record.suspended_seconds or 0.0)) * 1000)
        for call_id, name in open_call_ids(state.journal):
            started = call_id in state.started_calls
            append_message(state, {
                "role": "tool", "tool_call_id": call_id, "name": name,
                "content": STOPPED_CALL_TEXT if started else NOT_STARTED_CALL_TEXT,
            }, status="interrupted" if started else "not_run")
        store = self._registry.thread_store
        try:
            store.commit_turn(
                thread, run_id=run_id, turn=record.turn, trigger=record.trigger,
                system_prompt=state.system_prompt, first_message=state.first_message,
                brief=record.task, journal=state.journal, result=result, outcome=outcome,
                usage=dict(record.usage), duration_ms=duration_ms, aborted=aborted,
                with_conversation=_with_conversation(record, state),
            )
        except Exception:
            logger.exception(f"[SubagentManager] [{run_id[:8]}] could not save the turn into its thread")
            return
        if state.summarized and not state.base_mismatch:
            self._persist_context(thread, run_id, state, result)

    def _persist_context(self, thread: SubagentThreadRecord, run_id: str, state: ChildState,
                         result: str) -> None:
        """Keep the summary this turn made, and what came after it, for the thread's next turn."""
        from flowly.compaction.types import is_summary_message

        messages = state.messages
        if len(messages) < 2 or not is_summary_message(messages[1]):
            logger.warning(f"[SubagentManager] [{run_id[:8]}] summarized, but no summary leads its "
                           "context; the next task fits its history again")
            return
        kept = [dict(message) for message in messages[2:]]
        kept.append({"role": "assistant", "content": result or ""})
        try:
            if self._registry.thread_store.persist_context(thread, run_id=run_id, summary=messages[1],
                                                           kept=kept):
                logger.info(f"[SubagentManager] [{run_id[:8]}] kept the turn's summary for the "
                            f"subagent's next task ({len(kept)} message(s) after it)")
        except Exception:
            logger.exception(f"[SubagentManager] [{run_id[:8]}] could not keep the turn's summary")

    def _cap_tool_result(self, run_id: str, tool_name: str, result: str) -> str:
        """What the model gets of a tool result: the main agent's per-tool limit.

        Past it the text is cut and the full output spilled — into the run's
        thread (kept as long as its conversation) or, for a run with none,
        the shared temp spill dir — with a pointer read_file can follow. A
        read_file result points back at its own offset/limit instead.
        """
        from flowly.agent.loop import _DEFAULT_MAX_CHARS, _TOOL_MAX_CHARS
        from flowly.agent.tool_result_spill import build_spill_pointer, spill_tool_result

        max_chars = _TOOL_MAX_CHARS.get(tool_name, _DEFAULT_MAX_CHARS)
        if len(result) <= max_chars:
            return result
        total = len(result)
        if tool_name == "read_file":
            return result[:max_chars] + (
                f"\n[... truncated from {total} chars — re-call read_file "
                f"with offset/limit to read a smaller section.]"
            )
        directory = None
        record = self._registry.get(run_id)
        if record is not None and record.thread_id and self._registry.get_thread(record.thread_id):
            try:
                directory = self._registry.thread_store.spill_dir(record.thread_id)
            except (OSError, ValueError):
                directory = None
        path = spill_tool_result(result, tool_name, directory=directory)
        if path is None:
            return result[:max_chars] + f"\n[... truncated from {total} chars]"
        return result[:max_chars] + build_spill_pointer(path, total, result.count("\n") + 1)

    def forget_conversation(self, parent_session_key: str) -> int:
        """A conversation was deleted: close its subagents' threads and remove their conversations.

        Their records stay (closed, ``parent_deleted``) until pruned; their
        conversations and raw outputs go now. A run still working for it
        finishes as before, but its turn is no longer saved. Returns how many
        threads were removed.
        """
        try:
            thread_ids = self._registry.forget_parent(parent_session_key)
        except Exception:
            logger.exception("Could not close the subagents of a deleted conversation")
            return 0
        for thread_id in thread_ids:
            self._registry.thread_store.delete(thread_id)
        return len(thread_ids)

    def _child_tools(self, run_id: str, label: str) -> SubagentToolRegistry:
        """The restricted tools one child run gets.

        Factory lookup over a fixed allowlist. SubagentToolRegistry still
        refuses blocked names (spawn, delegate_to, cron, message, …) on
        register so a broken allowlist can't smuggle a dangerous tool in
        either. When the parent has governance enabled, its post_tool_call
        hook fires from the subagent's registry too, so self-review's
        memory/KG writes are governed like the main agent's.
        """
        sub_hooks = None
        if self.governance_post_tool is not None:
            from flowly.agent.hooks import HookRegistry
            sub_hooks = HookRegistry()
            sub_hooks.register("post_tool_call", self.governance_post_tool)
        tools = SubagentToolRegistry(hooks=sub_hooks)

        def _build_kg_tool() -> Any:
            from flowly.agent.tools.knowledge_graph import KnowledgeGraphTool
            kg_state = self._state_dir
            if not (kg_state / "knowledge_graph.sqlite3").exists():
                alt = self.workspace.parent / "knowledge_graph.sqlite3"
                if alt.exists():
                    kg_state = self.workspace.parent
            return KnowledgeGraphTool(state_dir=kg_state)

        def _build_artifact_tool() -> Any:
            if self.artifact_tool_factory is not None:
                return self.artifact_tool_factory()
            from flowly.artifacts.store import get_store
            from flowly.agent.tools.artifact import ArtifactTool
            art_tool = ArtifactTool(store=get_store(self._state_dir))
            if self._artifact_on_change:
                art_tool.set_on_change(self._artifact_on_change)
            return art_tool

        from flowly.agent.tools.skill_manage import SkillManageTool

        # Factory map: name → builder; the keys are the child's allowlist.
        _TOOL_FACTORIES: dict[str, Any] = {
            "read_file": lambda: ReadFileTool(workspace=self.workspace),
            "write_file": lambda: WriteFileTool(workspace=self.workspace),
            "edit_file": lambda: EditFileTool(workspace=self.workspace),
            "list_dir": lambda: ListDirTool(workspace=self.workspace),
            "memory_append": lambda: MemoryAppendTool(workspace=self.workspace),
            "exec": lambda: ExecTool(
                config=self.exec_config, working_dir=str(self.workspace),
            ),
            "web_search": lambda: WebSearchTool(
                api_key=self.brave_api_key,
                proxy_url=self._web_search_proxy_url,
                server_id=self._web_search_server_id,
                auth_token=self._web_search_auth_token,
            ),
            "web_fetch": lambda: WebFetchTool(),
            "web_extract": lambda: WebExtractTool(),
            "skill_manage": lambda: SkillManageTool(workspace=self.workspace),
            "knowledge_graph": _build_kg_tool,
            "artifact": _build_artifact_tool,
            "agent_message": lambda: ChildAgentMessageTool(self, run_id),
        }

        requested = set(_TOOL_FACTORIES.keys())
        own = self._registry.get(run_id)
        if own is not None and own.origin == "system":
            # Background bookkeeping has no conversation to talk to.
            requested.discard("agent_message")
        if label == "self-review":
            # Self-review is only for memory/KG evolution. Replace the
            # default tool set so it cannot create skills indirectly via
            # skill_manage or by writing workspace skill files.
            requested = {"memory_append", "knowledge_graph"}

        for tool_name in requested:
            factory = _TOOL_FACTORIES.get(tool_name)
            if factory is None:
                continue
            try:
                tools.register(factory())
            except ValueError as blocked:
                # The blocklist guard is authoritative.
                logger.warning(
                    f"[SubagentManager] [{run_id[:8]}] refused blocked tool: {blocked}"
                )
            except Exception as e:
                # Optional tools (KG, artifact) may fail to build on
                # fresh installs. Non-fatal — just not available.
                logger.debug(
                    f"[SubagentManager] [{run_id[:8]}] tool "
                    f"'{tool_name}' unavailable: {e}"
                )
        return tools

    def _log(self, run_id: str, kind: str, *, durable: bool = False, **fields: Any) -> None:
        """Add one line to the run's checkpoint; a checkpoint never fails its run.

        Runs without one (system, waited for, silent, or not started by a
        gateway) write nothing. A checkpoint that cannot be kept (full, or
        the disk refused it) is dropped: the run goes on, but a restart
        will not continue it.
        """
        checkpoint = self._checkpoints.get(run_id)
        if checkpoint is None:
            return
        try:
            checkpoint.append(kind, durable=durable, **fields)
        except Exception as exc:
            logger.warning(
                f"[SubagentManager] [{run_id[:8]}] checkpoint stopped ({type(exc).__name__}); "
                "this run will not be continued after a restart"
            )
            self._drop_checkpoint(run_id)
            try:
                self._registry.update(run_id, resumable=False)
            except Exception:
                logger.exception("Could not record that a run cannot be continued")

    def _made_progress(self, run_id: str) -> None:
        """A step finished after a restart: the crash-loop count starts over."""
        record = self._registry.get(run_id)
        if record is not None and record.resume_count:
            self._registry.update(run_id, resume_count=0)

    def _drop_checkpoint(self, run_id: str) -> None:
        checkpoint = self._checkpoints.pop(run_id, None)
        if checkpoint is not None:
            checkpoint.delete()

    def _answer_tool(self, run_id: str, state: ChildState, call_id: str, protocol_name: str,
                     content: str, *, status: str = "ok", guard: dict[str, Any] | None = None,
                     search: bool = False) -> None:
        """Give the model one tool message, and checkpoint exactly that."""
        append_message(state, {
            "role": "tool",
            "tool_call_id": call_id,
            "name": protocol_name,
            "content": content,
        }, status=status)
        self._log(run_id, "tool", callId=call_id, protocolName=protocol_name, content=content,
                  status=status, guard=guard, search=search)

    # ------------------------------------------------------------------
    # Context management: the main agent's, per request
    # ------------------------------------------------------------------

    def _output_budget(self, model: str) -> int:
        return subagent_output_budget(self.compaction.effective_context_window_for(model),
                                      self.compaction.effective_reserve_tokens_for(model))

    @staticmethod
    def _pin(state: ChildState) -> dict[str, Any]:
        """What a fitting keeps verbatim: the turn's task, where it stands.

        On a thread's first turn that is the message after the system prompt
        (after a summary, once there is one); on a later turn it follows the
        thread's history, which may be summarized like any other context.
        """
        index = task_index(state)
        return {"pinned": _PINNED_BRIEF} if index is None else {"pin_at": index}

    async def _fit_request(
        self, run_id: str, state: ChildState, tools: list[dict[str, Any]] | None, model: str,
        iteration: int, *, overflow: bool, activity: dict[str, Any],
    ) -> RequestFit:
        """Fit the run's next request to its model, as the main agent fits its own.

        ``ProviderRequestCoordinator.fit``: nothing while the request (messages
        and tool schemas) is under the compaction threshold of ``model``'s
        window; past it, old tool results are cut (microcompact); if that is
        not enough, the history is summarized — the last resort, a model call.
        After a context-overflow rejection (``overflow``) it reduces harder.
        The system prompt and the turn's task are kept verbatim (``_pin``); a
        later turn's earlier history is compactable like the rest.

        What changed the list is checkpointed before the request goes out: a
        cut with its parameters (``microcompact``), a summary with the list it
        produced (``compacted``, fsynced), so a restart rebuilds it without a
        model call. The summary's tokens count toward the run.
        """
        requests = self._requests
        pin = self._pin(state)
        before = requests.budget(state.messages, tools, model, **pin)
        if not overflow and before.estimated_input_tokens <= before.input_limit:
            return RequestFit(state.messages, before, before)
        reason = "overflow" if overflow else "threshold"
        await self._progress(run_id, {"phase": "compacting", "iteration": iteration, "reason": reason})
        micro = self.compaction.config.microcompact
        keep_recent_full, truncate_chars = micro.keep_recent_full, micro.truncate_chars
        original = state.messages
        calls: list[dict[str, Any]] = []
        # A thread's watchers see a summary as the conversation's compaction:
        # announced when the summary call starts (a cut alone is no compaction).
        stream = self._streams.get(run_id)
        fit_options: dict[str, Any] = {}
        if stream is not None:
            async def summary_started() -> None:
                stream.compaction("started", tokens_before=before.estimated_input_tokens)

            fit_options["on_summary_start"] = summary_started
        try:
            with collect_summary_usage() as calls:
                fitted = await requests.fit(
                    original, tools, model, session_key=f"subagent:{run_id}",
                    force_reduction=overflow, **pin,
                    custom_instructions=CHILD_SUMMARY_INSTRUCTIONS, **fit_options,
                )
        except Exception as exc:  # noqa: BLE001 — fitting fails closed, never the run
            logger.warning(f"[SubagentManager] [{run_id[:8]}] request fitting failed: {exc}")
            fitted = RequestFit(original, before, before, failure=f"request fitting failed: {exc}",
                                retryable=True)
        finally:
            for usage in calls:
                await self._count_usage(run_id, usage)
        if stream is not None:
            if fitted.fits and fitted.compaction is not None:
                stream.compaction("completed", tokens_before=fitted.before.estimated_input_tokens,
                                  tokens_after=fitted.after.estimated_input_tokens,
                                  messages_removed=max(0, len(original) - len(fitted.messages)))
            else:
                stream.compaction("failed", tokens_before=before.estimated_input_tokens)
        if fitted.fits and fitted.changed:
            summarized = fitted.compaction is not None
            if not summarized and microcompact_tool_results(
                original, keep_recent_full=keep_recent_full, truncate_chars=truncate_chars,
            ) == fitted.messages:
                apply_microcompact(state, keep_recent_full, truncate_chars)
                self._log(run_id, "microcompact", iteration=iteration, reason=reason,
                          keepRecentFull=keep_recent_full, truncateChars=truncate_chars)
            else:
                # A summary cannot be computed again: the list it produced is
                # the record (and so is any cut the replay could not redo).
                apply_compacted(state, fitted.messages, summarized=summarized)
                self._log(run_id, "compacted", durable=True, iteration=iteration, reason=reason,
                          summarized=summarized, messages=state.messages,
                          tokensBefore=fitted.before.estimated_input_tokens,
                          tokensAfter=fitted.after.estimated_input_tokens)
            if summarized:
                self._transcribe(run_id, {
                    "kind": "compacted", "at": time.time(), "iteration": iteration, "reason": reason,
                    "tokensBefore": fitted.before.estimated_input_tokens,
                    "tokensAfter": fitted.after.estimated_input_tokens,
                    "summary": _clip(fitted.compaction.summary, 4_000),
                })
            logger.info(
                f"[SubagentManager] [{run_id[:8]}] request {'summarized' if summarized else 'compacted'} "
                f"({reason}): {fitted.before.estimated_input_tokens} → "
                f"{fitted.after.estimated_input_tokens} estimated tokens (limit {fitted.after.input_limit})"
            )
        await self._progress(run_id, activity)
        return fitted

    async def _call_model(
        self, run_id: str, state: ChildState, *, tools: list[dict[str, Any]] | None, model: str,
        iteration: int, max_tokens: int, timeout: float, activity: dict[str, Any],
    ) -> LLMResponse:
        """One model call of the run, made the way the main agent makes one
        (``AgentLoop._call_provider_with_context_recovery``).

        The request is fitted first (``_fit_request``); one that cannot be
        made to fit is not sent. A context-overflow rejection teaches the
        shared service the provider's input ceiling (``input_too_large``),
        the request is fitted harder and sent once more — only if that
        changed it. Every call's tokens count toward the run.
        """
        fitted = await self._fit_request(run_id, state, tools, model, iteration,
                                         overflow=False, activity=activity)
        if not fitted.fits:
            return self._unfit_response(run_id, fitted)

        async def blocking() -> LLMResponse:
            response = await self.provider.chat(messages=state.messages, tools=tools, model=model,
                                                max_tokens=max_tokens, timeout=timeout)
            await self._count_usage(run_id, response.usage)
            return response

        async def dispatch() -> LLMResponse:
            # The reply streams token by token only while somebody watches the
            # thread and the provider streams; otherwise the call is today's.
            stream = self._streams.get(run_id)
            if stream is None or not _streams_tokens(self.provider) or not stream.watched():
                return await blocking()
            response = await self._stream_model(run_id, stream, state.messages, tools=tools, model=model,
                                                max_tokens=max_tokens, timeout=timeout)
            await self._count_usage(run_id, response.usage)
            if (response.finish_reason == "error" and not response.partial_content_delivered
                    and _blocking_may_succeed(classify_response(response))):
                # As the main loop: a stream that failed before any text falls
                # back to one blocking call (a model or route without streaming;
                # the blocking call also rotates a rejected key).
                logger.warning(f"[SubagentManager] [{run_id[:8]}] streaming failed, falling back to a blocking call")
                return await blocking()
            return response

        response = await dispatch()
        if response.finish_reason != "error":
            return response
        category = classify_response(response)
        if category not in {ErrorCategory.CONTEXT_OVERFLOW, ErrorCategory.INPUT_TOO_LARGE}:
            return response
        if category is ErrorCategory.INPUT_TOO_LARGE:
            ceiling = extract_input_limit_tokens(response)
            if ceiling is None:
                # The rejected payload bounds the ceiling from above.
                ceiling = int(self._requests.budget(state.messages, tools, model, **self._pin(state))
                              .estimated_input_tokens * 0.9)
            self.compaction.note_observed_input_limit(
                model, ceiling, reason="subagent provider input_too_large rejection",
            )
        rejected = request_fingerprint(state.messages)
        recovered = await self._fit_request(run_id, state, tools, model, iteration,
                                            overflow=True, activity=activity)
        if not recovered.fits or request_fingerprint(state.messages) == rejected:
            logger.warning(
                f"[SubagentManager] [{run_id[:8]}] {category.value}: no safely smaller request "
                f"({recovered.failure or 'unchanged'})"
            )
            return response
        logger.warning(
            f"[SubagentManager] [{run_id[:8]}] {category.value} — retrying once after reducing "
            f"the context ({recovered.before.estimated_input_tokens} → "
            f"{recovered.after.estimated_input_tokens} estimated tokens)"
        )
        return await dispatch()

    async def _stream_model(
        self, run_id: str, stream: ChildStream, messages: list[dict[str, Any]], *,
        tools: list[dict[str, Any]] | None, model: str, max_tokens: int, timeout: float,
    ) -> LLMResponse:
        """One model call whose reply streams to the thread's watchers.

        The provider's ``chat_stream`` (the main agent's streaming path),
        assembled by :class:`StreamedReply` into the response ``chat`` would
        have given: the whole text, the tool calls (``extra_content``
        included), the finish reason and the final chunk's usage. Its text
        goes out as it comes (``ChildStream.stream_text``, coalesced); nothing
        of it is recorded until the reply is complete — the run's checkpoint
        and journal get it exactly as from a blocking call. ``timeout`` bounds
        the whole reply, as it bounds a blocking call. A failed reply's text
        is taken back from the watchers (``discard_reply``); its error text is
        what classifies the failure.
        """
        reply = StreamedReply()
        stream.begin_reply()
        timed_out = False
        chunks = self.provider.chat_stream(messages=messages, tools=tools, model=model, max_tokens=max_tokens)
        try:
            async with asyncio.timeout(timeout) as deadline:
                async for chunk in chunks:
                    text = reply.feed(chunk)
                    if text:
                        stream.stream_text(text)
        except TimeoutError:
            if not deadline.expired():
                raise
            timed_out = True
        finally:
            close = getattr(chunks, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — a provider that cannot close
                    logger.debug(f"[SubagentManager] [{run_id[:8]}] closing the stream failed", exc_info=True)
        stream.end_reply()  # a run stopped mid-reply sends nothing more: its terminal follows
        if timed_out:
            logger.warning(f"[SubagentManager] [{run_id[:8]}] streamed model call timed out after {timeout:.0f}s")
            response = LLMResponse(content=f"Error calling LLM: the model call timed out after {timeout:.0f}s",
                                   finish_reason="error", partial_content_delivered=bool(reply.text))
        else:
            response = reply.response()
            final = reply.final
            if response.finish_reason == "error" and final is not None:
                response = LLMResponse(content=final.content, finish_reason="error", usage=dict(response.usage),
                                       error_info=final.error_info,
                                       partial_content_delivered=response.partial_content_delivered)
        if response.finish_reason == "error":
            stream.discard_reply()
        return response

    @staticmethod
    def _unfit_response(run_id: str, fitted: RequestFit) -> LLMResponse:
        """The answer for a request that was not sent because it did not fit.

        A summary that failed may work on another attempt: that is an
        ordinary transient error (retried with the run's error budget). Any
        other failure is structural and ends as ``input_too_large``, as for
        the main agent.
        """
        if fitted.retryable:
            logger.warning(f"[SubagentManager] [{run_id[:8]}] context compaction failed: {fitted.failure}")
            return LLMResponse(content="Error: compacting the run's context failed.",
                               finish_reason="error", error_info=LLMErrorInfo(code="compaction_failed"))
        return request_too_large_response(fitted.failure or "request does not fit")

    async def _child_run(
        self,
        run_id: str,
        task: str,
        label: str,
        origin_channel: str,
        origin_chat_id: str,
        model: str,
        timeout_seconds: int | None,
        context: str | None,
        tool_trace: list[dict[str, Any]],
        restored: ChildState | None,
        turn: dict[str, Any] | None = None,
    ) -> tuple[str, str, str | None, str | None]:
        """The child's own loop. Returns (result, status, error, error_code).

        ``turn`` (a dict the caller holds) receives the run's ``state`` as
        soon as it exists, so a run that ends any way — finished, failed,
        stopped — can still save what it did into its thread.
        """
        status: str = "ok"
        error_str: str | None = None
        error_code: str | None = None
        tools = self._child_tools(run_id, label)

        if restored is None:
            own = self._registry.get(run_id)
            history: list[dict[str, Any]] = []
            system_prompt = ""
            if own is not None and own.trigger == "followup" and own.thread_id:
                # A later turn of the thread: the same system prompt, the
                # thread's history as the model gets it, then this task
                # (with the messages that waited for it).
                saved = self._registry.thread_store.turn_context(own.thread_id)
                system_prompt, history = saved.system_prompt, saved.history
                first_message = followup_message(task, own.notes)
            else:
                first_message = (
                    f"## Conversation so far\n\n{context}\n\n## Your task\n\n{task}"
                    if context else task
                )
            if not system_prompt:
                # Build messages with subagent-specific prompt
                system_prompt = self._build_subagent_prompt(task, label=label)
                if build_tool_disclosure(
                    tools.get_definitions(),
                    toolsets=tools.get_toolsets(),
                    sources=tools.get_discovery_sources(),
                    intent_text=task,
                ).enabled:
                    system_prompt += (
                        "\n\nSome safe tools are represented by a compact catalog. "
                        "They may be invoked through tool_call. tool_search is available "
                        "when a name or compact signature is unclear; its results include "
                        "complete schemas and valid invocation options."
                    )
            state = ChildState(
                messages=[
                    {"role": "system", "content": system_prompt},
                    *history,
                    {"role": "user", "content": first_message},
                ],
                max_iterations=self.max_iterations,
                system_prompt=system_prompt,
                first_message=first_message,
                base_count=len(history),
                base_digest=history_digest(history) if history else "",
            )
            thread_fields: dict[str, Any] = {}
            if own is not None and own.thread_id:
                thread_fields = {"threadId": own.thread_id, "turn": own.turn, "trigger": own.trigger,
                                 "sessionKey": own.child_session_key}
            self._log(
                run_id, "header", durable=True, runId=run_id, coreVersion=_core_version(),
                model=model, label=label, maxIterations=state.max_iterations,
                systemPrompt=system_prompt, firstMessage=first_message,
                tools=sorted(tools.tool_names), baseCount=state.base_count,
                baseDigest=state.base_digest, **thread_fields,
            )
            self._transcribe(run_id, {
                "kind": "brief", "content": _clip(task, 8_000),
                "withConversation": bool(context),
            })
        else:
            state = restored
        if turn is not None:
            # The caller saves the run's journal into its thread at the end.
            turn["state"] = state
        if restored is not None:
            if state.final is not None:
                # The result was decided before the restart; only recording
                # it was left. No model call.
                final = state.final
                return (final["result"] or "Task completed but no final response was generated.",
                        final["status"], final["error"], final["errorCode"])
        # The turn streams live to whoever watches its thread from here on.
        try:
            stream = await self._open_stream(run_id, state, resumed=restored is not None)
        except Exception:  # noqa: BLE001 — the stream never fails the run
            logger.debug(f"[SubagentManager] [{run_id[:8]}] live stream unavailable", exc_info=True)
            stream = self._streams.pop(run_id, None)
            if stream is not None:
                stream.close()
            stream = None

        max_iterations = state.max_iterations
        final_result: str | None = None
        _consecutive_errors = 0  # Global error counter (not per-iteration)
        _MAX_CONSECUTIVE_ERRORS = 3

        # P1.1 — subagent→parent activity heartbeat. Called at each
        # iteration + tool boundary so a 10-min subagent keeps the
        # parent's cron inactivity timer fresh. No-op if parent didn't
        # wire a callback (tests, headless). Failures are swallowed —
        # heartbeat must never kill the child.
        def _heartbeat(desc: str, tool: str | None = None) -> None:
            touch = self.parent_activity_touch
            if touch is None:
                return
            try:
                touch(desc, tool)
            except Exception:
                pass

        # No wall clock unless a caller asked for one: each model call
        # then gets the fixed timeout below, as the main agent's do.
        _total_timeout: float | None = (
            float(timeout_seconds) if (timeout_seconds and timeout_seconds > 0) else None
        )
        _start_time = time.time()
        logger.info(
            f"[SubagentManager] [{run_id[:8]}] total timeout: "
            f"{f'{_total_timeout:.0f}s' if _total_timeout else 'none'}, max_iter={max_iterations}"
        )

        while state.grace is None:
            if not state.awaiting_model:
                if state.iteration >= max_iterations or state.stalled:
                    break
                # Messages the main agent sent since the last step, and the
                # old tool results the step cuts (the main loop's schedule) —
                # recorded so a restart rebuilds the same list.
                mail = self._take_inbox_entries(run_id)
                strip = iteration_strip_passes(state.iteration + 1)
                # A message is its text; a task (agent_followup) says so.
                notes: list[Any] = [
                    {"text": m["text"], "kind": "task"} if m.get("kind") == "task" else m["text"]
                    for m in mail
                ]
                apply_step(state, state.iteration + 1, notes, strip)
                self._log(run_id, "step", iteration=state.iteration,
                          notes=notes, mail=[m["id"] for m in mail],
                          strip=[list(p) for p in strip])
                if mail:
                    await self._stream_sync(run_id, state)
            iteration = state.iteration
            thinking = {"phase": "thinking", "iteration": iteration}
            await self._progress(run_id, thinking)
            _heartbeat(
                f"subagent {label}: iteration {iteration}/{max_iterations}",
                None,
            )

            # Adaptive per-call timeout. When a wall-clock total is
            # set, split the remaining budget across remaining
            # iterations (min 60s, biased 1.5× to favour earlier
            # calls). When unbounded, fall back to a fixed 300s
            # ceiling — enough for slow providers, still below any
            # human-patience threshold.
            if _total_timeout is not None:
                _elapsed = time.time() - _start_time
                _remaining = max(60.0, _total_timeout - _elapsed)
                _remaining_iters = max(1, max_iterations - iteration + 1)
                _llm_call_timeout = max(60.0, min(_remaining, _remaining / _remaining_iters * 1.5))
            else:
                _llm_call_timeout = _MODEL_CALL_TIMEOUT_SECONDS

            tool_disclosure = build_tool_disclosure(
                tools.get_definitions(),
                toolsets=tools.get_toolsets(),
                sources=tools.get_discovery_sources(),
                intent_text=task,
            )
            response = await self._call_model(
                run_id, state, tools=list(tool_disclosure.definitions), model=model,
                iteration=iteration, max_tokens=self._output_budget(model),
                timeout=_llm_call_timeout, activity=thinking,
            )
            state.awaiting_model = False

            if response.has_tool_calls:
                _consecutive_errors = 0  # Successful LLM call — reset error counter
                # Add assistant message with tool calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                        # Replay Gemini thought_signature (stripped for
                        # non-Gemini targets in the provider). See loop.py.
                        **({"extra_content": tc.extra_content} if getattr(tc, "extra_content", None) else {}),
                    }
                    for tc in response.tool_calls
                ]
                assistant = {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": tool_call_dicts,
                }
                append_message(state, assistant)
                # Written before any of its tools runs: a restart knows every
                # call this reply made, and which of them had started.
                self._log(run_id, "model", durable=True, iteration=iteration, message=assistant,
                          usage=response.usage or {})
                self._transcribe(run_id, {
                    "kind": "assistant",
                    "content": response.content or "",
                    "toolCalls": [
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": _clip(json.dumps(tc.arguments, ensure_ascii=False), 1_500),
                        }
                        for tc in response.tool_calls
                    ],
                })

                # Execute tools
                for tool_call in response.tool_calls:
                    # What the model said and the results so far, live.
                    await self._stream_sync(run_id, state)
                    protocol_tool_name = tool_call.name
                    effective_tool_name = protocol_tool_name
                    tool_args = dict(tool_call.arguments)

                    if protocol_tool_name == "tool_search":
                        search_offset = tool_args.get("offset", 0)
                        try:
                            search_limit = max(
                                1,
                                min(
                                    int(tool_args.get("limit", 5)),
                                    tool_disclosure.search_max_limit,
                                ),
                            )
                        except (TypeError, ValueError):
                            search_limit = 5
                        try:
                            search_offset = max(0, int(search_offset))
                        except (TypeError, ValueError):
                            search_offset = 0
                        result = tool_disclosure.search(
                            query=str(tool_args.get("query", "")),
                            toolset=str(tool_args.get("toolset", "")),
                            limit=search_limit,
                            offset=search_offset,
                        )
                        result = annotate_search_repetition(
                            result,
                            state.search_sets,
                        )
                        self._answer_tool(run_id, state, tool_call.id, protocol_tool_name, result,
                                          search=True)
                        continue

                    if protocol_tool_name == "tool_describe":
                        result = tool_disclosure.describe(str(tool_args.get("name", "")))
                        self._answer_tool(run_id, state, tool_call.id, protocol_tool_name, result)
                        continue

                    if protocol_tool_name == "tool_call":
                        resolved = tool_disclosure.resolve_call(
                            str(tool_args.get("name", "")),
                            tool_args.get("arguments"),
                        )
                        if isinstance(resolved, str):
                            self._answer_tool(run_id, state, tool_call.id, protocol_tool_name, resolved,
                                              status="error")
                            continue
                        effective_tool_name, tool_args = resolved

                    if (
                        protocol_tool_name != "tool_call"
                        and effective_tool_name not in tool_disclosure.direct_names
                    ):
                        result = f"Error: Tool '{effective_tool_name}' is unavailable."
                        self._answer_tool(run_id, state, tool_call.id, protocol_tool_name, result,
                                          status="error")
                        continue

                    logger.debug(
                        f"[SubagentManager] [{run_id[:8]}] tool: {effective_tool_name}"
                    )
                    if effective_tool_name == "artifact":
                        state.model_used_artifact = True
                    _heartbeat(
                        f"subagent {label}: running {effective_tool_name}",
                        effective_tool_name,
                    )
                    _tool_t0 = time.monotonic()
                    _tool_status = "ok"
                    step = {"tool": effective_tool_name, "args_bytes": len(json.dumps(tool_args or {}).encode()),
                            "result_bytes": 0, "status": "running", "duration_ms": 0,
                            "started_at": time.time()}
                    tool_trace.append(step)
                    self._registry.update(run_id, tool_trace=tool_trace)
                    await self._progress(run_id, {"phase": "tool", "tool": effective_tool_name,
                                                  "iteration": iteration})
                    # From here on the call may have effects: a restart never
                    # runs it again.
                    self._log(run_id, "tool_start", durable=True, iteration=iteration,
                              callId=tool_call.id, name=effective_tool_name,
                              protocolName=protocol_tool_name,
                              toolClass=tool_class(effective_tool_name, tool_args))
                    self._current_call[run_id] = tool_call.id
                    state.started_calls.add(tool_call.id)
                    if stream is not None:
                        await stream.tool_started(tool_call.id, effective_tool_name, tool_args)
                    try:
                        if (
                            effective_tool_name == "artifact"
                            and "session_key" not in tool_args
                        ):
                            tool_args["session_key"] = f"{origin_channel}:{origin_chat_id}"
                        result = await asyncio.wait_for(
                            tools.execute(effective_tool_name, tool_args),
                            # Per-tool timeout; a question to the main agent
                            # may wait the full reply window instead.
                            timeout=(
                                _REPLY_WAIT_SECONDS + 30
                                if effective_tool_name == "agent_message" else 120
                            ),
                        )
                    except asyncio.TimeoutError:
                        result = f"Error: Tool '{effective_tool_name}' timed out after 120s"
                        _tool_status = "timeout"
                        logger.warning(
                            f"[SubagentManager] [{run_id[:8]}] tool timeout: "
                            f"{effective_tool_name}"
                        )
                    except Exception as e:
                        result = f"Error executing {effective_tool_name}: {e}"
                        _tool_status = "error"
                        logger.warning(
                            f"[SubagentManager] [{run_id[:8]}] tool error: "
                            f"{effective_tool_name}: {e}"
                        )
                    finally:
                        self._current_call.pop(run_id, None)
                    _tool_duration_ms = int((time.monotonic() - _tool_t0) * 1000)
                    # Flip status to "error" if the tool returned a
                    # human-readable error string without raising
                    # (common for validation-level failures).
                    if _tool_status == "ok" and result.startswith("Error"):
                        _tool_status = "error"
                    step.update(result_bytes=len(result.encode()), status=_tool_status,
                                duration_ms=_tool_duration_ms, ended_at=time.time())
                    self._registry.update(run_id, tool_trace=tool_trace)
                    if effective_tool_name == "artifact" and _tool_status == "ok":
                        try:
                            payload = json.loads(result)
                            artifact_id = _created_artifact_id(payload, tool_args)
                            if artifact_id:
                                self._link_artifact(run_id, artifact_id)
                        except (TypeError, ValueError):
                            pass
                    await self._progress(run_id, {"phase": "thinking", "iteration": iteration})
                    # A long result is cut to the main agent's per-tool limit;
                    # the full output is kept in the thread's spill dir and
                    # the text ends with a pointer read_file can follow.
                    result = self._cap_tool_result(run_id, effective_tool_name, result)
                    repeated = state.repetition.observe(effective_tool_name, result)
                    if repeated:
                        result = mark_repeated(effective_tool_name, result)
                        state.stalled = state.repetition.stalled
                    self._answer_tool(run_id, state, tool_call.id, protocol_tool_name, result,
                                      status=_tool_status,
                                      guard={"tool": effective_tool_name, "repeated": repeated})
                    self._made_progress(run_id)
                    self._transcribe(run_id, {
                        "kind": "tool",
                        "toolCallId": tool_call.id,
                        "name": effective_tool_name,
                        "status": _tool_status,
                        "content": _clip(result, 2_000),
                    })
                await self._stream_sync(run_id, state)
            else:
                # Error response (timeout, provider error)
                if response.finish_reason == "error":
                    _consecutive_errors += 1
                    # P1.4 — route by error category. Rate limits get
                    # long jittered waits (anti thundering-herd);
                    # context overflow and auth are not retryable
                    # (_call_model already compacted and retried once;
                    # a bad key stays bad); everything else falls through
                    # to short jitter.
                    from flowly.agent.error_classifier import backoff_for
                    _category = classify_response(response)
                    _delay = backoff_for(
                        _category,
                        _consecutive_errors,
                        retry_after_seconds=(
                            response.error_info.retry_after_seconds
                            if response.error_info is not None
                            else None
                        ),
                    )
                    _err_snippet = (response.content or "")[:100]

                    if _delay is None:
                        logger.error(
                            f"[SubagentManager] [{run_id[:8]}] LLM error "
                            f"category={_category.value} is not retryable: {_err_snippet}"
                        )
                        if _category == ErrorCategory.CONTEXT_OVERFLOW:
                            final_result = (
                                "Error: Context overflow — task too large "
                                "for the model's window. Retry with a "
                                "smaller scope."
                            )
                        elif _category == ErrorCategory.INPUT_TOO_LARGE:
                            final_result = (
                                "Error: The provider input limit was exceeded. "
                                "Retry with fewer large inputs or a smaller scope."
                            )
                        elif _category == ErrorCategory.AUTH:
                            final_result = (
                                "Error: Authentication failed. "
                                "Check the provider API key."
                            )
                        elif _category == ErrorCategory.INSUFFICIENT_CREDITS:
                            final_result = (
                                "Error: Out of credits. Your Flowly usage "
                                "credits are exhausted — top up or upgrade "
                                "your plan to continue."
                            )
                        elif _category == ErrorCategory.IMAGE_INPUT_UNSUPPORTED:
                            final_result = (
                                "Error: This model can't read images. Choose "
                                "a vision-capable model or remove the image."
                            )
                        else:
                            # Never leak wrapped provider/SDK payloads into
                            # a parent-agent result. Unknown terminal kinds
                            # get stable product copy; raw detail stays logged.
                            final_result = "Error: The model provider couldn't respond."
                        status, error_str, error_code = "error", final_result, _category.value
                        break

                    if _consecutive_errors <= _MAX_CONSECUTIVE_ERRORS and iteration < max_iterations:
                        logger.warning(
                            f"[SubagentManager] [{run_id[:8]}] LLM error "
                            f"#{_consecutive_errors} ({_category.value}) on "
                            f"iteration {iteration}, backoff {_delay:.1f}s: "
                            f"{_err_snippet}"
                        )
                        await self._progress(run_id, {"phase": "retrying", "attempt": _consecutive_errors,
                                                      "errorCode": _category.value})
                        await asyncio.sleep(_delay)
                        continue
                    else:
                        logger.error(
                            f"[SubagentManager] [{run_id[:8]}] LLM errors exhausted "
                            f"({_consecutive_errors} consecutive, last={_category.value})"
                        )
                        final_result = f"Error: Task failed after {_consecutive_errors} consecutive LLM errors."
                        status, error_str, error_code = "error", final_result, _category.value
                        break

                # Empty response check
                if not response.content or not response.content.strip():
                    logger.warning(f"[SubagentManager] [{run_id[:8]}] empty response on iteration {iteration}")
                    if stream is not None:
                        stream.discard_reply()  # not recorded: whatever streamed goes
                    if iteration < max_iterations:
                        continue  # Retry
                    final_result = "Task produced no output."
                    status, error_str, error_code = "error", final_result, "empty_response"
                    break

                # Successful response — reset error counter
                _consecutive_errors = 0
                if self._inboxes.get(run_id) and iteration < max_iterations:
                    # The main agent wrote while this answer was being
                    # produced: read it before finishing, never drop it.
                    append_message(state, {"role": "assistant", "content": response.content})
                    self._log(run_id, "text", iteration=iteration, content=response.content)
                    await self._stream_sync(run_id, state)
                    continue
                final_result = response.content
                if stream is not None:
                    stream.answer(final_result)
                break

        # Graceful summary grace-call. Two paths into this block:
        #   1. max-iter exit: final_result is None because the LLM kept
        #      calling tools until the iteration cap. We inject a
        #      final user message and ask for a summary.
        #   2. LLM-error exhaustion: final_result is set to the
        #      "Task failed after N consecutive LLM errors" string.
        #      If we have any tool_trace to summarise, try one more
        #      call — the errors may have been transient 504s and
        #      the provider is back up now. On failure we KEEP the
        #      original error string (informative to the parent)
        #      instead of replacing it with a generic fallback.
        # Safety: tools=None makes another tool-call loop
        # structurally impossible.
        # Results that only repeat earlier ones end the run's tool use the
        # same way the iteration cap does: with a summary of what it has.
        if state.grace is None:
            _hit_max_iter = final_result is None and (state.iteration >= max_iterations or state.stalled)
            _hit_llm_errors = (
                final_result is not None
                and final_result.startswith("Error: Task failed")
                and bool(tool_trace)
            )
            if _hit_max_iter or _hit_llm_errors:
                _reason = (
                    ("repeating itself" if state.stalled else "max iterations")
                    if _hit_max_iter else "LLM errors exhausted"
                )
                append_message(state, {"role": "user", "content": GRACE_PROMPT}, _display_hidden=True)
                state.grace = {"reason": _reason, "hitMaxIter": _hit_max_iter, "preserved": final_result,
                               "status": status, "errorCode": error_code}
                self._log(run_id, "phase", phase="grace", **state.grace)
        if state.grace is not None:
            _hit_max_iter = bool(state.grace.get("hitMaxIter"))
            # None for max-iter, the error string for llm-errors.
            _preserved = state.grace.get("preserved")
            final_result = _preserved
            if _preserved is not None:
                status = state.grace.get("status") or "error"
                error_str, error_code = _preserved, state.grace.get("errorCode")
            logger.info(
                f"[SubagentManager] [{run_id[:8]}] {state.grace.get('reason')} — "
                "injecting summary grace-call"
            )
            _heartbeat(f"subagent {label}: generating summary", None)
            await self._progress(run_id, {"phase": "summarizing"})
            try:
                summary_response = await self._call_model(
                    run_id, state,
                    tools=None,  # CRITICAL: prevent another tool loop
                    model=model, iteration=state.iteration,
                    max_tokens=min(_GRACE_MAX_OUTPUT_TOKENS, self._output_budget(model)),
                    timeout=60, activity={"phase": "summarizing"},
                )
                summarized = bool(summary_response.finish_reason != "error" and summary_response.content
                                  and summary_response.content.strip())
                if not summarized and stream is not None:
                    stream.discard_reply()  # an empty summary is not the result
                if summarized:
                    final_result = summary_response.content
                    status, error_str, error_code = "ok", None, None
                    if stream is not None:
                        stream.answer(final_result)
                elif _hit_max_iter:
                    status, error_code = "error", "summary_failed"
                    final_result = (
                        f"Reached the iteration limit ({max_iterations}) "
                        "but the summary response was empty."
                    )
                # _hit_llm_errors + empty summary → keep _preserved error
            except Exception as e:
                logger.warning(
                    f"[SubagentManager] [{run_id[:8]}] summary grace-call failed: {e}"
                )
                if _hit_max_iter:
                    status, error_code = "error", "summary_failed"
                    final_result = (
                        f"Reached the iteration limit ({max_iterations}) "
                        "and the summary attempt failed."
                    )
                # _hit_llm_errors + grace-call failed → keep _preserved error
                else:
                    final_result = _preserved

        _tool_names = [t.get("tool", "?") for t in tool_trace[:20]]
        logger.info(
            f"[SubagentManager] [{run_id[:8]}] finished: {state.iteration} iterations, "
            f"{len(tool_trace)} tool calls: {', '.join(_tool_names)}"
        )
        return (
            final_result or "Task completed but no final response was generated.",
            status, error_str, error_code,
        )

    # ------------------------------------------------------------------
    # Messages between the main agent and a running subagent
    # ------------------------------------------------------------------

    async def send_to_child(self, run_id: str, text: str, *, kind: str = "message",
                            batch_id: str = "", origin: str = "agent") -> dict[str, Any]:
        """Queue a message — or, ``kind="task"``, a task (agent_followup) — for a running subagent.

        The child reads it at its next step — after the tool call it is in,
        never by interrupting one. A finished run cannot receive messages. A
        task keeps the batch and origin of the turn that gave it, for the
        follow-up it starts if the run ends before reading it.
        """
        record = self._registry.get(run_id)
        if record is None:
            return {"run_id": run_id, "status": "not_found"}
        if record.ended_at is not None or run_id not in self._running_tasks:
            return {"run_id": run_id, "status": "finished",
                    "error": "This subagent has already finished; it can't receive messages."}
        mail_id = uuid.uuid4().hex[:16]
        entry: dict[str, str] = {"id": mail_id, "text": text}
        extra: dict[str, str] = {}
        if kind == "task":
            entry.update(kind="task", batch=batch_id, origin=origin)
            extra = {"mailKind": "task", "batch": batch_id, "origin": origin}
        # Durable before the main agent is told it was delivered: a restart
        # gives the child every message it had not read yet.
        self._log(run_id, "mail", durable=True, id=mail_id, text=text, **extra)
        self._inboxes.setdefault(run_id, []).append(entry)
        self._inbox_events.setdefault(run_id, asyncio.Event()).set()
        self._transcribe(run_id, {"kind": "message_in", "from": "orchestrator", "content": text,
                                  **({"task": True} if kind == "task" else {})})
        await self._progress(run_id, {"phase": "message_received"})
        return {"run_id": run_id, "status": "delivered"}

    def _take_inbox_entries(self, run_id: str) -> list[dict[str, str]]:
        messages = self._inboxes.pop(run_id, [])
        event = self._inbox_events.get(run_id)
        if event is not None:
            event.clear()
        return messages

    def _take_inbox(self, run_id: str) -> list[str]:
        """Messages read inside a tool call (a reply agent_message waited for)."""
        entries = self._take_inbox_entries(run_id)
        call_id = self._current_call.get(run_id)
        if entries and call_id:
            # Read only once that call's result is recorded; a restart
            # before then gives them to the child again.
            self._log(run_id, "inbox", callId=call_id, ids=[e["id"] for e in entries])
        # A task that arrives as the reply is still a task.
        return [f"{ADDED_TASK_LABEL}\n{e['text']}" if e.get("kind") == "task" else e["text"] for e in entries]

    async def _wait_for_inbox(self, run_id: str, timeout: float) -> list[str]:
        if self._inboxes.get(run_id):
            return self._take_inbox(run_id)
        event = self._inbox_events.setdefault(run_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            return []
        return self._take_inbox(run_id)

    async def message_parent(self, run_id: str, text: str) -> dict[str, Any]:
        """Deliver a running subagent's message to the conversation that started it."""
        record = self._registry.get(run_id)
        if record is None:
            return {"status": "not_found"}
        title = record.display_name or record.label
        self._transcribe(run_id, {"kind": "message_out", "to": "orchestrator", "content": text})
        await self._progress(run_id, {"phase": "message_sent"})
        message_id = uuid.uuid4().hex[:16]
        self._parent_mail.setdefault(record.parent_session_key, []).append(
            {"id": message_id, "run_id": run_id, "title": title, "text": text},
        )
        self._wake(record.parent_session_key)
        prompt = (
            f"[Message from subagent '{title}' (run_id={run_id}, message_id={message_id}), still running]\n\n"
            f"{text}\n\n"
            f"If it needs an answer, reply with agent_message(to=\"{run_id}\", ...). "
            "Don't show run ids to the owner."
        )
        await self._deliver_to_parent(record, prompt, summary=f"{title} — message")
        return {"status": "delivered"}

    async def _deliver_to_parent(self, record: SubagentRunRecord, prompt: str, *, summary: str) -> None:
        """Hand a notice to the conversation that started the run.

        The notice is processed as that conversation's own turn — its key,
        its history and its turn lock — so it never runs alongside the
        owner's turn or lands in a session named after a connection id. The
        reply travels to the run's recorded delivery address.
        """
        parent_session_key = record.parent_session_key
        run_id = record.run_id

        async def _send(content: str) -> None:
            submit = self.report_submitter
            if submit is not None:
                # The surface the owner is watching runs the turn like a
                # typed one: streamed, stoppable, resumable. Nobody watching
                # (or no surface at all): the bus, as before.
                try:
                    if await submit(parent_session_key, record.parent_channel,
                                    record.parent_chat_id, content):
                        logger.debug(f"[SubagentManager] [{run_id[:8]}] reported to {parent_session_key} as a live turn")
                        return
                except Exception:
                    logger.exception("Could not start a live report turn; delivering over the bus")
            await self.bus.publish_inbound(ConversationInboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id=f"{record.parent_channel}:{record.parent_chat_id}",
                content=content,
                conversation_key=parent_session_key,
            ))
            logger.debug(f"[SubagentManager] [{run_id[:8]}] delivered to {parent_session_key}")

        if self.is_busy(parent_session_key):
            # The parent is mid-turn: queue it for when that turn ends.
            queue = get_or_create_queue(parent_session_key, _send)
            await queue.enqueue(AnnounceItem(prompt=prompt, summary=summary))
        else:
            await _send(prompt)

    def _wake(self, parent_session_key: str) -> None:
        event = self._parent_wake.get(parent_session_key)
        if event is not None:
            event.set()

    def notice_already_handled(self, content: str) -> bool:
        """True when a queued notice's content was already taken by agent_wait."""
        header = _NOTICE_HEADER_RE.search(content or "")
        if header is None:
            return False
        run_id, message_id = header.group("run_id"), header.group("message_id")
        if message_id:
            return message_id in self._consumed_mail
        # A notice for several runs is handled once all of them are.
        run_ids = _NOTICE_RUN_ID_RE.findall(content or "") or [run_id]
        records = [self._registry.get(rid) for rid in run_ids]
        return all(r is not None and r.delivery_state in ("delivered", "taken") for r in records)

    async def wait_for_children(
        self,
        parent_session_key: str,
        run_ids: list[str] | None = None,
        *,
        timeout: float,
        should_yield: Callable[[], bool] | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        """Wait until a child of this conversation finishes or writes.

        Returns as soon as there is something to act on — finished results
        not yet handled, messages from children — or the owner wrote into
        the turn, or the timeout passed. What it returns is marked taken, so
        the same result or message is not processed again as a notice; it
        becomes delivered when the turn is saved (mark_delivered), and goes
        back to be announced again if the turn ends unsaved (release_taken).
        """
        def targets() -> list[SubagentRunRecord]:
            records = [
                r for r in self._registry.all()
                if r.parent_session_key == parent_session_key and r.origin != "system"
            ]
            if run_ids:
                wanted = set(run_ids)
                return [r for r in records if r.run_id in wanted]
            return [r for r in records if r.ended_at is None or r.delivery_state in ("pending", "queued")]

        deadline = time.monotonic() + max(0.0, timeout)
        event = self._parent_wake.setdefault(parent_session_key, asyncio.Event())
        interrupted = timed_out = False
        while True:
            event.clear()
            current = targets()
            finished = [
                r for r in current
                if r.ended_at is not None and r.delivery_state in ("pending", "queued")
            ]
            mail = self._parent_mail.get(parent_session_key) or []
            if run_ids:
                wanted = set(run_ids)
                mail = [m for m in mail if m["run_id"] in wanted]
            running = [r for r in current if r.ended_at is None]
            if finished or mail or not running:
                break
            if should_yield is not None and should_yield():
                interrupted = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                # Short slices so an owner's message into the turn is noticed.
                await asyncio.wait_for(event.wait(), min(1.0, remaining))
            except asyncio.TimeoutError:
                pass

        results = []
        for record in finished:
            try:
                text = self._registry.read_result(record.run_id, 0, _ANNOUNCE_RESULT_CHARS)["content"]
            except (FileNotFoundError, ValueError, OSError):
                text = record.result_preview or ""
            item = {
                "run_id": record.run_id,
                "title": record.display_name or record.label,
                "status": record.outcome or "unknown",
                "result": text,
            }
            if record.result_chars > _ANNOUNCE_RESULT_CHARS:
                item["more"] = (
                    f"{record.result_chars - _ANNOUNCE_RESULT_CHARS} more characters: "
                    f'agent_read(run_id="{record.run_id}", offset={_ANNOUNCE_RESULT_CHARS})'
                )
            if record.error:
                item["error"] = record.error
            results.append(item)
            self._registry.update(record.run_id, delivery_state="taken")
            self._taken[record.run_id] = (parent_session_key, turn_id)
        taken_ids = {m["id"] for m in mail}
        if taken_ids:
            self._parent_mail[parent_session_key] = [
                m for m in self._parent_mail.get(parent_session_key, []) if m["id"] not in taken_ids
            ]
            for message_id in taken_ids:
                self._consumed_mail[message_id] = None
            while len(self._consumed_mail) > 1_000:
                self._consumed_mail.pop(next(iter(self._consumed_mail)))
        still_running = [
            r.display_name or r.label for r in targets() if r.ended_at is None
        ]
        return {
            "finished": results,
            "messages": [
                {"run_id": m["run_id"], "title": m["title"], "text": m["text"]} for m in mail
            ],
            "still_running": still_running,
            "timed_out": timed_out,
            "interrupted": interrupted,
        }

    def _transcribe(self, run_id: str, item: dict[str, Any]) -> None:
        """Record one step for the owner to read; never fails the run.

        System runs (self-review, Board) carry the owner's conversation and
        memory and are never opened from a chat: they keep no transcript.
        """
        record = self._registry.get(run_id)
        if record is None or record.origin == "system":
            return
        try:
            self._registry.append_transcript(run_id, item)
        except Exception:
            logger.warning(f"[SubagentManager] [{run_id[:8]}] could not record a transcript step")

    async def _deliver_event(self, event: str, data: dict) -> None:
        if self._on_event:
            await self._on_event(event, data)

    def _publish_event(self, event: str, run_id: str) -> None:
        try:
            record = self._registry.get(run_id)
            if record:
                thread = self._registry.get_thread(record.thread_id) if record.thread_id else None
                data = run_view(record, detail=event == "subagent.completed", thread=thread)
                data.update(outcome=record.outcome, running=len(self._registry.pending()))
                self._events.publish(event, data)
        except Exception:
            logger.exception("Task observation unavailable")

    async def _emit_event(self, event_name: str, run_id: str, label: str,
                          outcome: str, error: str | None = None) -> None:
        if event_name == "subagent.completed":
            self._record_metric(run_id)
        self._publish_event(event_name, run_id)

    def _record_metric(self, run_id: str) -> None:
        """Keep the ended run's numbers (subagent_metrics) past its pruning."""
        record = self._registry.get(run_id)
        path = getattr(self._registry, "_path", None)
        if record is None or record.ended_at is None or path is None:
            return
        append_metric(metrics_path(Path(path)), record)

    async def _count_usage(self, run_id: str, usage: dict[str, Any] | None) -> None:
        """Add one model call's tokens to the run, for the owner to see live."""
        if not isinstance(usage, dict) or not usage:
            return
        record = self._registry.get(run_id)
        if record is None:
            return
        totals = add_usage(record.usage, usage)
        if totals == record.usage:
            return
        self._registry.update(run_id, usage=totals)
        self._publish_event("subagent.progress", run_id)

    async def _progress(self, run_id: str, activity: dict) -> None:
        self._registry.update(run_id, activity=activity)
        self._publish_event("subagent.progress", run_id)

    def _link_artifact(self, run_id: str, artifact_id: str) -> None:
        record = self._registry.get(run_id)
        if record and artifact_id not in record.artifact_ids:
            self._registry.update(run_id, artifact_ids=[*record.artifact_ids, artifact_id])

    def _notice_section(self, record: SubagentRunRecord, result: str, status: str, limit: int) -> str:
        """One run's part of a completion notice: header, brief and result."""
        status_text = {
            "ok": "completed successfully",
            "error": "failed",
            "timeout": "timed out",
        }.get(status, status)
        duration = ""
        # A later task of the same subagent says which one it is.
        which = f"task {record.turn}" if record.thread_id and record.turn > 1 else ""
        if record.started_at and record.ended_at:
            # Time it worked: the downtime of a restart it continued across
            # is left out.
            runtime = int(max(0.0, record.ended_at - record.started_at - (record.suspended_seconds or 0.0)))
            continued = ", continued after a restart" if record.resumed_at else ""
            duration = f" (runtime: {runtime}s{continued}{', ' + which if which else ''})"
        elif which:
            duration = f" ({which})"
        # The subagent itself, which agent_followup and agent_close take.
        agent = f" agent_id={record.thread_id}" if record.thread_id else ""

        # The parent gets enough of the result to act on, and the run id to
        # read the rest — never a preview it cannot get past.
        result_text = result or ""
        shown = result_text[:limit]
        more = ""
        if len(result_text) > limit:
            more = (
                f"\n\n[{len(result_text) - limit} more characters — "
                f"read them with agent_read(run_id=\"{record.run_id}\", offset={limit})]"
            )

        # One-line "Tools used: web_search×2, exec×1" summary, so the parent
        # can judge what the child actually did.
        tool_summary = ""
        if record.tool_trace:
            from collections import Counter
            counts = Counter(t.get("tool", "?") for t in record.tool_trace)
            parts = [f"{name}×{n}" if n > 1 else name for name, n in counts.most_common()]
            tool_summary = f"\nTools used ({len(record.tool_trace)} calls): {', '.join(parts)}"

        title = record.display_name or record.label
        brief = record.task if len(record.task) <= 600 else record.task[:600] + " […]"
        late = ""
        if record.ended_at is not None and record.ended_at < self._started_at:
            late = (
                f"\nIt finished {_age_text(time.time() - record.ended_at)} ago, before Flowly "
                "restarted; this result reaches you only now."
            )
        return (
            f"[Subagent '{title}' {status_text}{duration}] run_id={record.run_id}{agent}{tool_summary}{late}\n\n"
            f"Brief: {brief}\n\n"
            f"Result:\n{shown}{more}"
        )

    def _stored_result(self, record: SubagentRunRecord) -> str:
        """A finished run's result as its completion notice shows it."""
        if record.result_available:
            try:
                return self._registry.read_result(record.run_id, 0, 200_000)["content"]
            except (FileNotFoundError, ValueError, OSError):
                pass
        if record.outcome == "timeout":
            return "(task timed out)"
        if record.outcome in ("cancelled", "interrupted"):
            # A run a restart could not continue says why.
            if record.error and record.error_code not in (None, "cancelled"):
                return record.error
            return "(task was interrupted)"
        return record.error or record.result_preview or ""

    async def _announce(self, run_id: str, result: str, status: str, *, flush: bool = False) -> None:
        """Hand a finished run's result to the conversation that started it.

        Runs started in the same turn report back together: while one of
        them is still going, a finished run's result is held, and the last
        one to finish delivers every held result in a single notice — one
        turn that can answer the owner's request as a whole, instead of a
        reply per part. A run alone in its turn reports at once.

        A held result never waits on a slow sibling for long: _BATCH_HOLD_SECONDS
        after the first one was held, every result held so far is delivered
        (``flush``), saying which siblings are still working; theirs follow in
        a notice of their own.
        """
        record = self._registry.get(run_id)
        if not record:
            return
        members = [record]
        still_running: list[SubagentRunRecord] = []
        if record.batch_id:
            batch = [
                r for r in self._registry.all()
                if r.batch_id == record.batch_id
                and r.parent_session_key == record.parent_session_key
                and r.origin != "system"
            ]
            still_running = [r for r in batch if r.ended_at is None and r.run_id != run_id]
            if still_running and not flush:
                logger.info(
                    f"[SubagentManager] [{run_id[:8]}] finished; held until the rest of its batch ends"
                )
                self._arm_batch_flush(record)
                return
            # Claim before any await: the last two runs of a batch can finish
            # together, and only one of them may deliver.
            members = [
                r for r in batch
                if (r.run_id == run_id and not flush)
                or (r.ended_at is not None and not r.announced and r.delivery_state in ("pending", "queued"))
            ]
            members.sort(key=lambda r: r.created_at)
        if record.batch_id and record.announced and len(members) == 1:
            return
        # Siblings the owner (or the agent) stopped: named once, so the
        # agent does not wait for them or wonder where their results went.
        stopped = [] if not record.batch_id else [
            r for r in self._registry.all()
            if r.batch_id == record.batch_id and r.parent_session_key == record.parent_session_key
            and r.outcome == "cancelled" and not r.announced and r.run_id != run_id
        ]
        for member in [*members, *stopped]:
            self._registry.update(member.run_id, announced=True)

        # Results the main agent already took with agent_wait are not
        # handed over again, and a run stopped on request has nothing to
        # report: whoever stopped it already knows.
        to_tell = [
            m for m in members
            if m.delivery_state not in ("delivered", "taken") and m.outcome != "cancelled"
        ]
        if not to_tell:
            return
        sections = []
        limit = _ANNOUNCE_RESULT_CHARS if len(to_tell) == 1 else max(
            2_000, _ANNOUNCE_BATCH_CHARS // len(to_tell),
        )
        for member in to_tell:
            if member.run_id == run_id and not flush:
                sections.append(self._notice_section(member, result, status, limit))
            else:
                sections.append(self._notice_section(
                    member, self._stored_result(member), member.outcome or "unknown", limit,
                ))
        waiting = ""
        if stopped:
            names = ", ".join(f"'{r.display_name or r.label}'" for r in stopped)
            waiting += f"\n\nStopped before finishing, on request: {names}. There is no result from them."
        if still_running:
            names = ", ".join(f"'{r.display_name or r.label}'" for r in still_running)
            waiting += (
                f"\n\nStill working, started in the same turn: {names}. Their results "
                "reach you in a message of their own when they finish; don't wait for "
                "them or redo their work."
            )
        elif record.batch_id:
            # The batch is complete, but part of it reached the agent earlier
            # (held too long for a slow sibling): this is the moment to answer
            # the owner's request as a whole, not to report the last part alone.
            told_ids = {m.run_id for m in to_tell}
            earlier = [
                r for r in self._registry.all()
                if r.batch_id == record.batch_id and r.parent_session_key == record.parent_session_key
                and r.origin != "system" and r.run_id not in told_ids and r.outcome != "cancelled"
                and r.ended_at is not None
            ]
            if earlier:
                names = ", ".join(f"'{r.display_name or r.label}'" for r in earlier)
                waiting += (
                    f"\n\nThis completes the work started together with {names}, whose "
                    "results reached you earlier in this conversation. Give the owner one "
                    "answer that brings all of them together."
                )
        # A fact about what is left of them: an open subagent keeps its
        # conversation and can be given another task, or closed.
        keeps = [m for m in to_tell if m.thread_id and (
            (t := self._registry.get_thread(m.thread_id)) is not None and t.status == "open")]
        kept = ""
        if keeps and len(to_tell) == 1:
            kept = (" The subagent keeps its conversation: agent_followup gives it another task, "
                    "agent_close ends it.")
        elif keeps:
            kept = (" Each subagent keeps its conversation: agent_followup gives one another task, "
                    "agent_close ends it.")
        if len(to_tell) == 1:
            announce_content = (
                f"{sections[0]}\n\n"
                "This is the subagent's own report: check what matters before relying "
                "on it, and don't redo its work. Tell the owner what it means for their "
                "request in your own words; don't show run ids or agent ids."
                + kept
                + waiting
            )
        else:
            together = "were started together and have all finished" if not still_running \
                else "were started together and have finished"
            announce_content = (
                "\n\n---\n\n".join(sections)
                + f"\n\nThese {len(to_tell)} subagents {together}. These are their own "
                "reports: check what matters before relying on them, and don't redo their "
                "work. Answer the owner's request as a whole, in your own words; don't "
                "show run ids or agent ids."
                + kept
                + waiting
            )
        titles = ", ".join(m.display_name or m.label for m in to_tell)
        await self._deliver_to_parent(record, announce_content, summary=f"{titles} — finished")
        # Only a result still waiting becomes "queued": one that needs no
        # delivery (cancelled, interrupted) or that the main agent already
        # took with agent_wait keeps its state, so the notice is not handled
        # twice and a goal never waits for it.
        for member in to_tell:
            current = self._registry.get(member.run_id)
            if current and current.delivery_state == "pending":
                self._registry.update(member.run_id, delivery_state="queued")

    def _arm_batch_flush(self, record: SubagentRunRecord) -> None:
        """Deliver this batch's held results if its other runs are still going later."""
        key = f"{record.parent_session_key}\0{record.batch_id}"
        current = self._batch_flushes.get(key)
        if current is not None and not current.done():
            return

        # _BATCH_HOLD_SECONDS after the first result was held. A result held
        # before a restart gives the siblings that continued a fresh moment.
        # Only a result that will be delivered starts the clock: a run stopped
        # on request has none, and starting it there delivered the next
        # result early and alone, apart from siblings about to finish.
        held_since = min(
            (r.ended_at for r in self._registry.all()
             if r.batch_id == record.batch_id and r.parent_session_key == record.parent_session_key
             and r.ended_at is not None and not r.announced and r.outcome != "cancelled"
             and r.delivery_state in ("pending", "queued")),
            default=None,
        )
        if held_since is None:
            return
        deadline = held_since + _BATCH_HOLD_SECONDS
        if held_since < self._started_at:
            deadline = max(deadline, time.time() + _RESUMED_BATCH_HOLD_SECONDS)

        async def flush_later() -> None:
            await asyncio.sleep(max(0.0, deadline - time.time()))
            held = sorted(
                (
                    r for r in self._registry.all()
                    if r.batch_id == record.batch_id
                    and r.parent_session_key == record.parent_session_key
                    and r.origin != "system" and r.ended_at is not None and not r.announced
                    and r.delivery_state in ("pending", "queued") and r.outcome != "cancelled"
                ),
                key=lambda r: r.created_at,
            )
            if held:
                logger.info(
                    f"[SubagentManager] batch {record.batch_id[:8]}: delivering {len(held)} held "
                    "result(s) while the rest still run"
                )
                await self._announce(held[0].run_id, "", held[0].outcome or "unknown", flush=True)

        def settled(task: asyncio.Task[None]) -> None:
            if self._batch_flushes.get(key) is task:
                del self._batch_flushes[key]
            if not task.cancelled() and task.exception() is not None:
                logger.error(f"[SubagentManager] held batch delivery failed: {task.exception()}")

        task = asyncio.create_task(flush_later())
        self._batch_flushes[key] = task
        task.add_done_callback(settled)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_subagent_prompt(self, task: str, label: str = "") -> str:
        """Build a focused system prompt for the subagent."""
        base = f"""# Subagent

The main agent started you for one task. Complete ONLY that task. Stay focused, be
concise, do not initiate side tasks. Use tools to act — never describe what you
would do without doing it.

Your final reply is your report to the main agent: lead with the answer, give the
evidence it can check (URLs, file paths, ids), and say plainly what you could not
do. Use `agent_message` only when you need something from the main agent to go on
or it must know something before you finish; messages from it appear as
"[Message from the main agent]".
Workspace: {self.workspace}
"""

        # Self-review subagent gets strict memory/KG rules
        if label == "self-review":
            base += """
## Self-Review Rules (CRITICAL)

You are reviewing a conversation to extract valuable information.

### Knowledge Graph (knowledge_graph tool)
- Use for STRUCTURED FACTS: people, companies, projects, emails, relationships
- ALWAYS use FULL NAMES — the full form the user gave, not a short form
- ALWAYS set subject_type (person/company/project/event)
- ONE entity per field — never comma-separated
- Query KG BEFORE adding to check if fact already exists
- SKIP facts already in the "Existing Knowledge Graph" section

### Memory (memory_append tool)
- Use ONLY for general preferences, corrections, environment details
- NEVER save: task outputs, analysis results, news, articles, file listings
- NEVER save: structured facts (names, emails, companies → use KG instead)
- NEVER save: temporary info, session-specific data
- Keep entries to 1 line max
- Duplicates are auto-rejected by the engine — but still try to avoid them

### Decision Rule
When in doubt, DON'T save. Less is more. Only save genuinely valuable, new information.
If nothing new to save, reply 'Nothing to save.'

### Boundary
Do not create, patch, install, or manage skills. Do not edit project files or run commands.
"""

        # Children working for the owner get their context (memory + KG),
        # but only after a prompt-injection scan. Matches Faz 5.2's hardening on
        # the main agent: memory and KG are treated like bootstrap
        # context files, so a poisoned entry (e.g. a stray
        # "ignore previous instructions" snippet written by an earlier
        # turn) surfaces as a [BLOCKED: …] placeholder instead of being
        # silently relayed into the child's system prompt.
        # Self-review reads the conversation it reviews instead.
        if label != "self-review":
            from flowly.cron.guard import scan_context_file
            try:
                memory_path = self.workspace / "memory" / "MEMORY.md"
                if memory_path.exists():
                    memory_text = memory_path.read_text(encoding="utf-8")[:3000]
                    if memory_text.strip():
                        blocked = scan_context_file(memory_text, "MEMORY.md")
                        base += f"\n## User Context\n\n{blocked or memory_text}\n"
            except Exception:
                pass
            try:
                from flowly.memory.knowledge_graph import KnowledgeGraph
                kg_path = self._state_dir / "knowledge_graph.sqlite3"
                if not kg_path.exists():
                    alt = self.workspace.parent / "knowledge_graph.sqlite3"
                    if alt.exists():
                        kg_path = alt
                if kg_path.exists():
                    kg = KnowledgeGraph(str(kg_path))
                    kg_summary = kg.summary(max_entities=15)
                    if kg_summary:
                        blocked = scan_context_file(kg_summary, "knowledge_graph")
                        base += f"\n## Known Facts\n\n{blocked or kg_summary}\n"
            except Exception:
                pass

        return base

    def cancel_all(self) -> int:
        """Cascade-cancel EVERY running subagent, regardless of parent.

        Called from the gateway shutdown path. Without this, the event
        loop closes with tasks mid-``await provider.chat(...)`` — the
        registry entries stay with ``ended_at=None`` and surface on
        the next boot as "Process restarted before task completed"
        errors to the user, even though the shutdown was clean.

        Each task's ``asyncio.CancelledError`` handler in
        ``_run_subagent`` records ``outcome="interrupted"`` + announces
        to the parent, so this is a graceful teardown, not a kill.
        Returns the number of tasks actually cancelled.
        """
        count = 0
        for run_id, task in list(self._running_tasks.items()):
            if not task.done():
                task.cancel()
                count += 1
        for flush in list(self._batch_flushes.values()):
            flush.cancel()
        if count:
            logger.info(f"[SubagentManager] shutdown: cancelled {count} running subagent(s)")
        return count

    def stop_all(self, reason: str = "shutdown") -> int:
        """Stop every running subagent for a shutdown; returns how many.

        A run that will be continued after the restart (``resumable``) is
        suspended: it stays running in the registry with ``suspended_at``
        and ``activity.phase == "suspended"``, its handler writes
        ``suspended`` to its checkpoint and announces nothing, and the next
        start continues it. Any other run is interrupted as before. New
        spawns are refused from here on. Idempotent.
        """
        self._shutting_down = True
        self._suspending = True
        self._suspend_reason = reason
        now = time.time()
        count = 0
        for run_id, task in list(self._running_tasks.items()):
            if task.done():
                continue
            record = self._registry.get(run_id)
            if record is not None and record.resumable and record.ended_at is None:
                try:
                    self._registry.update(run_id, activity={"phase": "suspended", "reason": reason},
                                          suspended_at=now)
                    # Clients still connected hear it now, rather than only
                    # noticing when the connection drops.
                    self._publish_event("subagent.progress", run_id)
                except Exception:
                    logger.exception("Could not record a suspended subagent")
            task.cancel()
            count += 1
        # Held batch results are re-armed from the registry at the next start.
        for flush in list(self._batch_flushes.values()):
            flush.cancel()
        if count:
            logger.info(f"[SubagentManager] shutdown: stopped {count} running subagent(s)")
        return count

    async def shutdown(self, wait_seconds: float = 1.5, reason: str = "shutdown") -> int:
        """Stop every running subagent first, and give each a moment to record it.

        The gateway calls this before anything else on its way down: the
        owner's desktop kills a foreground gateway 3 s after asking it to
        stop (a named agent's runtime after 5 s), so subagents cannot wait
        for channels, cron and the gateway server to close first. See
        stop_all for what happens to each run. Idempotent.
        """
        tasks = [task for task in self._running_tasks.values() if not task.done()]
        count = self.stop_all(reason)
        if tasks and wait_seconds > 0:
            await asyncio.wait(tasks, timeout=wait_seconds)
            # The "suspended" views go out while the gateway can still send.
            events = getattr(self, "_events", None)
            if events is not None:
                try:
                    await asyncio.wait_for(events.flush(), timeout=0.5)
                except Exception:
                    logger.debug("[SubagentManager] suspend events not all sent before shutdown")
        return count

    def cancel_children(self, parent_session_key: str) -> int:
        """Cascade-cancel every running subagent spawned by ``parent_session_key``.

        Called from ``AIAgent.interrupt(session_key=…)`` when the user
        aborts the parent turn. Kicks off cancellation synchronously —
        each child's ``_run_subagent`` task catches ``asyncio.CancelledError``
        on its next ``await``, persists the ``interrupted`` outcome, sends
        an announce, and exits. Returns the number of tasks actually
        signalled so the caller can log how wide the blast radius was.
        """
        run_ids = list(self._children_by_parent.get(parent_session_key, ()))
        if not run_ids:
            return 0
        count = 0
        for run_id in run_ids:
            task = self._running_tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
                count += 1
        if count:
            logger.info(
                f"[SubagentManager] cascade-cancelled {count} child(ren) of {parent_session_key}"
            )
        return count

    async def cancel(self, run_id_prefix: str) -> str:
        """Cancel a running subagent by run_id (prefix match).

        A run still marked running that no live process works on (a restart
        left it and this process does not continue runs) is ended here.
        """
        matches = [rid for rid in self._running_tasks if run_id_prefix and rid.startswith(run_id_prefix)]
        if not matches and run_id_prefix:
            orphans = [r for r in self._registry.all()
                       if r.ended_at is None and r.run_id.startswith(run_id_prefix)]
            if len(orphans) == 1 and self._adopt(orphans[0], {}) is not None:
                run_id = orphans[0].run_id
                self._registry.finish(run_id, "cancelled", error="Task interrupted", error_code="cancelled")
                self._record_metric(run_id)
                self._publish_event("subagent.completed", run_id)
                return json.dumps({"status": "cancelled", "run_id": run_id})
        if len(matches) != 1:
            return json.dumps({"status": "error", "error": "Task ID is missing, unknown or ambiguous"})
        run_id = matches[0]
        task = self._running_tasks[run_id]
        self._cancel_outcomes[run_id] = "cancelled"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        record = self._registry.get(run_id)
        return json.dumps({"status": record.outcome if record else "cancelled", "run_id": run_id})

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
