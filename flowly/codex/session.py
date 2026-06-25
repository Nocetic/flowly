"""Per-Flowly-session Codex thread lifecycle.

One :class:`CodexSession` instance owns:

  * One :class:`CodexAppServerClient` subprocess (lazy spawn).
  * One Codex thread id, persisted on the parent Flowly session's
    metadata under ``codex_thread_id``.
  * The encrypted-reasoning continuity blobs from previous turns.

The wrapping ``codex_session`` tool (Phase C) calls
:meth:`run_turn` once per user turn. The session takes care of:

  * Spawning the subprocess on first call (lazy, so a Flowly user
    who never uses Codex never pays for the binary).
  * ``thread/start`` on first turn, ``turn/start`` on subsequent
    turns (resuming the existing thread).
  * Streaming Codex's item events through a projector into the
    user-visible chat surface.
  * Owning a Codex's-asking-us-something approval loop so the
    user actually gets prompted (auto-decline for now, real
    approval flow lands in Phase C).
  * Detecting wedged turns (post-tool silence > 90s, total turn
    > 600s) and retiring the session for the next caller.
  * Classifying OAuth refresh failures so the wrapping tool can
    surface a "login expired" message instead of a generic crash.
  * Carrying encrypted reasoning items forward so multi-turn Codex
    threads keep the model's thinking context.

What this module is NOT
~~~~~~~~~~~~~~~~~~~~~~~

* Not the Flowly tool — that lives in
  ``flowly/agent/tools/codex_session.py`` (Phase C).
* Not a parallel agent loop — the session is a thin orchestrator
  over the transport; nothing here decides what to ask Codex.
* Not multi-thread. One session = one Codex thread. Concurrent
  threads need a new session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from flowly.codex.app_server import (
    CodexAppServerClient,
    CodexProtocolError,
    CodexRPCError,
    CodexSpawnError,
)
from flowly.codex.projector import (
    CodexEventProjector,
    StreamCallback,
    TurnProjection,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

# Hard ceiling on wall-clock time for one Codex turn. Long agentic
# flows (refactor a whole package, search a large repo) routinely
# spend 5+ minutes; 600s gives them room without letting a genuine
# hang block the agent forever. Aligns with Codex's own default
# turn timeout.
DEFAULT_TURN_TIMEOUT_S: float = 600.0

# Wedge-detection threshold. If the turn has emitted at least one
# tool-completion notification but then goes silent for this long
# (no new notifications, no turn/completed), we assume Codex is
# wedged and tear down. 90s mirrors the upstream Codex post-tool
# watchdog.
POST_TOOL_QUIET_TIMEOUT_S: float = 90.0

# How long to wait for a single notification before checking the
# subprocess health, the turn deadline, and the wedge clock. Short
# enough to react promptly; long enough to not burn CPU spinning
# on an idle queue.
NOTIFICATION_POLL_TIMEOUT_S: float = 0.25

# JSON-RPC requests to the Codex server have per-method timeouts
# tuned to expected response times. thread/start initialises
# subprocess state; turn/start kicks off the agentic loop (don't
# put a tight timeout on it — the loop itself takes minutes);
# turn/interrupt is best-effort and should return immediately.
THREAD_START_TIMEOUT_S: float = 15.0
TURN_INTERRUPT_TIMEOUT_S: float = 5.0

# OAuth refresh hint keywords scanned in Codex's stderr when a
# request fails. Triggers a "your Codex login is expired" message
# instead of a generic transport error so the user knows how to fix it.
_OAUTH_FAILURE_HINTS: tuple[str, ...] = (
    "invalid_grant",
    "refresh token",
    "token has expired",
    "token expired",
    "not authenticated",
    "unauthorized",
    "401 unauthorized",
    "re-authenticate",
    "please log in",
    "please re-login",
    "oauth",
)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    """Outcome of one :meth:`CodexSession.run_turn` call.

    Returned to the wrapping ``codex_session`` tool, which turns it
    into a tool_result the parent Flowly agent can summarise to the
    user.

    Fields:
        thread_id: The Codex thread we ran in. First-turn callers
            store this on the Flowly session metadata so subsequent
            turns resume the same thread.
        final_text: Last assistant-message text from the projection.
            What the parent agent should treat as Codex's "answer".
        messages: Projected Flowly messages to append to the session
            (assistant texts, tool_call / tool_result pairs).
        reasoning_items: Encrypted-continuity blobs to ship back on
            the next turn. Stored under session metadata.
        tool_iterations: Number of mutating items in this turn —
            drives the skill-nudge counter so Codex turns count the
            same way native Flowly turns do.
        error: When set, the turn failed mid-flight. ``final_text``
            and ``messages`` may still carry partial output collected
            before the failure.
        should_retire: True when the session is too damaged to reuse
            (subprocess crashed, OAuth expired, hard deadline hit).
            The next ``run_turn`` caller must close this session and
            spawn a fresh one.
        interrupted: True when the turn was cut short by the wedge
            watchdog (we issued a turn/interrupt). ``error`` is set
            in tandem; ``should_retire`` may be True or False
            depending on whether the subprocess responded to the
            interrupt cleanly.
    """

    thread_id: str = ""
    final_text: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    reasoning_items: list[dict[str, Any]] = field(default_factory=list)
    tool_iterations: int = 0
    error: str | None = None
    should_retire: bool = False
    interrupted: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def classify_oauth_failure(*parts: str) -> str | None:
    """Scan candidate strings for OAuth refresh failure hints.

    Returns a user-actionable hint string when any of the known
    OAuth-failure substrings appears; ``None`` otherwise.

    Used to upgrade a generic "request failed" surface into an
    actionable "your Codex login is expired, run ``codex login``"
    message. We check Codex's stderr tail + the JSON-RPC error
    payload (both stringified, lowercased) because the actual
    failure phrasing varies between Codex versions and auth
    flows (API-key 401 vs OAuth refresh failure vs token revoked).
    """
    haystack = " ".join(str(p) for p in parts if p).lower()
    if not haystack:
        return None
    for needle in _OAUTH_FAILURE_HINTS:
        if needle in haystack:
            return (
                "Codex authentication failed — your ChatGPT/Codex "
                "login looks expired or invalid. Run `codex login` "
                "to refresh, then try again."
            )
    return None


def _is_thread_missing_error(exc: CodexRPCError) -> bool:
    """True when a turn/start failure means the resumed thread is gone.

    Codex reports this as ``thread not found: <id>`` (and occasionally
    ``unknown thread`` / ``no such thread`` across versions). Used to decide
    whether to recover by starting a fresh thread instead of surfacing the
    error to the user.
    """
    blob = f"{getattr(exc, 'message', '')} {getattr(exc, 'data', '')}".lower()
    return any(
        s in blob for s in ("thread not found", "unknown thread", "no such thread")
    )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class CodexSessionConfig:
    """Static configuration for a Codex session.

    Pulled from the Flowly tool config (``tools.codex``) when the
    session is created. Field choices are minimal on purpose — every
    knob here is one more thing for an operator to mis-set, so we
    expose only the knobs that actually matter in practice.
    """

    codex_bin: str = "codex"
    codex_home: str | None = None
    cwd: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    turn_timeout_s: float = DEFAULT_TURN_TIMEOUT_S
    post_tool_quiet_timeout_s: float = POST_TOOL_QUIET_TIMEOUT_S
    client_name: str = "flowly"
    client_version: str = "1.9.9"
    # Default approval policy used at thread/start. The session
    # auto-declines server-initiated approval prompts by default;
    # see ``approval_callback`` for hooking a real flow.
    approval_policy: str = "on-request"
    # Sandbox level: "read-only" | "workspace-write" | "full-access".
    # Defaults to workspace-write (matches Codex's own default) so
    # exec / apply_patch work in the project but can't reach
    # /etc or other system paths.
    sandbox: str = "workspace-write"


ApprovalCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class CodexSession:
    """Single Codex thread bound to a single Flowly session.

    Construction is async (``spawn()`` is a coroutine), and the
    instance is meant to live for the duration of a Flowly session.
    The wrapping tool calls :meth:`run_turn` for each user message;
    :meth:`close` is called when the Flowly session ends or the
    session asks itself to be retired.
    """

    def __init__(
        self,
        *,
        config: CodexSessionConfig,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        # Constructed empty; the real subprocess + state live behind
        # :meth:`ensure_client`. This lets the wrapping tool decide
        # whether the user-visible "starting Codex…" message should
        # come before or after the spawn cost.
        self._config = config
        self._approval_callback = approval_callback
        self._client: CodexAppServerClient | None = None
        # Codex thread id; persisted on the Flowly session metadata
        # so a stale session can resume the same thread across
        # Flowly restarts.
        self._thread_id: str | None = None
        # Codex turn id for the IN-FLIGHT turn (or the most recently
        # completed one). Used to scope turn/interrupt so a stale
        # interrupt doesn't tear down a turn that already replaced
        # the one we meant to kill.
        self._current_turn_id: str | None = None
        # Encrypted reasoning blobs collected from earlier turns;
        # shipped back on the next turn/start to preserve thinking
        # state. Caller can hand in initial state via
        # :meth:`set_initial_reasoning_items` when resuming a
        # session from disk.
        self._reasoning_items: list[dict[str, Any]] = []
        # If a turn ever sets this, every subsequent call returns
        # immediately with should_retire=True so the parent rebuilds
        # the session cleanly.
        self._retired: bool = False

    # ── Resume support ──────────────────────────────────────────────

    def set_thread_id(self, thread_id: str | None) -> None:
        """Adopt a thread id loaded from Flowly session metadata.

        Called by the tool before the first ``run_turn`` when a
        Flowly session is being resumed across restarts. ``None``
        clears the id and forces a fresh thread/start on next run.
        """
        self._thread_id = thread_id

    def set_initial_reasoning_items(
        self, items: list[dict[str, Any]],
    ) -> None:
        """Seed the reasoning continuity buffer from persisted state.

        Symmetric to ``set_thread_id``; usually called together.
        Items should be in the same shape :class:`TurnProjection`
        emits them.
        """
        self._reasoning_items = list(items)

    # ── Resource lifecycle ─────────────────────────────────────────

    async def ensure_client(self) -> CodexAppServerClient:
        """Spawn the subprocess on first use, return the cached client.

        Lazy spawn is intentional: a Flowly user who never invokes
        the Codex tool never pays the ~1s startup cost. Spawn errors
        propagate (CodexSpawnError) so the tool can surface a clean
        "Codex CLI not installed" message instead of pretending the
        session works.
        """
        if self._client is not None:
            return self._client
        if self._retired:
            raise RuntimeError(
                "this CodexSession was retired; build a new one"
            )

        self._client = await CodexAppServerClient.spawn(
            codex_bin=self._config.codex_bin,
            codex_home=self._config.codex_home,
            cwd=self._config.cwd,
            env=self._config.extra_env or None,
            client_name=self._config.client_name,
            client_version=self._config.client_version,
        )
        return self._client

    async def close(self) -> None:
        """Tear down the subprocess if it was spawned.

        Idempotent. Safe to call multiple times. Always marks the
        session retired so any concurrent caller sees a clean
        failure rather than racing for a half-closed client.
        """
        self._retired = True
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.exception("[codex.session] error closing client")

    # ── Read-only state ─────────────────────────────────────────────

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def reasoning_items(self) -> list[dict[str, Any]]:
        return list(self._reasoning_items)

    @property
    def retired(self) -> bool:
        return self._retired

    # ── Turn execution ──────────────────────────────────────────────

    async def run_turn(
        self,
        user_input: str,
        *,
        stream_callback: StreamCallback | None = None,
    ) -> TurnResult:
        """Run one Codex turn with *user_input* and return the projection.

        Lifecycle:

          1. Ensure subprocess + client are live.
          2. If we don't have a thread id, ``thread/start``;
             otherwise ``turn/start``.
          3. Poll notifications + server-requests in a watchdog loop
             until ``turn/completed`` or a failure condition fires.
          4. Project the events and return the result.

        The watchdog tracks two clocks:
          * Wall-clock since the turn started → hard timeout
            (:data:`DEFAULT_TURN_TIMEOUT_S`).
          * Wall-clock since the last tool item completion →
            post-tool wedge timeout
            (:data:`POST_TOOL_QUIET_TIMEOUT_S`). Only armed once at
            least one tool iteration has been observed; pure
            text-only turns aren't subject to it.

        Either timeout firing tries a ``turn/interrupt`` and returns
        ``should_retire=True``.
        """
        result = TurnResult()

        try:
            client = await self.ensure_client()
        except CodexSpawnError as exc:
            result.error = str(exc)
            result.should_retire = True
            return result

        # Start (or continue) the thread.
        projector = CodexEventProjector(stream_callback=stream_callback)
        try:
            await self._begin_turn(client, user_input)
        except CodexRPCError as exc:
            return self._handle_rpc_error(client, exc, result)
        except CodexProtocolError as exc:
            result.error = f"Codex protocol error: {exc}"
            result.should_retire = True
            return result
        except asyncio.TimeoutError:
            result.error = (
                f"timed out starting Codex turn "
                f"({THREAD_START_TIMEOUT_S}s)"
            )
            result.should_retire = True
            return result

        # Drive the notification loop.
        try:
            await self._drain_until_complete(client, projector, result)
        except CodexProtocolError as exc:
            result.error = f"Codex protocol error during turn: {exc}"
            result.should_retire = True
            # Fall through to projection — there may still be partial
            # state worth surfacing to the user.

        # Always finalize so even an aborted turn produces partial
        # messages instead of silently losing what we got.
        projection = projector.finalize_turn()
        self._merge_projection(projection, result)
        return result

    # ── Turn-start ──────────────────────────────────────────────────

    async def _begin_turn(
        self, client: CodexAppServerClient, user_input: str,
    ) -> None:
        """Send the right thread/start or turn/start request.

        First turn → ``thread/start`` (creates a new Codex thread).
        Subsequent turns → ``turn/start`` (resumes the existing one).

        On first turn we also pass the reasoning_items seed if the
        session was resumed from disk. On subsequent turns we ship
        accumulated reasoning items so Codex can continue the
        thought thread.
        """
        input_items = self._build_input_items(user_input)

        # Codex's protocol splits thread creation and turn execution
        # into two separate RPCs (confirmed wire-level against codex
        # 0.125.0):
        #
        #   * ``thread/start`` — creates the thread, returns its id.
        #     Does NOT accept ``input``; supplying one causes Codex to
        #     create the thread but silently never start a turn,
        #     producing the "thread/start succeeds, no item/*
        #     notifications ever arrive" failure mode.
        #
        #   * ``turn/start`` — actually drives the model. The
        #     ``input`` list (reasoning continuity + user text) goes
        #     here. Returns turn metadata including the new turn id.
        #
        # First call in a session does both back-to-back; subsequent
        # calls only need turn/start because the thread is already
        # alive.
        # Whether we're resuming a stored thread (vs creating one now). A
        # resumed thread can be stale: codex app-server threads live with the
        # subprocess, so a gateway restart / session retirement / a thread
        # created by a different codex process leaves the persisted id
        # dangling, and turn/start then fails "thread not found". When that
        # happens we drop the dead thread and start fresh instead of erroring.
        was_resuming = self._thread_id is not None

        if self._thread_id is None:
            await self._do_thread_start(client)

        # Always send turn/start after the thread exists — this is
        # the call that actually kicks off the model and the
        # item-event stream we consume in the drain loop.
        try:
            turn_result = await self._do_turn_start(client, input_items)
        except CodexRPCError as exc:
            if was_resuming and _is_thread_missing_error(exc):
                logger.info(
                    "[codex.session] resumed thread %s is gone; starting fresh",
                    self._thread_id,
                )
                # The continuity reasoning blobs belong to the dead thread —
                # replaying them into a new thread is meaningless, so drop them.
                self._thread_id = None
                self._reasoning_items = []
                input_items = self._build_input_items(user_input)
                await self._do_thread_start(client)
                turn_result = await self._do_turn_start(client, input_items)
            else:
                raise
        # Capture the new turn's id so interrupts target the right
        # turn. Codex's response shape: ``{"turn": {"id": "..."}}``.
        self._current_turn_id = self._extract_turn_id(turn_result)

    async def _do_thread_start(self, client: CodexAppServerClient) -> None:
        """Create a fresh codex thread and store its id.

        Conservative params — only ``cwd`` when set. Permissions / sandbox /
        approval policy are configured in ``~/.codex/config.toml`` rather than
        sent here (the experimentalApi-gated fields are rejected otherwise).
        """
        start_params: dict[str, Any] = {}
        if self._config.cwd:
            start_params["cwd"] = self._config.cwd
        result = await client.request(
            "thread/start", start_params, timeout=THREAD_START_TIMEOUT_S,
        )
        thread_id = self._extract_thread_id(result)
        if not thread_id:
            raise CodexProtocolError(
                f"thread/start returned no thread id: {result!r}"
            )
        self._thread_id = thread_id

    async def _do_turn_start(
        self, client: CodexAppServerClient, input_items: list[dict[str, Any]],
    ) -> Any:
        return await client.request(
            "turn/start",
            {"threadId": self._thread_id, "input": input_items},
            timeout=THREAD_START_TIMEOUT_S,
        )

    @staticmethod
    def _extract_turn_id(result: Any) -> str | None:
        """Pull the turn id out of a thread/start or turn/start response.

        Codex 2026-05+ shape:
          * ``{"thread": {...}, "turn": {"id": "..."}}`` (thread/start
            also creates the first turn)
          * ``{"turn": {"id": "..."}}`` (turn/start)

        Older builds returned ``{"turnId": "..."}``. ``None`` when no
        recognised location matches — interrupt will fall back to
        omitting turnId, which Codex tolerates (best-effort).
        """
        if not isinstance(result, dict):
            return None
        turn_obj = result.get("turn")
        if isinstance(turn_obj, dict):
            v = turn_obj.get("id") or turn_obj.get("turnId")
            if isinstance(v, str) and v:
                return v
        for key in ("turnId", "turn_id"):
            v = result.get(key)
            if isinstance(v, str) and v:
                return v
        return None

    @staticmethod
    def _extract_thread_id(result: Any) -> str | None:
        """Pull the thread id out of a ``thread/start`` response.

        Codex's response schema has moved around between versions:

          * 2026-05+: ``{"thread": {"id": "...", "forkedFromId": null,
            "preview": ""}}``
          * Older (some 2026-04 builds): ``{"threadId": "..."}``
          * Even older: ``{"id": "..."}``

        We check every known location in priority order. Returns the
        first non-empty string id, or ``None`` if none match — the
        caller turns that into a CodexProtocolError carrying the raw
        result for diagnostics.
        """
        if not isinstance(result, dict):
            return None
        # 2026-05+ nested shape: result.thread.id
        thread_obj = result.get("thread")
        if isinstance(thread_obj, dict):
            nested = thread_obj.get("id") or thread_obj.get("threadId")
            if isinstance(nested, str) and nested:
                return nested
        # Flat shape: result.threadId / result.thread_id / result.id
        for key in ("threadId", "thread_id", "id"):
            v = result.get(key)
            if isinstance(v, str) and v:
                return v
        return None

    def _build_input_items(self, user_input: str) -> list[dict[str, Any]]:
        """Compose the ``input`` list for thread/start or turn/start.

        Codex's input format is an array of typed items:
          - ``{type: "input_text", text: "..."}`` for the user prompt
          - ``{type: "reasoning", encryptedContent: "..."}`` for
            continuity replay (one entry per saved blob)

        Replaying reasoning is what lets the model "remember" what
        it was thinking on previous turns. We always include the
        accumulated buffer; Codex tolerates an empty list on a
        first turn.
        """
        items: list[dict[str, Any]] = []
        # Reasoning continuity first — Codex reads them in order to
        # rebuild thinking state before processing the new input.
        for r in self._reasoning_items:
            blob = r.get("encryptedContent") or r.get("encrypted_content")
            if not blob:
                continue
            items.append({
                "type": "reasoning",
                "encryptedContent": blob,
            })
        # User input last. Codex 2026-05+ expects ``type: "text"``
        # for the user prompt — verified against the reference
        # client implementation. Earlier docs sometimes mentioned
        # ``input_text`` but the live binary rejects it (silent: the
        # turn never produces item/* notifications, leading to a
        # hung wait that only the watchdog can interrupt).
        items.append({"type": "text", "text": user_input})
        return items

    # ── Notification loop ───────────────────────────────────────────

    async def _drain_until_complete(
        self,
        client: CodexAppServerClient,
        projector: CodexEventProjector,
        result: TurnResult,
    ) -> None:
        """Poll notifications + server-requests until turn ends.

        Three conditions can end the loop:
          * Codex sends ``turn/completed`` → normal end.
          * Hard turn deadline elapses → interrupt + retire.
          * Post-tool wedge timeout elapses → interrupt + retire.
          * Subprocess dies → fail + retire.

        On normal end the loop returns cleanly and the projection
        is finalised by the caller.
        """
        started_at = time.monotonic()
        last_tool_completion_at: float | None = None
        turn_done = False

        while not turn_done:
            now = time.monotonic()

            # Hard turn deadline.
            if now - started_at > self._config.turn_timeout_s:
                await self._interrupt(client, reason="turn timeout")
                result.error = (
                    f"Codex turn exceeded {self._config.turn_timeout_s:.0f}s "
                    "deadline; interrupted."
                )
                result.interrupted = True
                result.should_retire = True
                return

            # Post-tool wedge: only armed after we've seen at least
            # one tool iteration. Avoids killing pure-text turns that
            # legitimately take their time.
            if (
                last_tool_completion_at is not None
                and (now - last_tool_completion_at)
                    > self._config.post_tool_quiet_timeout_s
            ):
                await self._interrupt(client, reason="post-tool wedge")
                result.error = (
                    f"Codex went silent for "
                    f"{self._config.post_tool_quiet_timeout_s:.0f}s after a "
                    "tool completion; interrupted."
                )
                result.interrupted = True
                result.should_retire = True
                return

            # Subprocess health check — catches an unexpected exit.
            if not client.is_alive():
                stderr_tail = "\n".join(client.stderr_tail(20))
                hint = classify_oauth_failure(stderr_tail)
                if hint:
                    result.error = hint
                else:
                    result.error = (
                        f"codex app-server subprocess exited "
                        f"(code={client.exit_code}). stderr tail:\n"
                        f"{stderr_tail or '(empty)'}"
                    )
                result.should_retire = True
                return

            # Drain any pending server-initiated requests first —
            # leaving an approval prompt unanswered would block the
            # Codex side from making progress.
            sreq = await client.take_server_request(timeout=0)
            if sreq is not None:
                await self._handle_server_request(client, sreq)
                continue

            # Drain one notification (with a short poll-timeout so
            # we revisit the watchdog clocks regularly).
            note = await client.take_notification(
                timeout=NOTIFICATION_POLL_TIMEOUT_S,
            )
            if note is None:
                continue

            method = note.get("method", "")

            if method == "turn/completed":
                turn_done = True
                # Don't return yet — drain any straggler
                # notifications that Codex queued before turn/completed
                # so the projection is complete.
                await self._drain_remaining(client, projector, budget=0.5)
                return

            # Track tool-completion timing for wedge detection.
            if method == "item/completed":
                last_tool_completion_at = now

            try:
                await projector.handle_notification(note)
            except Exception:
                logger.exception(
                    "[codex.session] projector raised on notification %s",
                    method,
                )

    async def _drain_remaining(
        self,
        client: CodexAppServerClient,
        projector: CodexEventProjector,
        *,
        budget: float,
    ) -> None:
        """Drain any straggler notifications after turn/completed.

        Codex sometimes queues a last `item/completed` (or similar)
        just before `turn/completed`; with the polling loop's
        ordering they can arrive after we've seen the terminator. We
        drain anything still in the queue with a small budget so
        the projection captures it.
        """
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            note = await client.take_notification(timeout=0)
            if note is None:
                return
            try:
                await projector.handle_notification(note)
            except Exception:
                logger.exception("[codex.session] projector raised on tail drain")

    # ── Server-initiated requests (approval flow) ───────────────────

    async def _handle_server_request(
        self, client: CodexAppServerClient, req: dict[str, Any],
    ) -> None:
        """Reply to a server-initiated request.

        Codex uses these for approval prompts and elicitations. The
        default policy is auto-decline (safe default — destructive
        actions need the user's eyes). If an ``approval_callback``
        was supplied, route the request through it so the wrapping
        tool can prompt the user via Flowly's standard approval
        infrastructure.
        """
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if req_id is None:
            # Malformed — nothing we can reply to. Codex will time
            # out on its end eventually.
            logger.warning(
                "[codex.session] server-initiated request without id: %r",
                req,
            )
            return

        # MCP elicitation — Codex's MCP layer asks the user to confirm a
        # tool call (or supply structured input) on behalf of an MCP
        # server. This uses a DIFFERENT response contract than approval
        # prompts: ``{"action": "accept"|"decline", "content": ..., "_meta": ...}``,
        # NOT ``{"decision": ...}``. So it must be handled BEFORE the
        # approval_callback path (which speaks the decision dialect).
        #
        # For our OWN flowly-tools callback we auto-accept: the user
        # already opted in by enabling the runtime, and the callback only
        # exposes tools Codex's built-in shell could already reach. For
        # any other MCP server we decline so the user opts in explicitly
        # via Codex's own flow.
        if method == "mcpServer/elicitation/request":
            server_name = params.get("serverName") or params.get("server") or ""
            if server_name == "flowly-tools":
                await client.respond(
                    req_id, {"action": "accept", "content": None, "_meta": None},
                )
            else:
                await client.respond(
                    req_id, {"action": "decline", "content": None, "_meta": None},
                )
            return

        if self._approval_callback is not None:
            try:
                response = await self._approval_callback({
                    "method": method,
                    "params": params,
                })
                await client.respond(req_id, response)
                return
            except Exception:
                logger.exception(
                    "[codex.session] approval_callback raised; "
                    "auto-declining request id=%s",
                    req_id,
                )

        # Default behaviour: decline. Approval requests follow a
        # specific contract — they expect a ``{"decision": "..."}``
        # result, NOT a JSON-RPC error. Sending respond_error to
        # an approval request leaves Codex in a confused state
        # where the turn neither continues nor terminates.
        # Non-approval server requests (elicitations, MCP sampling)
        # are rare; we send a generic decline result with a
        # diagnostic message so Codex can move on.
        approval_methods = (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
        )
        if method in approval_methods:
            await client.respond(req_id, {"decision": "decline"})
        else:
            # Best-effort generic decline for unknown server-request
            # types. We respond with a result rather than an error
            # so Codex doesn't treat it as a transport failure.
            await client.respond(req_id, {
                "decision": "decline",
                "reason": (
                    "Flowly's Codex bridge declines server-initiated "
                    "requests by default."
                ),
            })

    # ── Interruption ─────────────────────────────────────────────────

    async def _interrupt(
        self, client: CodexAppServerClient, *, reason: str,
    ) -> None:
        """Best-effort turn/interrupt.

        Called when the wedge watchdog or hard-deadline fires.
        Failure to interrupt is non-fatal — we're retiring the
        session anyway, so Codex's own end will clean up when the
        subprocess is closed.

        Sends ``turnId`` when known (Codex correlates interrupts
        against the specific turn) and falls back to thread-scoped
        cancellation when not.
        """
        if not self._thread_id:
            return
        params: dict[str, Any] = {"threadId": self._thread_id}
        if self._current_turn_id:
            params["turnId"] = self._current_turn_id
        # ``reason`` is informational — Codex logs it but doesn't act
        # on it. We send it anyway so the upstream stderr makes the
        # cause clear in postmortems.
        params["reason"] = reason
        try:
            await client.request(
                "turn/interrupt",
                params,
                timeout=TURN_INTERRUPT_TIMEOUT_S,
            )
        except Exception:
            logger.exception(
                "[codex.session] turn/interrupt failed (non-fatal)",
            )

    # ── Error projection ─────────────────────────────────────────────

    def _handle_rpc_error(
        self,
        client: CodexAppServerClient,
        exc: CodexRPCError,
        result: TurnResult,
    ) -> TurnResult:
        """Translate a Codex JSON-RPC error into a friendly TurnResult.

        Most importantly, detects OAuth refresh failures so the
        wrapping tool can show "your Codex login is expired" instead
        of a generic transport error. Sets ``should_retire=True``
        because once the auth is bad the session can't recover.
        """
        stderr_tail = "\n".join(client.stderr_tail(20))
        hint = classify_oauth_failure(
            exc.message, str(exc.data), stderr_tail,
        )
        if hint:
            result.error = hint
            result.should_retire = True
        else:
            result.error = f"Codex error: {exc.message}"
            # Don't retire on every error — some are recoverable
            # (e.g. a malformed turn input). But CodexRPCErrors
            # are uncommon enough in practice that we err on the
            # safe side and retire to avoid leaving the session
            # in an undefined state.
            result.should_retire = True
        return result

    # ── Projection merge ─────────────────────────────────────────────

    def _merge_projection(
        self, projection: TurnProjection, result: TurnResult,
    ) -> None:
        """Copy projection state onto the result and update session state.

        Reasoning items from this turn extend the session's continuity
        buffer so the next turn ships them all back to Codex.
        """
        result.thread_id = self._thread_id or ""
        result.final_text = projection.final_text
        result.messages = projection.messages
        result.reasoning_items = projection.reasoning_items
        result.tool_iterations = projection.tool_iterations

        # Extend the session-level continuity buffer with this turn's
        # reasoning items so multi-turn threads keep state.
        if projection.reasoning_items:
            self._reasoning_items.extend(projection.reasoning_items)


__all__ = [
    "CodexSession",
    "CodexSessionConfig",
    "TurnResult",
    "ApprovalCallback",
    "classify_oauth_failure",
    "DEFAULT_TURN_TIMEOUT_S",
    "POST_TOOL_QUIET_TIMEOUT_S",
]
