"""``codex_session`` — Flowly tool that delegates a turn to Codex.

The wrapping point between Flowly's agent loop and the Codex
runtime built in ``flowly/codex/``. The main agent (running on
Claude / OpenRouter / whatever the user picked) calls this tool
when it wants to hand off a coding-heavy turn to OpenAI's Codex CLI.

What the user sees
~~~~~~~~~~~~~~~~~~

Calling agent message ("I'll delegate this to Codex...") → tool
icon "🦊 codex_session" appears → Codex's item-stream renders
inline in the chat (assistant deltas, 🔧 exec / 🔧 apply_patch icons
with their results) → final assistant summary from Codex →
Flowly's main agent reads the result and replies to the user
with a wrap-up.

The user never realises they're talking to two different agents
because everything appears in one conversation thread.

What persists across calls
~~~~~~~~~~~~~~~~~~~~~~~~~~

The tool stores two pieces of state on the parent Flowly session's
metadata so subsequent calls resume the same Codex thread:

  * ``codex_thread_id`` — string. Set on first call, read on every
    subsequent call. Cleared by ``action="new"`` to force a fresh
    thread.
  * ``codex_reasoning_items`` — list of encrypted continuity blobs.
    Codex's reasoning state from previous turns; shipped back on
    each ``turn/start`` so the model remembers what it was thinking.

The CodexSession instance itself is held on the loop's
``_codex_sessions[session_key]`` dict so the subprocess stays
warm across Flowly turns. Closed + repopened lazily as needed.

When the tool retires a Codex session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The CodexSession returns ``should_retire=True`` when:
  * The subprocess crashed.
  * The hard turn deadline (600s) elapsed.
  * The post-tool wedge timeout (90s) elapsed.
  * OAuth tokens are bad.

The tool closes the session and DOES NOT clear the thread_id —
next call rebuilds a fresh session but resumes the same thread
(Codex keeps thread state on disk in ``~/.codex/threads/``, so a
crashed subprocess can pick up where it left off).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable

from flowly.agent.tools.base import Tool
from flowly.codex.session import (
    CodexSession,
    CodexSessionConfig,
    TurnResult,
)

logger = logging.getLogger(__name__)


# Type alias for the host-supplied lookup functions. The tool needs
# to read/write a Flowly session's metadata and stream Codex output
# back to the user-visible chat, but it shouldn't depend on the full
# AgentLoop machinery (keeps the tool unit-testable).
SessionAccessor = Callable[[str], dict[str, Any]]
"""``session_accessor(session_key) -> session.metadata dict``.

Returned dict is **live** — mutations on it persist on the session
(equivalent to ``session.metadata`` access in the loop).
"""

StreamCallbackResolver = Callable[[str], Callable[[str], Awaitable[None]] | None]
"""``stream_resolver(session_key) -> async (delta) -> None | None``.

Returns the active per-turn stream callback if the wrapping loop
is currently streaming this session, else ``None`` (no live
streaming sink). Letting the tool fetch this lazily means a single
``CodexSessionTool`` instance can serve any concurrent session.
"""

SessionStore = Callable[[str], "CodexSession | None"]
"""``session_store(session_key) -> CodexSession | None``.

Returns the warm CodexSession bound to ``session_key`` if one
exists, else ``None``. The tool reads existing sessions and asks
the host to install fresh ones via ``session_setter``.
"""

SessionSetter = Callable[[str, "CodexSession | None"], None]
"""``session_setter(session_key, session_or_none)``.

Installs a new CodexSession (or removes one when passed None) under
``session_key`` on the host's warm-session registry.
"""


_TOOL_DESCRIPTION = (
    "Delegate a HEAVY, MULTI-STEP coding task to OpenAI's Codex agent "
    "(GPT-5 specialist with its own sandboxed terminal + apply_patch). "
    "Codex spawns a subprocess and typically runs for 30 seconds to "
    "several minutes — it is NOT a substitute for your own exec / "
    "edit_file / read_file tools on simple work.\n"
    "\n"
    "ONLY USE WHEN ALL OF THESE HOLD:\n"
    "  1. The task is genuinely coding (writing, refactoring, "
    "debugging, testing real source code) — NOT 'run this one-liner', "
    "'show the answer', 'explain this concept'.\n"
    "  2. The work spans MULTIPLE FILES, or requires deep reasoning "
    "across a codebase, OR the user said the word 'codex' explicitly.\n"
    "  3. Doing it yourself would take 10+ tool calls (exec, read_file, "
    "edit_file chained together).\n"
    "\n"
    "DO NOT USE FOR (handle directly with your own tools instead):\n"
    "  - 'Print/show/calculate X' (fibonacci, primes, fizzbuzz, a "
    "regex, a SQL query, a one-line script) → use `exec`.\n"
    "  - 'Edit this one file' / 'add a comment' / 'rename a variable' "
    "→ use `edit_file` or `exec` with sed.\n"
    "  - 'Read this file' / 'what's in this directory' → use "
    "`read_file` / `exec ls`.\n"
    "  - 'Explain how X works' / 'what does this do' → answer "
    "directly, no tool needed.\n"
    "  - 'Run my tests / linter / build' → `exec` is enough.\n"
    "  - Anything non-coding (calendar, memory, web search, voice, "
    "chat, planning) → use the dedicated Flowly tool.\n"
    "\n"
    "If you are unsure whether a task is heavy enough, default to NOT "
    "calling Codex. The cost of an unnecessary 30s spawn + UI lag is "
    "much higher than the cost of doing the work yourself in 3 tool "
    "calls. Codex pays off only when the orchestration savings are "
    "real (5+ minute refactors, codebase-wide changes).\n"
    "\n"
    "GOOD EXAMPLES:\n"
    "  - 'Refactor the auth middleware to use the new token format "
    "across all 6 handlers.'\n"
    "  - 'There's a bug in the cron parser — find it and fix it.'\n"
    "  - 'Migrate this module from callbacks to async/await.'\n"
    "  - 'Add full test coverage to flowly/codex/'.\n"
    "  - 'Use codex to ...' (user explicit request).\n"
    "\n"
    "BAD EXAMPLES (do these yourself):\n"
    "  - 'Write a script that prints fibonacci numbers.'\n"
    "  - 'Show me the first 10 primes.'\n"
    "  - 'Add a docstring to this function.'\n"
    "  - 'What's the syntax for X in language Y?'\n"
    "\n"
    "REQUIREMENTS: The user must have the OpenAI Codex CLI installed "
    "(`npm i -g @openai/codex`) and be logged in (`codex login`). If "
    "either is missing the tool returns a clear error explaining the "
    "fix.\n"
    "\n"
    "CONTINUITY: Calls within the same Flowly session resume the same "
    "Codex thread automatically. Pass ``action='new'`` to explicitly "
    "abandon the current thread and start fresh (only when switching "
    "to a clearly different project)."
)


class CodexSessionTool(Tool):
    """Agent tool exposing :class:`CodexSession` via the standard Tool ABC.

    Construction wires in the host's session-state accessors; one
    instance serves every session_key the host runs.
    """

    def __init__(
        self,
        *,
        config: CodexSessionConfig,
        session_accessor: SessionAccessor,
        stream_resolver: StreamCallbackResolver,
        session_store_get: SessionStore,
        session_store_set: SessionSetter,
        active_session_key_getter: Callable[[], str] = lambda: "",
        approval_callback: Any = None,
    ) -> None:
        self._config = config
        self._session_accessor = session_accessor
        self._stream_resolver = stream_resolver
        self._session_store_get = session_store_get
        self._session_store_set = session_store_set
        self._active_session_key_getter = active_session_key_getter
        # Async callback that translates Codex's server-initiated
        # approval requests (commandExecution, fileChange) into
        # Flowly's ApprovalManager flow. ``None`` keeps the safe
        # default (auto-decline inside CodexSession). The loop wires
        # this in production; tests can leave it None.
        self._approval_callback = approval_callback

    # ── Tool ABC surface ─────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "codex_session"

    @property
    def description(self) -> str:
        return _TOOL_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The coding task or follow-up instruction to send "
                        "Codex. Pass the user's exact ask verbatim — do "
                        "NOT rephrase or summarise. Codex needs the full "
                        "context to plan its own approach."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["ask", "new"],
                    "default": "ask",
                    "description": (
                        "``ask`` (default): continue in the existing Codex "
                        "thread, or start one if none exists yet. "
                        "``new``: abandon the current thread and start a "
                        "fresh one — use only when the user explicitly "
                        "switches to a different project or context."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Optional working directory override for Codex's "
                        "tool execution. Must be an EXISTING directory; "
                        "prefer an absolute path (a leading ~ is expanded). "
                        "If omitted, Codex uses the cwd configured at "
                        "session-spawn time (typically the user's home or "
                        "the Flowly workspace)."
                    ),
                },
            },
            "required": ["task"],
        }

    # ── Execute ──────────────────────────────────────────────────────

    async def execute(self, **kwargs: Any) -> str:
        """Run one Codex turn and return a tool-result-friendly string.

        Return value contract:

          * On success → a JSON blob containing ``status``,
            ``thread_id``, ``final_text``, ``tool_iterations``, and a
            short ``summary``. The wrapping agent (Claude/etc.) reads
            this and decides how to surface it to the user.
          * On failure → a JSON blob with ``status: "error"`` plus
            ``error`` (one-sentence reason) and optionally ``hint``
            (actionable next step).

        Returning structured JSON rather than free-form text means the
        parent agent can detect failure deterministically — Flowly's
        rest of the tool surface follows the same convention.
        """
        # Resolve which Flowly session is firing this tool. The
        # active_session_key_getter is set by the loop on a per-turn
        # basis so concurrent sessions don't cross-contaminate.
        session_key = self._active_session_key_getter() or ""
        if not session_key:
            # Defensive — shouldn't happen in production but tests
            # can hit this if they exercise the tool in isolation.
            return _err(
                "codex_session called without an active Flowly session",
                hint="this is a Flowly internal error — please report it",
            )

        task = (kwargs.get("task") or "").strip()
        if not task:
            return _err(
                "codex_session requires a non-empty 'task' parameter",
                hint="pass the user's coding ask verbatim",
            )

        action = (kwargs.get("action") or "ask").lower()
        cwd_override = kwargs.get("cwd") or None
        if cwd_override:
            # Models routinely pass "~/project" — the OS never expands the
            # tilde, so the subprocess spawn fails with FileNotFoundError
            # that used to be misreported as "codex binary not found".
            cwd_override = os.path.expanduser(str(cwd_override))
            if not os.path.isdir(cwd_override):
                return _err(
                    f"working directory does not exist: {cwd_override}",
                    hint="pass an existing absolute path in 'cwd', or omit it",
                )

        # Fetch session metadata (live dict; mutations persist).
        metadata = self._session_accessor(session_key)

        # ── action="new": close existing session, clear stored IDs ──
        if action == "new":
            await self._reset_session(session_key, metadata)

        # ── Resolve or create the CodexSession ─────────────────────
        codex_session = self._session_store_get(session_key)
        if codex_session is None or codex_session.retired:
            codex_session = self._build_codex_session(
                metadata=metadata,
                cwd_override=cwd_override,
            )
            self._session_store_set(session_key, codex_session)

        # Stream callback comes from whichever channel is currently
        # rendering this session (WebSocket gateway, CLI, etc.).
        stream_cb = self._stream_resolver(session_key)

        # ── Run one Codex turn ─────────────────────────────────────
        try:
            turn: TurnResult = await codex_session.run_turn(
                task, stream_callback=stream_cb,
            )
        except Exception as exc:
            # An exception escaping run_turn is unexpected — the
            # session is supposed to catch and surface failures via
            # TurnResult.error. Treat this as a fatal session
            # crash, retire, and surface a clear error.
            logger.exception("[codex_session] turn raised unexpectedly")
            await self._reset_session(session_key, metadata)
            return _err(
                f"Codex session crashed: {exc}",
                hint="the session was retired; the next call will spawn a fresh one",
            )

        # ── Persist updated thread id / reasoning state ─────────────
        if turn.thread_id:
            metadata["codex_thread_id"] = turn.thread_id
        # Persisting the full reasoning_items list keeps continuity
        # across Flowly restarts (the wrapping loop saves session
        # metadata to disk).
        if codex_session.reasoning_items:
            metadata["codex_reasoning_items"] = list(
                codex_session.reasoning_items
            )

        # Inject projected messages directly into the live Flowly
        # session history. The wrapping loop's session is a live
        # object the loop reads on the next turn; appending here
        # makes Codex's tool_calls + tool_results visible in the
        # final assistant response and in the persisted session log.
        self._inject_messages_into_session(session_key, turn.messages)

        # ── Retire on terminal failure ──────────────────────────────
        if turn.should_retire:
            await self._reset_session(session_key, metadata, keep_thread=True)

        # ── Build the tool-result envelope ──────────────────────────
        if turn.error is not None:
            # Failure path. The session may have streamed partial
            # content already (final_text non-empty); we surface
            # what we got so the parent can salvage information.
            return _err(
                turn.error,
                final_text=turn.final_text or None,
                thread_id=turn.thread_id or None,
                hint=_actionable_hint(turn.error),
            )

        # Success path.
        return _ok(
            thread_id=turn.thread_id,
            final_text=turn.final_text,
            tool_iterations=turn.tool_iterations,
            summary=_short_summary(turn),
        )

    # ── Internal helpers ─────────────────────────────────────────────

    def _build_codex_session(
        self,
        *,
        metadata: dict[str, Any],
        cwd_override: str | None,
    ) -> CodexSession:
        """Construct a fresh CodexSession, seeded from metadata.

        Reads any persisted ``codex_thread_id`` and
        ``codex_reasoning_items`` so a session resumed from disk
        picks up where it left off.
        """
        config = self._config
        if cwd_override:
            # Build a shallow-modified copy so per-call cwd overrides
            # don't leak into the session-wide config.
            config = CodexSessionConfig(
                codex_bin=self._config.codex_bin,
                codex_home=self._config.codex_home,
                cwd=cwd_override,
                extra_env=dict(self._config.extra_env),
                turn_timeout_s=self._config.turn_timeout_s,
                post_tool_quiet_timeout_s=self._config.post_tool_quiet_timeout_s,
                client_name=self._config.client_name,
                client_version=self._config.client_version,
                approval_policy=self._config.approval_policy,
                sandbox=self._config.sandbox,
            )

        session = CodexSession(
            config=config,
            approval_callback=self._approval_callback,
        )

        stored_thread_id = metadata.get("codex_thread_id")
        if isinstance(stored_thread_id, str) and stored_thread_id:
            session.set_thread_id(stored_thread_id)

        stored_reasoning = metadata.get("codex_reasoning_items")
        if isinstance(stored_reasoning, list) and stored_reasoning:
            session.set_initial_reasoning_items(stored_reasoning)

        return session

    async def _reset_session(
        self,
        session_key: str,
        metadata: dict[str, Any],
        *,
        keep_thread: bool = False,
    ) -> None:
        """Close the live session and clear stored state.

        ``keep_thread=True`` preserves the thread_id on metadata so
        the NEXT call spawns a fresh subprocess but resumes the
        same Codex thread (Codex keeps thread state on disk, so a
        wedged subprocess can be replaced without losing context).
        ``keep_thread=False`` clears everything — used by
        ``action="new"``.
        """
        existing = self._session_store_get(session_key)
        if existing is not None:
            try:
                await existing.close()
            except Exception:
                logger.exception(
                    "[codex_session] error closing retired session",
                )
            self._session_store_set(session_key, None)

        if not keep_thread:
            metadata.pop("codex_thread_id", None)
            metadata.pop("codex_reasoning_items", None)

    def _inject_messages_into_session(
        self, session_key: str, messages: list[dict[str, Any]],
    ) -> None:
        """Persist Codex's projected messages into the Flowly session.

        The wrapping loop's session is a live object; appending to
        its history here means the renderer (desktop / iOS) sees the
        tool_call / tool_result pairs in the conversation flow, and
        the persisted session log records them for cross-session
        recall.

        The implementation lives behind the metadata accessor —
        ``metadata`` is the same live dict that ``session.metadata``
        exposes in the loop, and the wrapping loop drains a
        ``codex_pending_messages`` queue on every turn boundary.
        Using a queue (rather than a direct ``session.add_message``
        call) avoids coupling the tool to ``SessionManager``'s
        internal API.
        """
        metadata = self._session_accessor(session_key)
        queue = metadata.setdefault("codex_pending_messages", [])
        # Defensive: previous turn's queue might not have been drained
        # by the loop (interrupted turn). We append, letting the loop
        # drain everything in order.
        if isinstance(queue, list):
            queue.extend(messages)


# ---------------------------------------------------------------------------
# Result envelope helpers
# ---------------------------------------------------------------------------


def _ok(**fields: Any) -> str:
    """Build a successful tool-result envelope (JSON string)."""
    payload: dict[str, Any] = {"status": "ok"}
    for k, v in fields.items():
        if v is None or v == "":
            continue
        payload[k] = v
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str, **fields: Any) -> str:
    """Build an error tool-result envelope (JSON string).

    Always includes ``status: "error"`` and a ``error`` field so
    the parent agent can branch deterministically. Optional fields
    (``hint``, ``thread_id``, ``final_text``) are included only when
    set, keeping the envelope compact.
    """
    payload: dict[str, Any] = {"status": "error", "error": message}
    for k, v in fields.items():
        if v is None or v == "":
            continue
        payload[k] = v
    return json.dumps(payload, ensure_ascii=False)


def _short_summary(turn: TurnResult) -> str:
    """One-sentence overview of what happened in a turn.

    The parent agent gets this in addition to ``final_text`` so it
    can decide how to introduce Codex's output to the user without
    re-reading the whole message stream.
    """
    parts: list[str] = []
    if turn.tool_iterations:
        parts.append(
            f"{turn.tool_iterations} tool "
            f"{'iteration' if turn.tool_iterations == 1 else 'iterations'}"
        )
    if turn.interrupted:
        parts.append("interrupted")
    if turn.final_text:
        # Truncate aggressively — this is a summary, not the answer.
        head = turn.final_text.replace("\n", " ").strip()
        if len(head) > 140:
            head = head[:137] + "..."
        if head:
            parts.append(f"final: {head}")
    return "; ".join(parts) if parts else "(no output)"


def _actionable_hint(error: str) -> str | None:
    """Map common Codex errors to a one-line fix hint.

    These hints surface in the tool result envelope so the parent
    agent can repeat them verbatim to the user instead of having to
    guess what the user should do next.
    """
    lowered = error.lower()
    if "codex binary" in lowered or "not found" in lowered:
        return (
            "Install the Codex CLI: `npm i -g @openai/codex`, then "
            "log in with `codex login`."
        )
    if "login" in lowered or "expired" in lowered or "oauth" in lowered:
        return "Run `codex login` in the terminal to refresh your session."
    if "deadline" in lowered or "timeout" in lowered:
        return (
            "Codex's turn timed out — break the task into smaller "
            "pieces or run it directly in the terminal."
        )
    return None


__all__ = [
    "CodexSessionTool",
    "SessionAccessor",
    "StreamCallbackResolver",
    "SessionStore",
    "SessionSetter",
]
