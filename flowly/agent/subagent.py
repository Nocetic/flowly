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

from flowly.bus.events import InboundMessage
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMProvider
from flowly.artifacts.context import (
    INTERNAL_CONTEXT_TAGS,
    build_persisted_output_message,
    generate_preview,
    internal_context_metadata,
)
from flowly.agent.tools.base import Tool
from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, MemoryAppendTool
from flowly.agent.tools.shell import ExecTool
from flowly.exec.types import ExecConfig
from flowly.agent.tools.web import WebSearchTool, WebFetchTool, WebExtractTool
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord
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
# Flowly uses an explicit allowlist in _do_run (computed from the
# assistant's ``allowed_tools`` field) AND this blocklist as a
# defense-in-depth backstop. If the allowlist ever expands to cover a
# dangerous tool by accident, the blocklist still refuses the
# obviously-dangerous names regardless.
_BLOCKED_SUBAGENT_TOOLS: frozenset[str] = frozenset({
    # recursion / privilege escalation
    "spawn", "builtin_agent", "delegate_to",
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


# System-wide wall-clock cap for every subagent run. Not per-assistant
# configurable — keeping the user-facing config minimal is worth more
# than letting operators tune this. 15 min is generous: normal runs
# finish in 7-15 minutes (15 iterations × 30-60s average); anything
# over this ceiling is almost always a genuine hang that the per-tool
# (120s) / per-LLM-call (300s) / heartbeat guards failed to catch.
_SUBAGENT_WALL_CLOCK_TIMEOUT = 900

_SUBAGENT_OUTPUT_CONTEXT_LIMIT_CHARS = 6_000


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
    ):
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecConfig()
        self._registry = registry or SubagentRegistry()
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        # parent_session_key -> set of run_ids currently spawned by that
        # parent. Used by cancel_children() so AIAgent.interrupt(session_key)
        # can cascade into background subagents without killing everyone
        # (other sessions / other cron jobs keep running).
        self._children_by_parent: dict[str, set[str]] = {}
        self._busy_sessions: set[str] = set()
        # Per-session history of successful dispatches with associated
        # artifacts. Used by two consumers:
        #   - BuiltinAgentTool duplicate-dispatch guard: "did a run with
        #     this label finish in the last 10 min? refuse the second
        #     call, redirect to the existing artifact."
        #   - AgentLoop recent-artifacts hint injection: "list up to the
        #     last 5 artifacts in this session so the parent LLM can
        #     reference them correctly — but only the ones that really
        #     exist, never a blanket 'you may have artifacts' rule."
        # Stored as list (newest appended) so multiple artifacts for
        # the same label coexist (FRESH: bypass produces art_B alongside
        # art_A; both remain queryable). Capped at 20 per session to
        # prevent unbounded growth during marathon chats. In-memory
        # only — best-effort across restarts.
        self._recent_completed_dispatches: dict[str, list[dict[str, Any]]] = {}
        self._dispatch_history_cap_per_session = 20
        self._on_event = on_event  # async callback(event_name, data)
        self._state_dir = state_dir or (workspace / ".flowly_state")
        self._web_search_proxy_url = web_search_proxy_url
        self._web_search_server_id = web_search_server_id
        self._web_search_auth_token = web_search_auth_token
        self._artifact_on_change: Any | None = None  # Broadcast callback for S3 sync
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

    # ------------------------------------------------------------------
    # Duplicate-dispatch guard (builtin_agent mutex)
    # ------------------------------------------------------------------

    def recent_completed_dispatches(
        self,
        parent_session_key: str,
        max_age_seconds: float = 600.0,
    ) -> list[dict[str, Any]]:
        """Return every successful dispatch for this session within the
        age window, newest first.

        Multi-artifact aware: if the parent ran researcher twice (via
        FRESH: on the second call), both art_A and art_B appear here.
        Callers should NOT hint about artifacts when this list is
        empty, otherwise the LLM starts offering files that were never
        created.
        """
        records = self._recent_completed_dispatches.get(parent_session_key, [])
        if not records:
            return []
        now = time.time()
        fresh = [r for r in records if now - r["completed_at"] <= max_age_seconds]
        # Newest first — callers can truncate to a display cap.
        fresh.sort(key=lambda r: r["completed_at"], reverse=True)
        return fresh

    def last_completed_dispatch(
        self,
        parent_session_key: str,
        label: str,
        max_age_seconds: float = 600.0,
    ) -> dict[str, Any] | None:
        """Return the most recent successful dispatch for this parent
        session + label, or None if none within the window.

        Used by BuiltinAgentTool to refuse a same-label re-dispatch and
        redirect the parent LLM to the existing artifact. Falsely missing
        records (after a process restart, before any dispatch completes)
        are safe — the worst case is one extra dispatch. Falsely stale
        records are bounded by ``max_age_seconds``.
        """
        records = self._recent_completed_dispatches.get(parent_session_key, [])
        if not records:
            return None
        now = time.time()
        # Walk newest → oldest. First match within window is the answer.
        for r in reversed(records):
            if r["label"] != label:
                continue
            if now - r["completed_at"] > max_age_seconds:
                # Older than this is only going to be older still.
                return None
            return r
        return None

    def _record_completed_dispatch(
        self,
        parent_session_key: str,
        label: str,
        artifact_id: str,
        title: str,
    ) -> None:
        """Register a successful dispatch with an associated artifact.

        Appends to a per-session list so multiple dispatches (same or
        different labels) all stay queryable. Older entries age out
        naturally via the window check in the read APIs; the per-session
        cap prevents unbounded growth during very long chats.

        Failed or interrupted runs are deliberately NOT recorded so the
        LLM can retry them without tripping the guard.
        """
        records = self._recent_completed_dispatches.setdefault(
            parent_session_key, [],
        )
        records.append({
            "artifact_id": artifact_id,
            "title": title,
            "label": label,
            "completed_at": time.time(),
        })
        if len(records) > self._dispatch_history_cap_per_session:
            # Drop oldest. Guard/hint only care about recent entries
            # anyway, so this is pure memory hygiene.
            del records[0: len(records) - self._dispatch_history_cap_per_session]

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def resume_pending(self) -> None:
        """Called on startup: announce any tasks that were running when process crashed."""
        pending = self._registry.pending()
        if not pending:
            return
        logger.warning(f"[SubagentManager] Found {len(pending)} pending task(s) from previous run — marking as failed")
        for record in pending:
            self._registry.update(
                record.run_id,
                ended_at=time.time(),
                outcome="error",
                error="Process restarted before task completed",
            )
            # Track recovery announce tasks (audit: was fire-and-forget)
            task = asyncio.create_task(
                self._announce(record.run_id, "The process restarted before this task completed.", "error")
            )
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

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
        assistant: Any = None,  # Assistant | None — avoids circular import
        silent: bool = False,
        display_name: str | None = None,
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

        # Concurrency limit — cap parallel children so a runaway
        # parent can't spawn N subagents and pin the event loop.
        MAX_CONCURRENT = 5
        if len(self._running_tasks) >= MAX_CONCURRENT:
            logger.warning(f"[SubagentManager] Concurrent limit reached ({MAX_CONCURRENT})")
            return json.dumps({
                "status": "rejected",
                "error": f"Maximum {MAX_CONCURRENT} concurrent subagents. Wait for one to finish.",
            })

        run_id = str(uuid.uuid4())
        child_session_key = f"subagent:{run_id}"
        parent_session_key = f"{origin_channel}:{origin_chat_id}"
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
        )
        self._registry.register(record)

        logger.info(f"[SubagentManager] Spawned [{run_id[:8]}]: {resolved_display}")

        # Broadcast event to connected clients
        if self._on_event:
            try:
                asyncio.ensure_future(self._on_event("subagent.started", {
                    "runId": run_id[:8],
                    "label": resolved_display,
                    "task": task[:200],
                    "model": resolved_model,
                }))
            except Exception:
                pass

        if wait:
            # Sync mode — inline execution. Returns the child's final
            # response text directly so the parent (typically a cron run)
            # can aggregate it into the same turn instead of waiting for
            # an async announcement that arrives on a later session turn.
            return await self._run_subagent(
                run_id, task, display_label, origin_channel, origin_chat_id,
                resolved_model, timeout_seconds, skip_announce=True,
                assistant=assistant,
            )

        # Fire-and-forget default — background task, result announced
        # to the parent session via _announce when it completes.
        # silent=True (self-review) skips the announce: the child still
        # runs and does its work (memory_append / KG writes), but no
        # system message is pushed to the parent session — avoids a
        # parent LLM turn triggered just to paraphrase "review done".
        bg_task = asyncio.create_task(
            self._run_subagent(run_id, task, display_label, origin_channel, origin_chat_id,
                               resolved_model, timeout_seconds, assistant=assistant,
                               skip_announce=silent)
        )
        self._running_tasks[run_id] = bg_task
        self._children_by_parent.setdefault(parent_session_key, set()).add(run_id)

        def _cleanup(_task: asyncio.Task[None], _rid: str = run_id, _pk: str = parent_session_key) -> None:
            self._running_tasks.pop(_rid, None)
            siblings = self._children_by_parent.get(_pk)
            if siblings is not None:
                siblings.discard(_rid)
                if not siblings:
                    self._children_by_parent.pop(_pk, None)

        bg_task.add_done_callback(_cleanup)

        # Async dispatch envelope. The wording here is load-bearing —
        # earlier versions said only "Background task started. I'll
        # notify you when it completes." which the parent LLM happily
        # misread as "tool done, I can answer now" and then fabricated
        # an answer from its own knowledge while the real specialist
        # was still running (the "async amnesia" pattern observed in
        # production Kimi/Minimax sessions). We now spell out the
        # required next steps verbatim so there is no room to guess.
        return json.dumps({
            "status": "dispatched",
            "run_id": run_id[:8],
            "label": resolved_display,
            "message": (
                f"Specialist '{resolved_display}' is now running in the "
                "background. You DO NOT have its result yet."
            ),
            "required_next_steps": [
                "Emit ONE short acknowledgement to the user in their "
                "language. Keep it under 15 words. Do not preview an "
                "answer.",
                "End your turn immediately after the acknowledgement. "
                "Do not call another tool. Do not attempt to answer "
                "the user's actual question — you do not have the "
                "specialist's output yet and guessing from memory "
                "produces hallucinations.",
                "When the specialist finishes, its full result will "
                "be injected as a system message that wakes you on a "
                "later turn. ONLY THEN compose your final answer "
                "using the real data.",
            ],
        })

    # ------------------------------------------------------------------
    # Internal execution (core loop preserved from original)
    # ------------------------------------------------------------------

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
        assistant: Any = None,  # Assistant | None
    ) -> str:
        """Execute the subagent task and optionally announce the result.

        If ``skip_announce`` is True, the caller gets the final result text
        back directly and `_announce` (which enqueues an InboundMessage to
        the parent session so the parent agent can summarise the child's
        output) is skipped. Sync-mode spawn uses this so the cron agent
        receives the child's response inline instead of as a separate
        system message on a later turn.
        """
        logger.info(f"[SubagentManager] [{run_id[:8]}] starting: {label}")
        self._registry.update(run_id, started_at=time.time())

        async def _do_run() -> tuple[str, bool, list[dict[str, Any]]]:
            # Subagent tool registry — factory lookup + assistant-level
            # allowlist. SubagentToolRegistry still refuses blocked names
            # (spawn, delegate_to, cron, message, …) on register so a
            # broken allowlist can't smuggle a dangerous tool in either.
            # When the parent has governance enabled, fire its post_tool_call
            # hook from the subagent's registry too, so self-review's memory/KG
            # writes are governed like the main agent's.
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
                from flowly.artifacts.store import get_store
                from flowly.agent.tools.artifact import ArtifactTool
                art_tool = ArtifactTool(store=get_store(self._state_dir))
                if self._artifact_on_change:
                    art_tool.set_on_change(self._artifact_on_change)
                return art_tool

            from flowly.agent.tools.skill_manage import SkillManageTool

            # Factory map: name → builder. Used so assistant.allowed_tools
            # can turn off tools without having to import each class here.
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
            }

            # Default = every safe tool. Explicit allowed_tools on the
            # assistant narrows the set (artifact stays in the set —
            # it's the canonical output channel for auto_save_artifact
            # assistants and the only way to surface non-text outputs).
            if assistant is not None and assistant.allowed_tools is not None:
                requested = set(assistant.allowed_tools) | {"artifact"}
            else:
                requested = set(_TOOL_FACTORIES.keys())
            if label == "self-review":
                # Self-review is only for memory/KG evolution. Replace the
                # default tool set so it cannot create skills indirectly via
                # skill_manage or by writing workspace skill files.
                requested = {"memory_append", "knowledge_graph"}

            for tool_name in requested:
                factory = _TOOL_FACTORIES.get(tool_name)
                if factory is None:
                    logger.warning(
                        f"[SubagentManager] [{run_id[:8]}] assistant "
                        f"'{getattr(assistant, 'name', '?')}' listed "
                        f"unknown tool '{tool_name}', skipping"
                    )
                    continue
                try:
                    tools.register(factory())
                except ValueError as blocked:
                    # Blocked tool in allowed_tools → log + skip (the
                    # blocklist guard is authoritative; assistant file
                    # doesn't get to escalate privilege).
                    logger.warning(
                        f"[SubagentManager] [{run_id[:8]}] allowed_tools "
                        f"contains blocked tool: {blocked}"
                    )
                except Exception as e:
                    # Optional tools (KG, artifact) may fail to build on
                    # fresh installs. Non-fatal — just not available.
                    logger.debug(
                        f"[SubagentManager] [{run_id[:8]}] tool "
                        f"'{tool_name}' unavailable: {e}"
                    )

            # Build messages with subagent-specific prompt
            system_prompt = self._build_subagent_prompt(
                task, label=label, assistant=assistant,
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # Run agent loop (limited iterations)
            max_iterations = 15
            iteration = 0
            final_result: str | None = None
            _model_used_artifact = False
            # P1.2 — structured tool trace, one dict per call
            # (args_bytes / result_bytes / status / duration_ms) so the
            # parent LLM and registry audit know exactly what happened —
            # not just which tools fired.
            _tool_trace: list[dict[str, Any]] = []
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

            # Wall-clock cap is a system-wide constant — callers may
            # override (typically for tests) by passing a positive
            # timeout_seconds, but normal runs all land on the same
            # value. Kept as a last-resort hang trap below max_iter +
            # per-call + heartbeat.
            _total_timeout: float = float(
                timeout_seconds if (timeout_seconds and timeout_seconds > 0)
                else _SUBAGENT_WALL_CLOCK_TIMEOUT
            )
            _start_time = time.time()
            logger.info(
                f"[SubagentManager] [{run_id[:8]}] total timeout: "
                f"{_total_timeout:.0f}s, max_iter={max_iterations}"
            )

            while iteration < max_iterations:
                iteration += 1
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
                    _llm_call_timeout = 300.0

                # Context management: strip old tool results to save tokens
                # (keep recent, compact old)
                if iteration > 3:
                    messages = _strip_subagent_tool_results(
                        messages, keep_last=3, max_old_chars=300,
                    )
                if iteration > 7:
                    messages = _strip_subagent_tool_results(
                        messages, keep_last=2, max_old_chars=150,
                    )

                response = await self.provider.chat(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=model,
                    max_tokens=16384,
                    timeout=_llm_call_timeout,
                )

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
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                    })

                    # Execute tools
                    for tool_call in response.tool_calls:
                        logger.debug(f"[SubagentManager] [{run_id[:8]}] tool: {tool_call.name}")
                        if tool_call.name == "artifact":
                            _model_used_artifact = True
                        _heartbeat(
                            f"subagent {label}: running {tool_call.name}",
                            tool_call.name,
                        )
                        _tool_t0 = time.monotonic()
                        _tool_status = "ok"
                        try:
                            tool_args = dict(tool_call.arguments)
                            if (
                                tool_call.name == "artifact"
                                and "session_key" not in tool_args
                            ):
                                tool_args["session_key"] = f"{origin_channel}:{origin_chat_id}"
                            result = await asyncio.wait_for(
                                tools.execute(tool_call.name, tool_args),
                                timeout=120,  # Per-tool timeout
                            )
                        except asyncio.TimeoutError:
                            result = f"Error: Tool '{tool_call.name}' timed out after 120s"
                            _tool_status = "timeout"
                            logger.warning(f"[SubagentManager] [{run_id[:8]}] tool timeout: {tool_call.name}")
                        except Exception as e:
                            result = f"Error executing {tool_call.name}: {e}"
                            _tool_status = "error"
                            logger.warning(f"[SubagentManager] [{run_id[:8]}] tool error: {tool_call.name}: {e}")
                        _tool_duration_ms = int((time.monotonic() - _tool_t0) * 1000)
                        # Flip status to "error" if the tool returned a
                        # human-readable error string without raising
                        # (common for validation-level failures).
                        if _tool_status == "ok" and result.startswith("Error"):
                            _tool_status = "error"
                        _tool_trace.append({
                            "tool": tool_call.name,
                            "args_bytes": len(json.dumps(tool_call.arguments or {})),
                            "result_bytes": len(result),
                            "status": _tool_status,
                            "duration_ms": _tool_duration_ms,
                        })
                        # Truncate long tool results to prevent context bloat
                        if len(result) > 4000:
                            result = result[:4000] + f"\n[... truncated from {len(result)} chars]"
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result,
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
                        _delay = backoff_for(_category, _consecutive_errors)
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
                            break

                        if _consecutive_errors <= _MAX_CONSECUTIVE_ERRORS and iteration < max_iterations:
                            logger.warning(
                                f"[SubagentManager] [{run_id[:8]}] LLM error "
                                f"#{_consecutive_errors} ({_category.value}) on "
                                f"iteration {iteration}, backoff {_delay:.1f}s: "
                                f"{_err_snippet}"
                            )
                            await asyncio.sleep(_delay)
                            continue
                        else:
                            logger.error(
                                f"[SubagentManager] [{run_id[:8]}] LLM errors exhausted "
                                f"({_consecutive_errors} consecutive, last={_category.value})"
                            )
                            final_result = f"Error: Task failed after {_consecutive_errors} consecutive LLM errors."
                            break

                    # Empty response check
                    if not response.content or not response.content.strip():
                        logger.warning(f"[SubagentManager] [{run_id[:8]}] empty response on iteration {iteration}")
                        if iteration < max_iterations:
                            continue  # Retry
                        final_result = "Task completed but produced no output."
                        break

                    # Successful response — reset error counter
                    _consecutive_errors = 0
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
            _hit_max_iter = final_result is None and iteration >= max_iterations
            _hit_llm_errors = (
                final_result is not None
                and final_result.startswith("Error: Task failed")
                and bool(_tool_trace)
            )
            if _hit_max_iter or _hit_llm_errors:
                _reason = "max iterations" if _hit_max_iter else "LLM errors exhausted"
                logger.info(
                    f"[SubagentManager] [{run_id[:8]}] {_reason} — "
                    "injecting summary grace-call"
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "You cannot call any more tools. Provide a brief "
                        "final summary of what you accomplished and found "
                        "so far based on your previous tool results. Do "
                        "not call tools — just answer directly."
                    ),
                })
                _heartbeat(f"subagent {label}: generating summary", None)
                _preserved = final_result  # None for max-iter, error string for llm-errors
                try:
                    summary_response = await self.provider.chat(
                        messages=messages,
                        tools=None,  # CRITICAL: prevent another tool loop
                        model=model,
                        max_tokens=4096,
                        timeout=60,
                    )
                    if summary_response.content and summary_response.content.strip():
                        final_result = summary_response.content
                    elif _hit_max_iter:
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
                        final_result = (
                            f"Reached the iteration limit ({max_iterations}) "
                            f"and the summary attempt failed: {e}"
                        )
                    # _hit_llm_errors + grace-call failed → keep _preserved error
                    else:
                        final_result = _preserved

            _tool_names = [t.get("tool", "?") for t in _tool_trace[:20]]
            logger.info(
                f"[SubagentManager] [{run_id[:8]}] finished: {iteration} iterations, "
                f"{len(_tool_trace)} tool calls: {', '.join(_tool_names)}"
            )
            return (
                final_result or "Task completed but no final response was generated.",
                _model_used_artifact,
                _tool_trace,
            )

        final_result: str = ""
        status: str = "ok"
        error_str: str | None = None
        tool_trace: list[dict[str, Any]] = []

        # Artifact save is OPT-IN. Driven solely by
        # ``assistant.auto_save_artifact``; there is no longer a
        # label-based fallback (used to be ``builtin:writer`` /
        # ``builtin:researcher`` always saving regardless of the
        # assistant config — removed 2026-04-23 because users kept
        # getting artifacts they didn't ask for). The output cap
        # below still routes oversize results to the artifact store
        # for context protection, but that's a context-management
        # mechanism, not a "save this report" decision.
        _auto_save = bool(getattr(assistant, "auto_save_artifact", False))
        _art_type = getattr(assistant, "artifact_type", "markdown") or "markdown"

        try:
            # Wall-clock guard is opt-in: positive timeout_seconds enforces
            # a hard cap; 0 / None runs unbounded (rely on max_iter +
            # per-call + heartbeat).
            if timeout_seconds and timeout_seconds > 0:
                final_result, model_used_artifact, tool_trace = await asyncio.wait_for(
                    _do_run(), timeout=timeout_seconds,
                )
            else:
                final_result, model_used_artifact, tool_trace = await _do_run()

            # Assistant-opt-in auto-save. Skip if the model already used
            # the artifact tool itself (prevents duplicates) or if the
            # result is an error message.
            if (
                _auto_save
                and final_result
                and not final_result.startswith("Error")
                and not model_used_artifact
            ):
                artifact_id = await self._save_result_as_artifact(
                    run_id, label, task, final_result, art_type=_art_type,
                    session_key=f"{origin_channel}:{origin_chat_id}",
                )
                if artifact_id:
                    final_result = (
                        f"{final_result}\n\n"
                        f"[Result saved as artifact: {artifact_id}]"
                    )

            # Opt-in context persistence for subagents whose output is
            # structurally long (researcher, writer, user assistants with
            # ``cap_to_artifact: true`` in their frontmatter). For terse
            # specialists (coder, reviewer) and unconfigured callers, the
            # final_result is returned verbatim — no artifact bloat for a
            # "LGTM, 2 nits" review or a short patch.
            #
            # When a parking opts in and fires, the parent gets a
            # <persisted-output> envelope with preview + artifact id, and
            # an existing auto-save artifact (writer/researcher emitted
            # during its run) is reused instead of duplicated.
            _cap_opt_in = bool(getattr(assistant, "cap_to_artifact", False))
            artifact_id_for_cap: str | None = None
            if (
                _cap_opt_in
                and len(final_result) > _SUBAGENT_OUTPUT_CONTEXT_LIMIT_CHARS
                and not final_result.startswith("Error")
            ):
                _embed_marker = "[Result saved as artifact: "
                _embed_idx = final_result.rfind(_embed_marker)
                if _embed_idx >= 0:
                    _end = final_result.find("]", _embed_idx)
                    if _end > _embed_idx:
                        artifact_id_for_cap = final_result[
                            _embed_idx + len(_embed_marker):_end
                        ].strip()
                if artifact_id_for_cap is None:
                    # User-visible by default (Claude Code pattern): the
                    # research output IS the persistence — no separate
                    # "promote to make it visible" step. The artifact
                    # lands in the desktop list immediately and the
                    # user can see/open it. The parent LLM still sees
                    # only the <persisted-output> preview so its
                    # context stays lean; the two concerns
                    # (user visibility vs parent context size) are
                    # independent.
                    artifact_id_for_cap = await self._save_result_as_artifact(
                        run_id,
                        label,
                        task,
                        final_result,
                        source="subagent_result",
                        session_key=f"{origin_channel}:{origin_chat_id}",
                    )
                if artifact_id_for_cap:
                    _total = len(final_result)
                    _preview, _has_more = generate_preview(final_result)
                    final_result = build_persisted_output_message(
                        artifact_id=artifact_id_for_cap,
                        original_size=_total,
                        preview=_preview,
                        has_more=_has_more,
                        source="subagent result",
                    )
                    logger.info(
                        f"[SubagentManager] [{run_id[:8]}] output persisted "
                        f"({_total:,} → ~{len(final_result):,} chars, "
                        f"artifact={artifact_id_for_cap})"
                    )

            # Classify the outcome. `_do_run` signals LLM-error exhaustion
            # by returning final_result starting with "Error: Task failed"
            # — that's not an exception, so without this check the registry
            # would record outcome="ok" even when every LLM call hit a 504
            # and the parent receives a bare error string.
            if final_result and final_result.startswith("Error: Task failed"):
                status = "error"
                error_str = final_result
                logger.warning(
                    f"[SubagentManager] [{run_id[:8]}] completed with errors"
                )
            else:
                logger.info(f"[SubagentManager] [{run_id[:8]}] completed successfully")
                # Register for the duplicate-dispatch guard. Only successful
                # runs that produced a persisted artifact are recorded —
                # failed/interrupted runs stay invisible so the parent LLM
                # can retry them without being blocked. Short sync runs that
                # never triggered the cap are also invisible (they return
                # inline text; re-dispatching a coder one-liner is cheap).
                if artifact_id_for_cap:
                    self._record_completed_dispatch(
                        parent_session_key=f"{origin_channel}:{origin_chat_id}",
                        label=label,
                        artifact_id=artifact_id_for_cap,
                        title=(task or label)[:200],
                    )
            self._registry.update(
                run_id, ended_at=time.time(), outcome=status,
                error=error_str, tool_trace=tool_trace,
            )

        except asyncio.TimeoutError:
            logger.warning(f"[SubagentManager] [{run_id[:8]}] timed out")
            self._registry.update(
                run_id, ended_at=time.time(), outcome="timeout", tool_trace=tool_trace,
            )
            final_result = "(task timed out)"
            status = "timeout"

        except asyncio.CancelledError:
            # Cancelled via cancel_children (parent interrupt cascade) or
            # cancel() (explicit user cancel). Persist the outcome and
            # announce to the parent so the user sees what happened —
            # then re-raise so asyncio considers the task properly
            # cancelled and doesn't treat it as a "completed" future.
            logger.info(f"[SubagentManager] [{run_id[:8]}] cancelled")
            self._registry.update(
                run_id,
                ended_at=time.time(),
                outcome="interrupted",
                error="Parent interrupted before the task could finish",
                tool_trace=tool_trace,
            )
            if not skip_announce:
                try:
                    await self._announce(run_id, "(task was interrupted)", "interrupted")
                except Exception:
                    logger.exception(f"[SubagentManager] [{run_id[:8]}] announce on cancel failed")
            try:
                await self._emit_event("subagent.completed", run_id, label, "interrupted", None)
            except Exception:
                pass
            raise

        except Exception as e:
            logger.error(f"[SubagentManager] [{run_id[:8]}] failed: {e}")
            self._registry.update(
                run_id,
                ended_at=time.time(),
                outcome="error",
                error=str(e),
                tool_trace=tool_trace,
            )
            final_result = f"Error: {e}"
            status = "error"
            error_str = str(e)

        if not skip_announce:
            await self._announce(run_id, final_result, status)
        await self._emit_event("subagent.completed", run_id, label, status, error_str)
        return final_result

    async def _emit_event(self, event_name: str, run_id: str, label: str, outcome: str, error: str | None = None) -> None:
        """Broadcast subagent lifecycle event to connected clients."""
        if not self._on_event:
            return
        # P1.2 — surface the structured tool_trace so the desktop UI /
        # telemetry can show what the child actually did. Registry is
        # fetched fresh here so the caller doesn't need to pass it.
        record = self._registry.get(run_id)
        tool_trace = record.tool_trace if record else []
        try:
            await self._on_event(event_name, {
                "runId": run_id[:8],
                "label": label,
                "outcome": outcome,
                "error": error,
                "running": len(self._running_tasks),
                "toolTrace": tool_trace,
            })
        except Exception:
            pass

    async def _announce(self, run_id: str, result: str, status: str) -> None:
        """Deliver result to parent agent — via queue if parent is busy."""
        record = self._registry.get(run_id)
        if not record:
            return

        status_text = {
            "ok": "completed successfully",
            "error": "failed",
            "timeout": "timed out",
        }.get(status, status)

        duration = ""
        if record.started_at and record.ended_at:
            secs = int(record.ended_at - record.started_at)
            duration = f" (runtime: {secs}s)"

        # Truncate result for announce — parent sees a summary, not full output
        # Full content is in the artifact — parent just needs to know it's done
        _result_preview = result[:2000] if len(result) > 2000 else result
        if len(result) > 2000:
            _result_preview += f"\n[... full result truncated from {len(result)} chars — saved as artifact]"

        # P1.2 — one-line "Tools used: web_search×2, exec×1" summary. Helps
        # the parent LLM reason about what the child actually did instead
        # of treating the summary as a black box.
        _tool_summary = ""
        if record.tool_trace:
            from collections import Counter
            counts = Counter(t.get("tool", "?") for t in record.tool_trace)
            parts = [f"{name}×{n}" if n > 1 else name for name, n in counts.most_common()]
            _tool_summary = f"\nTools used ({len(record.tool_trace)} calls): {', '.join(parts)}"

        _disp = record.display_name or record.label
        announce_content = (
            f"[Background task '{_disp}' {status_text}{duration}]{_tool_summary}\n\n"
            f"Task: {record.task}\n\n"
            f"Result:\n{_result_preview}\n\n"
            "Summarize this naturally for the user. Keep it brief (1-2 sentences). "
            "Do not mention technical details like 'subagent' or task IDs."
        )

        parent_session_key = record.parent_session_key

        async def _send(prompt: str) -> None:
            msg = InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id=parent_session_key,
                content=prompt,
            )
            await self.bus.publish_inbound(msg)
            logger.debug(f"[SubagentManager] [{run_id[:8]}] announced to {parent_session_key}")

        if self.is_busy(parent_session_key):
            # Parent is currently processing — queue the announce
            q = get_or_create_queue(parent_session_key, _send)
            await q.enqueue(AnnounceItem(
                prompt=announce_content,
                summary=f"{_disp} — {status_text}",
            ))
            logger.debug(f"[SubagentManager] [{run_id[:8]}] queued announce (parent busy)")
        else:
            await _send(announce_content)

        self._registry.update(run_id, announced=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_subagent_prompt(
        self, task: str, label: str = "", assistant: Any = None,
    ) -> str:
        """Build a focused system prompt for the subagent.

        If ``assistant`` is supplied, its ``system_prompt`` body is
        appended verbatim — this is how user-defined personas
        (``~/.flowly/assistants/*.md``) and migrated builtins both
        deliver their instructions. The legacy hardcoded writer /
        researcher / coder blocks are now seeds in
        ``flowly.agent.assistants._BUILTIN_ASSISTANTS``.
        """
        base = f"""# Subagent

Complete ONLY the assigned task. Stay focused, be concise, do not initiate side tasks.
You MUST use tools to take action — never describe what you would do without doing it.
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

        # Built-in agents: inject user context (memory + KG), but only
        # after a prompt-injection scan. Matches Faz 5.2's hardening on
        # the main agent: memory and KG are treated like bootstrap
        # context files, so a poisoned entry (e.g. a stray
        # "ignore previous instructions" snippet written by an earlier
        # turn) surfaces as a [BLOCKED: …] placeholder instead of being
        # silently relayed into the child's system prompt.
        # P2.8 — inject for any assistant-driven run (registry or
        # legacy builtin: label), not self-review or one-off spawns.
        if assistant is not None or label.startswith("builtin:"):
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

        # P2.8 — assistant persona injection. Replaces the hardcoded
        # writer/researcher/coder blocks; those are now seeded in
        # _BUILTIN_ASSISTANTS and reach us via the assistant argument
        # along with any user-defined ones from ~/.flowly/assistants/.
        if assistant is not None and assistant.system_prompt:
            base += "\n" + assistant.system_prompt.strip() + "\n"

        return base

    async def _save_result_as_artifact(
        self,
        run_id: str,
        label: str,
        task: str,
        result: str,
        art_type: str | None = None,
        internal: bool = False,
        source: str = "assistant_output",
        session_key: str | None = None,
    ) -> str | None:
        """Auto-save assistant output as an artifact. Returns artifact ID or None.

        ``art_type`` is supplied by the assistant registry
        (``Assistant.artifact_type``). Falls back to the legacy heuristic
        (coder → code, everything else → markdown) when callers don't
        pass it — e.g. the P2.5 output-cap path which doesn't know about
        the assistant object.
        """
        try:
            from flowly.artifacts.store import get_store
            store = get_store(self._state_dir)

            title = task[:100].strip()
            if len(task) > 100:
                title += "..."

            # Strip either 'builtin:' or 'assistant:' prefix so tags stay
            # readable. Legacy suffix heuristic fills in art_type when
            # the caller doesn't supply one.
            clean_label = label.split(":", 1)[1] if ":" in label else label
            if art_type is None:
                art_type = "code" if clean_label == "coder" else "markdown"

            # Always record provenance metadata so the store can tell
            # subagent_result from user-typed artifacts later (useful
            # for cleanup / filtering / TTL). Internal flag now only
            # hides the artifact from list views — it does not change
            # what we remember about where the artifact came from.
            metadata: dict[str, Any] = {
                "source": source,
                "run_id": run_id,
                "label": label,
                "original_chars": len(result),
            }
            if task:
                metadata["task"] = task[:400]
            tags = [f"assistant:{clean_label}"]
            if internal:
                metadata.update(internal_context_metadata(
                    source=source,
                    original_chars=len(result),
                    run_id=run_id,
                    label=label,
                    task=task,
                ))
                tags.extend(sorted(INTERNAL_CONTEXT_TAGS))

            artifact = store.create(
                type=art_type,
                title=title,
                content=result,
                metadata=metadata,
                tags=tags,
                session_key=session_key,
            )
            artifact_id = artifact["id"]
            if self._artifact_on_change and not internal:
                try:
                    await self._artifact_on_change("artifact.created", artifact)
                except Exception as exc:
                    logger.debug(
                        "[SubagentManager] artifact broadcast failed: {}", exc
                    )
            logger.info(
                f"[SubagentManager] [{run_id[:8]}] result saved as artifact {artifact_id}"
            )
            return artifact_id
        except Exception as e:
            logger.warning(f"[SubagentManager] [{run_id[:8]}] artifact save failed: {e}")
            return None

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
        if count:
            logger.info(f"[SubagentManager] shutdown: cancelled {count} running subagent(s)")
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
        """Cancel a running subagent by run_id (prefix match)."""
        # Find matching task
        matched_id = None
        for rid in self._running_tasks:
            if rid.startswith(run_id_prefix) or rid[:8] == run_id_prefix:
                matched_id = rid
                break

        if not matched_id:
            return json.dumps({"status": "error", "error": f"No running task matching '{run_id_prefix}'"})

        task = self._running_tasks.get(matched_id)
        if not task:
            return json.dumps({"status": "error", "error": "Task already completed"})

        # Cancel the asyncio task
        task.cancel()
        logger.info(f"[SubagentManager] [{matched_id[:8]}] cancelled by user")

        # Update registry
        self._registry.update(
            matched_id,
            ended_at=time.time(),
            outcome="cancelled",
            error="Cancelled by user",
        )

        # Announce cancellation
        await self._announce(matched_id, "Task was cancelled by user.", "cancelled")

        return json.dumps({
            "status": "cancelled",
            "run_id": matched_id[:8],
            "message": f"Task [{matched_id[:8]}] cancelled successfully.",
        })

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
