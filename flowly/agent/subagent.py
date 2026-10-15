"""Subagent manager for background task execution."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

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
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord, add_usage
from flowly.agent import subagent_owner
from flowly.agent.subagent_checkpoint import (
    ChildState,
    GRACE_PROMPT,
    ResumeRefusal,
    SubagentCheckpoint,
    apply_step,
    interrupted_result,
    read_checkpoint,
    restore_child_state,
    tool_class,
)
from flowly.agent.delegation import MAX_CHILDREN_PER_CONVERSATION, assign_mark
from flowly.agent.tools.agent_tools import ChildAgentMessageTool
from flowly.agent.subagent_observation import TaskEvents, run_view
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
    "spawn", "builtin_agent", "delegate_to", "agent_read", "agent_wait",
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


def _strip_subagent_tool_results(
    messages: list[dict[str, Any]],
    keep_last: int = 3,
    max_old_chars: int = 300,
) -> list[dict[str, Any]]:
    """Compact old tool results to reduce token usage.

    Keeps the last `keep_last` tool results at full length.
    Truncates older ones to `max_old_chars` with a placeholder.
    Keeps the last ``keep_last`` tool results at full length; older
    ones are compacted to ``max_old_chars`` with a placeholder.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_last:
        return messages

    to_truncate = set(tool_indices[:-keep_last])
    result = list(messages)
    for i in to_truncate:
        content = result[i].get("content", "")
        if len(content) > max_old_chars:
            result[i] = {
                **result[i],
                "content": content[:max_old_chars] + "\n[compacted: old output removed to save context]",
            }
    return result


# Headroom left for the subagent's own reply. Smaller than the main loop's
# reserve: a subagent answers with a result, not a conversation.
_SUBAGENT_RESERVE_TOKENS = 8_000
# …but never more than this share of a small model's window, or the reserve
# alone swallows the context the subagent is supposed to think with.
_SUBAGENT_MAX_RESERVE_SHARE = 0.35
# Cap on one reply. Also clamped to the window: asked for 16,384 output
# tokens, a 16,385-token model has nothing left to read the request with.
_SUBAGENT_MAX_OUTPUT_TOKENS = 16_384
_SUBAGENT_MAX_OUTPUT_SHARE = 0.25
# Everything a request carries besides the messages: tool schemas and the
# system preamble. Counted so the three parts are budgeted against ONE window
# instead of each being sized as if it were alone.
_SUBAGENT_OVERHEAD_TOKENS = 4_000
# Slack between the planned request and the hard limit. The message-list size
# is an ESTIMATE (tokenisers disagree by model and language), so a plan that
# lands exactly on the window lands over it about half the time.
_SUBAGENT_SAFETY_MARGIN_SHARE = 0.05


def subagent_context_window(model: str) -> int:
    """The model's real window, or a conservative guess."""
    from flowly.compaction.service import _heuristic_context_window

    window = 0
    try:
        from flowly.integrations.model_catalog import get_context_window

        window = get_context_window(model) or 0
    except Exception:  # noqa: BLE001 — catalog is best-effort
        window = 0
    if not window:
        window = _heuristic_context_window(model) or 128_000
    return max(1, window)


def subagent_output_budget(model: str) -> int:
    """Output tokens one subagent reply may claim.

    A constant 16,384 was sent to every model. On a 16,385-token model that
    leaves one token for the entire request — the provider rejects it, and no
    amount of trimming the message list can help, because the overflow is the
    reply budget itself.
    """
    window = subagent_context_window(model)
    return max(512, min(_SUBAGENT_MAX_OUTPUT_TOKENS,
                        int(window * _SUBAGENT_MAX_OUTPUT_SHARE)))


def _subagent_context_budget(model: str) -> int:
    """Tokens a subagent may spend on its message list for ``model``.

    Sized against the SAME window as the reply and the fixed overhead, so the
    three together fit one request. Budgeting the message list alone let a
    16,385-token model be handed 8,385 tokens of messages plus a 16,384-token
    reply plus tool schemas — 25K+ into a 16K window, rejected every time.
    """
    window = subagent_context_window(model)
    reserve = max(
        subagent_output_budget(model),
        min(_SUBAGENT_RESERVE_TOKENS, int(window * _SUBAGENT_MAX_RESERVE_SHARE)),
    )
    margin = int(window * _SUBAGENT_SAFETY_MARGIN_SHARE)
    return max(1_000, window - reserve - _SUBAGENT_OVERHEAD_TOKENS - margin)


def _trim_to_context_budget(
    messages: list[dict[str, Any]],
    model: str,
    *,
    budget: int | None = None,
) -> list[dict[str, Any]]:
    """Collapse old tool results until the list fits the model's window.

    Subagents deliberately do NOT summarize: their context is disposable by
    design, and a summarization call inside one doubles its cost for work
    that is thrown away when it returns. Progressive truncation is the cheap
    half of what the main loop does, and it is what keeps a long subagent
    from dying on a provider 413 instead of returning its result.
    """
    from flowly.compaction.estimator import estimate_messages_tokens

    # A child run passes the budget it recorded for the step, so a restart
    # compacts its rebuilt list exactly as the live one was.
    budget = _subagent_context_budget(model) if budget is None else budget
    if estimate_messages_tokens(messages) <= budget:
        return messages

    for keep_last, max_chars in ((2, 200), (1, 120), (1, 60)):
        messages = _strip_subagent_tool_results(
            messages, keep_last=keep_last, max_old_chars=max_chars,
        )
        if estimate_messages_tokens(messages) <= budget:
            return messages

    logger.warning(
        "Subagent context still over budget after truncation "
        f"({estimate_messages_tokens(messages)} > {budget}); the provider may reject it"
    )
    return messages


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
    ):
        self.provider = provider
        # The main agent's own limit; AgentLoop passes it in.
        self.max_iterations = max(1, int(max_iterations))
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecConfig()
        self._registry = registry or SubagentRegistry()
        # This manager's identity as an owner of runs (subagent_owner).
        self._instance = uuid.uuid4().hex
        subagent_owner.register_instance(self._instance)
        self._started_at = time.time()
        # Results agent_wait handed to a turn not yet saved: run_id → the
        # conversation's key. Saving the turn delivers them; a turn that
        # ends without saving gives them back (release_taken).
        self._taken: dict[str, str] = {}
        # Reads the run ids a conversation's saved turns handled (the
        # session's SUBAGENT_REPORTS_KEY). AgentLoop sets it; None = unknown.
        self.committed_reports: Callable[[str], set[str]] | None = None
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

    def turn_reports(self, session_key: str, notice_run_ids: list[str] | None = None) -> list[str]:
        """The results a turn of ``session_key`` handled, to commit with its save.

        The runs a notice reported plus those agent_wait handed to the turn.
        The caller records them in the session (record_subagent_reports),
        saves, then calls mark_delivered.
        """
        taken = [rid for rid, key in self._taken.items() if key == session_key]
        return list(dict.fromkeys([*(notice_run_ids or []), *taken]))

    def mark_delivered(self, parent_session_key: str, run_ids: list[str]) -> None:
        """Record that the conversation has saved a turn handling these results."""
        for run_id in dict.fromkeys(run_ids):
            if self._taken.get(run_id) == parent_session_key:
                del self._taken[run_id]
            record = self._registry.get(run_id)
            if (
                record is not None
                and record.parent_session_key == parent_session_key
                and record.ended_at is not None
                and record.delivery_state in ("pending", "queued", "taken")
            ):
                self._registry.update(run_id, delivery_state="delivered")

    def release_taken(self, session_key: str) -> None:
        """A turn of ``session_key`` ended: results it took but never saved go back.

        agent_wait handed them to the turn, but the turn was not saved (it
        failed, or was dropped), so the conversation does not have them:
        they are announced again instead of being lost.
        """
        for run_id, key in list(self._taken.items()):
            if key != session_key:
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
        now = time.time()
        for record in records:
            if record.ended_at is None or record.delivery_state not in ("pending", "queued", "taken"):
                continue
            if record.kind != "subagent":
                # A delegated task reports through its own message, not a
                # subagent notice; it is never announced again here.
                if now - record.ended_at >= _REANNOUNCE_WITHIN_SECONDS:
                    self._registry.update(record.run_id, delivery_state="undelivered")
                continue
            if self._adopt(record, owners) is None:
                continue
            if record.run_id in self._committed(record.parent_session_key):
                # The turn that handled it was saved; the process stopped
                # before recording that here.
                self._registry.update(record.run_id, delivery_state="delivered")
                continue
            recent = now - record.ended_at < _REANNOUNCE_WITHIN_SECONDS
            if recent and record.reannounced < _MAX_REANNOUNCE and record.outcome != "cancelled":
                # Announced before the restart but never handled: it must be
                # claimable again by its batch's notice.
                # A result held for its batch was never announced: holding it
                # again across restarts does not count against it.
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
        owners: dict[str, str] = {}
        now = time.time()
        continued: list[tuple[SubagentRunRecord, ChildState]] = []
        settled: list[tuple[SubagentRunRecord, str, str | None, str | None]] = []
        for record in records:
            if record.ended_at is not None or record.run_id in self._running_tasks:
                continue
            if record.resumable and not self.resume_enabled:
                continue  # a gateway on this home continues it
            # Its last sign of life, read before the claim updates it.
            last_seen = record.suspended_at or record.updated_at or record.created_at
            adopted = self._adopt(record, owners)
            if adopted is None:
                continue
            state, refusal = self._restorable(adopted)
            if state is not None and now - last_seen > _RESUME_WITHIN_SECONDS:
                settled.append((adopted, "interrupted", "suspended_too_long", (
                    f"Flowly was not running for {_age_text(now - last_seen)} while this task ran, "
                    "longer than a subagent is continued after a restart (24 h). It was stopped "
                    "where it was; nothing it had started was run again."
                )))
            elif state is not None and adopted.resume_count >= _MAX_RESUMES:
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
            else:
                settled.append((adopted, "error", "process_restarted", None))
        try:
            self._reconcile_undelivered(records, owners)
        except (OSError, ValueError):
            logger.exception("Subagent delivery recovery unavailable")
        if settled:
            logger.warning(f"[SubagentManager] Settling {len(settled)} task(s) a restart stopped")
        for record, outcome, code, text in settled:
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
                continue
            self._spawn_announce(
                record.run_id, text or "The process restarted before this task completed.",
                outcome,
            )
        for record, state in continued:
            try:
                self._resume(record, state)
            except Exception:
                logger.exception(f"[SubagentManager] [{record.run_id[:8]}] could not continue")
                self._registry.finish(record.run_id, "error", error_code="process_restarted",
                                      error="Process restarted before task completed")
                self._record_metric(record.run_id)
                self._spawn_announce(record.run_id, "The process restarted before this task completed.",
                                     "error")

    def _restorable(self, record: SubagentRunRecord) -> tuple[ChildState | None, str | None]:
        """The state a run's checkpoint rebuilds, or why there is none."""
        if not record.resumable:
            return None, "not_resumable"
        try:
            records = read_checkpoint(self._registry.checkpoint_path(record.run_id))
        except OSError:
            logger.exception(f"[SubagentManager] [{record.run_id[:8]}] checkpoint unreadable")
            return None, "unreadable"
        restored = restore_child_state(
            records, strip=_strip_subagent_tool_results,
            trim=self._trim_for(record.model or self.model),
            annotate=annotate_search_repetition,
        )
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

        if self._shutting_down:
            return json.dumps({
                "status": "rejected",
                "error": "Flowly is shutting down; no new subagent can start now.",
            })
        parent_session_key = parent_session_key or f"{origin_channel}:{origin_chat_id}"
        # Concurrency limits — a runaway parent can't pin the event loop, and
        # one conversation can't take every slot from the others.
        if len(self._running_tasks) >= _MAX_CONCURRENT_SUBAGENTS:
            logger.warning(f"[SubagentManager] Concurrent limit reached ({_MAX_CONCURRENT_SUBAGENTS})")
            return json.dumps({
                "status": "rejected",
                "error": (
                    f"Maximum {_MAX_CONCURRENT_SUBAGENTS} subagents are running. "
                    "Wait for one to finish, or do this part yourself."
                ),
            })
        siblings = self._running_siblings(parent_session_key)
        if origin != "system" and len(siblings) >= MAX_CHILDREN_PER_CONVERSATION:
            return json.dumps({
                "status": "rejected",
                "error": (
                    f"{MAX_CHILDREN_PER_CONVERSATION} subagents are already running "
                    "for this conversation. Wait for one to finish, or do this part "
                    "yourself."
                ),
            })

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
        self._registry.register(record)

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
        return json.dumps({
            "status": "started",
            "run_id": run_id,
            "title": resolved_display,
            "model": resolved_model,
            "message": (
                f"'{resolved_display}' is running in the background. When everything "
                "you started in this turn has finished, the results reach you "
                "together as a new message and you continue from there. Don't wait "
                "for them, don't redo their tasks and don't guess their results: do "
                "any work that does not depend on them, then tell the owner briefly "
                "what is running and end your turn."
            ),
        })

    # ------------------------------------------------------------------
    # Internal execution (core loop preserved from original)
    # ------------------------------------------------------------------

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
                self._cancel_outcomes.pop(_rid, None)
                self._running_tasks.pop(_rid, None)
                self._inboxes.pop(_rid, None)
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
        if not self._suspending:
            return False
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

        try:
            run = self._child_run(
                run_id, task, label, origin_channel, origin_chat_id, model,
                timeout_seconds, context, tool_trace, restored,
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
            self._registry.finish(run_id, status, result=durable_result,
                                  error=error_str, error_code=error_code)

        except asyncio.TimeoutError:
            logger.warning(f"[SubagentManager] [{run_id[:8]}] timed out")
            self._registry.finish(run_id, "timeout", error="Task timed out", error_code="timeout")
            final_result = "(task timed out)"
            status = "timeout"

        except asyncio.CancelledError:
            if self._suspends(run_id):
                # A clean shutdown: the run is stopped to be continued after
                # the restart, not finished. Nothing is announced.
                suspended = True
                logger.info(f"[SubagentManager] [{run_id[:8]}] suspended for the restart")
                self._log(run_id, "suspended", durable=True, reason=self._suspend_reason)
                raise
            # Cancelled via cancel_children (parent interrupt cascade) or
            # cancel() (explicit user cancel). Persist the outcome and
            # announce to the parent so the user sees what happened —
            # then re-raise so asyncio considers the task properly
            # cancelled and doesn't treat it as a "completed" future.
            logger.info(f"[SubagentManager] [{run_id[:8]}] cancelled")
            outcome = self._cancel_outcomes.get(run_id, "interrupted")
            self._finish_cancelled(run_id, outcome)
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
            self._registry.finish(run_id, "error", error="Task could not finish", error_code="execution_failed")
            final_result = "Error: Task could not finish."
            status = "error"
            error_str = final_result

        finally:
            if not suspended:
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
        state.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "name": protocol_name,
            "content": content,
        })
        self._log(run_id, "tool", callId=call_id, protocolName=protocol_name, content=content,
                  status=status, guard=guard, search=search)

    def _trim_for(self, model: str) -> Callable[..., list[dict[str, Any]]]:
        def trim(messages: list[dict[str, Any]], *, budget: int) -> list[dict[str, Any]]:
            return _trim_to_context_budget(messages, model, budget=budget)
        return trim

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
    ) -> tuple[str, str, str | None, str | None]:
        """The child's own loop. Returns (result, status, error, error_code)."""
        status: str = "ok"
        error_str: str | None = None
        error_code: str | None = None
        tools = self._child_tools(run_id, label)
        trim = self._trim_for(model)

        if restored is None:
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
            first_message = (
                f"## Conversation so far\n\n{context}\n\n## Your task\n\n{task}"
                if context else task
            )
            state = ChildState(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": first_message},
                ],
                max_iterations=self.max_iterations,
            )
            self._log(
                run_id, "header", durable=True, runId=run_id, coreVersion=_core_version(),
                model=model, label=label, maxIterations=state.max_iterations,
                systemPrompt=system_prompt, firstMessage=first_message,
                tools=sorted(tools.tool_names),
            )
            self._transcribe(run_id, {
                "kind": "brief", "content": _clip(task, 8_000), "withConversation": bool(context),
            })
        else:
            state = restored
            if state.final is not None:
                # The result was decided before the restart; only recording
                # it was left. No model call.
                final = state.final
                return (final["result"] or "Task completed but no final response was generated.",
                        final["status"], final["error"], final["errorCode"])

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
                # compaction the step needs — recorded so a restart rebuilds
                # the same list.
                mail = self._take_inbox_entries(run_id)
                budget = _subagent_context_budget(model)
                apply_step(state, state.iteration + 1, [m["text"] for m in mail], budget,
                           _strip_subagent_tool_results, trim)
                self._log(run_id, "step", iteration=state.iteration,
                          notes=[m["text"] for m in mail], mail=[m["id"] for m in mail], budget=budget)
            iteration = state.iteration
            await self._progress(run_id, {"phase": "thinking", "iteration": iteration})
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
            response = await self.provider.chat(
                messages=state.messages,
                tools=list(tool_disclosure.definitions),
                model=model,
                max_tokens=subagent_output_budget(model),
                timeout=_llm_call_timeout,
            )
            await self._count_usage(run_id, response.usage)
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
                state.messages.append(assistant)
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
                    # Truncate long tool results to prevent context bloat
                    if len(result) > 4000:
                        result = result[:4000] + f"\n[... truncated from {len(result)} chars]"
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
            else:
                # Error response (timeout, provider error)
                if response.finish_reason == "error":
                    _consecutive_errors += 1
                    # P1.4 — route by error category. Rate limits get
                    # long jittered waits (anti thundering-herd);
                    # context overflow and auth are not retryable
                    # (retrying the same prompt / bad key is pointless);
                    # everything else falls through to short jitter.
                    from flowly.agent.error_classifier import (
                        classify_response, backoff_for, ErrorCategory,
                    )
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

                    # One-shot overflow recovery. Giving up here throws
                    # away everything the subagent has already done, when
                    # the fix is usually just old tool output it no longer
                    # needs. Trim hard and retry once; a second overflow
                    # means the task really is too big.
                    if (
                        _category in {
                            ErrorCategory.CONTEXT_OVERFLOW,
                            ErrorCategory.INPUT_TOO_LARGE,
                        }
                        and not state.input_budget_recovered
                    ):
                        state.input_budget_recovered = True
                        _before = len(state.messages)
                        state.messages = _strip_subagent_tool_results(
                            state.messages, keep_last=1, max_old_chars=60,
                        )
                        self._log(run_id, "recovery", iteration=iteration)
                        logger.warning(
                            f"[SubagentManager] [{run_id[:8]}] input budget exceeded — "
                            f"trimmed tool history ({_before} messages) and retrying once"
                        )
                        continue

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
                    state.messages.append({"role": "assistant", "content": response.content})
                    self._log(run_id, "text", iteration=iteration, content=response.content)
                    continue
                final_result = response.content
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
                state.messages.append({"role": "user", "content": GRACE_PROMPT})
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
                summary_response = await self.provider.chat(
                    messages=state.messages,
                    tools=None,  # CRITICAL: prevent another tool loop
                    model=model,
                    max_tokens=4096,
                    timeout=60,
                )
                await self._count_usage(run_id, summary_response.usage)
                if summary_response.finish_reason != "error" and summary_response.content and summary_response.content.strip():
                    final_result = summary_response.content
                    status, error_str, error_code = "ok", None, None
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

    async def send_to_child(self, run_id: str, text: str) -> dict[str, Any]:
        """Queue a message from the main agent for a running subagent.

        The child reads it at its next step — after the tool call it is in,
        never by interrupting one. A finished run cannot receive messages.
        """
        record = self._registry.get(run_id)
        if record is None:
            return {"run_id": run_id, "status": "not_found"}
        if record.ended_at is not None or run_id not in self._running_tasks:
            return {"run_id": run_id, "status": "finished",
                    "error": "This subagent has already finished; it can't receive messages."}
        mail_id = uuid.uuid4().hex[:16]
        # Durable before the main agent is told it was delivered: a restart
        # gives the child every message it had not read yet.
        self._log(run_id, "mail", durable=True, id=mail_id, text=text)
        self._inboxes.setdefault(run_id, []).append({"id": mail_id, "text": text})
        self._inbox_events.setdefault(run_id, asyncio.Event()).set()
        self._transcribe(run_id, {"kind": "message_in", "from": "orchestrator", "content": text})
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
        return [e["text"] for e in entries]

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
            self._taken[record.run_id] = parent_session_key
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
                data = run_view(record, detail=event == "subagent.completed")
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
        if record.started_at and record.ended_at:
            # Time it worked: the downtime of a restart it continued across
            # is left out.
            runtime = int(max(0.0, record.ended_at - record.started_at - (record.suspended_seconds or 0.0)))
            continued = ", continued after a restart" if record.resumed_at else ""
            duration = f" (runtime: {runtime}s{continued})"

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
            f"[Subagent '{title}' {status_text}{duration}] run_id={record.run_id}{tool_summary}{late}\n\n"
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
        if len(to_tell) == 1:
            announce_content = (
                f"{sections[0]}\n\n"
                "This is the subagent's own report: check what matters before relying "
                "on it, and don't redo its work. Tell the owner what it means for their "
                "request in your own words; don't show run ids."
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
                "show run ids."
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
        held_since = min(
            (r.ended_at for r in self._registry.all()
             if r.batch_id == record.batch_id and r.parent_session_key == record.parent_session_key
             and r.ended_at is not None and not r.announced and r.delivery_state in ("pending", "queued")),
            default=time.time(),
        )
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
