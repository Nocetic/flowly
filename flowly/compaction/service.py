"""Compaction service for managing context compression."""

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from loguru import logger

from flowly.compaction.coordination import (
    BreakerState,
    CompactionCoordinator,
    CompactionLease,
)
from flowly.compaction.estimator import estimate_messages_tokens
from flowly.compaction.pruning import (
    compute_adaptive_chunk_ratio,
    prune_history_for_context_share,
    split_into_turn_blocks,
)
from flowly.compaction.summarizer import (
    ground_historical_request,
    reject_invented_user_attribution,
    summarize_in_stages,
)
from flowly.compaction.types import (
    EPHEMERAL_NUDGE_KEY,
    SILENT_REPLY_TOKEN,
    CompactionConfig,
    CompactionError,
    CompactionResult,
    build_summary_content,
    extract_summary_text,
    is_context_boundary,
    is_summary_message,
)
from flowly.providers.base import (
    PROVIDER_COMPACTION_CHECKPOINT_KEY,
    LLMProvider,
)


def _heuristic_context_window(model: str) -> int | None:
    """Rough context window by model family, for a cold catalog.

    The table itself lives in ``integrations.model_catalog`` — the TUI's token
    bar needs the same answer, and when each kept its own copy they drifted.
    This wrapper stays because three callers already import it by name.
    """
    from flowly.integrations.model_catalog import family_context_window

    return family_context_window(model)


def _flowly_proxy_max_input_tokens(model: str) -> int:
    """The Flowly proxy's per-model input ceiling.

    MIRROR of ``modelMaxInputTokens`` in flowly-app's
    ``app/api/v1/chat/completions/route.ts`` — keep the two in step.
    Budgeting against a window the wire would 413 is how mid-turn failures
    happen, so the bot must never assume more than the proxy accepts.

    Both sides resolve the model's REAL context length first and fall back to
    the family table only when the catalogue cannot place it. Reading that
    table as the primary answer is what capped most of the catalogue at 128K:
    it knows four families, so a 1M deepseek or a 262K gemma was budgeted —
    and compacted — as though it had 128K.

    The lookup is cache-only on both sides, so a cold process answers from the
    family table exactly as before. That is also what keeps the two safely in
    step when their caches disagree: a cold bot's number is lower than the
    proxy's, never higher, so it budgets conservatively instead of building a
    prompt the proxy would reject.
    """
    try:
        from flowly.integrations.model_catalog import get_context_window

        known = get_context_window(model)
        if known and known > 0:
            return known
    except Exception:  # noqa: BLE001 — catalogue is best-effort
        pass
    return _heuristic_context_window(model) or 128_000


def count_conversational_messages(messages: list[dict[str, Any]]) -> int:
    """How many of these a person would call messages.

    The working context also holds tool-call frames, tool results and the
    compaction summary — real entries the model reads, but not things the user
    sees as messages. Counting them made a three-exchange chat report "12 msgs
    summarized", which reads as either a bug or a lie about what was thrown
    away.
    """
    total = 0
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue  # tool results, system scaffolding
        if message.get("tool_calls"):
            continue  # the model asking for a tool, not speaking
        if message.get("_display_hidden"):
            continue  # an internal trigger (subagent announce, board result)
            # wearing the user role — nobody typed it
        if is_summary_message(message) or is_context_boundary(message):
            continue  # a previous compaction's summary or its seam, not a turn
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            total += 1
        elif isinstance(content, list) and content:
            total += 1
    return total


def _essential_turn_tail(block: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The irreducible verbatim core of a turn block: the user's question and
    the final plain-text answer.

    Used when the newest block is too big to preserve whole (a turn whose
    tool traffic dwarfs its words). Tool members and tool-calling assistant
    messages are deliberately left out — the summary carries what the tools
    did, and keeping only messages WITHOUT ``tool_calls`` keeps the sequence
    protocol-clean (no dangling call awaiting its result).
    """
    user = next((m for m in block if m.get("role") == "user"), None)
    final = None
    for m in reversed(block):
        if m.get("role") == "assistant" and not m.get("tool_calls"):
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                final = m
                break
    return [m for m in (user, final) if m is not None]


@dataclass
class _SessionState:
    """Compaction bookkeeping for one conversation."""

    compaction_count: int = 0
    memory_flush_at_count: int | None = None
    consecutive_failures: int = 0
    checks_since_suppression: int = 0
    # Created on first use — building an asyncio.Lock outside a running loop
    # binds it to the wrong one.
    lock: "asyncio.Lock | None" = None


class CompactionService:
    """
    Service for managing context compaction.

    Handles:
    - Automatic compaction when context exceeds threshold
    - Memory flush before compaction
    - Safeguard mode with adaptive chunking and pruning
    """

    # Consecutive failures before a session backs off compaction attempts.
    MAX_CONSECUTIVE_FAILURES = 2
    # While backed off, allow one probe every N over-threshold checks so a
    # session recovers by itself once the provider is healthy again.
    FAILURE_PROBE_INTERVAL = 10
    # Upper bound on tracked sessions before the oldest counters are dropped.
    MAX_TRACKED_SESSIONS = 500
    # A reply reserve may never eat more than this share of the window, however
    # it was configured. Above it there is no room left to hold a conversation
    # and compaction has nothing it can do.
    MAX_RESERVE_SHARE = 0.35
    # …and never less than this, or a tiny window would reserve nothing and the
    # reply itself would overflow the request.
    MIN_RESERVE_TOKENS = 512
    # The provider-observed trigger needs at least this much (estimated)
    # history before it may fire — roughly the size of a summary. Below it,
    # the request is over the threshold on fixed overhead the summariser
    # cannot shrink, and compacting just churns the session.
    MIN_OBSERVED_TRIGGER_HISTORY_TOKENS = 4_000
    # Sanity floor for a provider-observed input ceiling. Below this a
    # "learned" limit is more likely a mis-parsed number or a degenerate
    # estimate than a real request cap, and clamping the window to it would
    # leave no room to hold a conversation at all.
    MIN_OBSERVED_INPUT_LIMIT = 4_096

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        config: CompactionConfig | None = None,
        state_dir: Path | None = None,
    ):
        """
        Initialize the compaction service.

        Args:
            provider: LLM provider for summarization.
            model: Model to use for summarization.
            config: Compaction configuration.
            state_dir: Durable coordination root. Production callers should
                provide it; ``None`` keeps isolated/stateless uses in-memory.
        """
        self.provider = provider
        self.model = model
        self.config = config or CompactionConfig()
        self._coordinator: CompactionCoordinator | None = None
        if state_dir is not None:
            try:
                self._coordinator = CompactionCoordinator(Path(state_dir))
            except OSError as exc:
                # Read-only/test profiles must still boot. Compaction remains
                # protected by its in-process lock and fails safe at canonical
                # revision checks; production profiles log the missing durable
                # coordination layer prominently.
                logger.warning(
                    "Cross-process compaction coordination unavailable at {}: {}",
                    state_dir,
                    exc,
                )
        # Compaction bookkeeping is PER SESSION. A single service instance
        # serves every chat on the gateway, so a global counter let one
        # conversation's memory-flush cycle cancel another's, and one
        # conversation's outage suppress compaction for all of them.
        self._sessions: dict[str, _SessionState] = {}
        # Provider-observed request input ceilings, keyed by model id.
        # Deliberately in-memory: a tier upgrade or key change is picked up
        # after a restart at the cost of exactly one relearning 413, whereas a
        # persisted clamp could silently cap a healthy account forever.
        self._observed_input_limits: dict[str, int] = {}

    def session_lock(self, session_key: str = "") -> asyncio.Lock:
        """The lock serialising compaction for one conversation.

        Manual ``/compact`` and automatic compaction both end in a commit that
        clears the session and rewrites it, and ``get_or_create`` hands every
        caller the SAME session object — so without this, two compactions can
        interleave and the loser writes a summary derived from history the
        winner already replaced.

        Held across the summarisation call, which is a network round trip, so
        every caller must be prepared to wait. The lock is per session: a slow
        compaction in one conversation never blocks another.
        """
        state = self._state(session_key)
        if state.lock is None:
            state.lock = asyncio.Lock()
        return state.lock

    @asynccontextmanager
    async def compaction_lease(
        self,
        session_key: str = "",
    ) -> AsyncIterator[CompactionLease]:
        """Serialise one session in-process and across independent processes.

        The local lock is acquired first and remains the compatibility surface
        for callers/tests that inspect lock ownership.  The OS-backed lease is
        then held across summarisation and commit.  A stateless service still
        receives a fence-shaped value, but only production services configured
        with ``state_dir`` claim cross-process safety.
        """
        key = session_key or ""
        async with self.session_lock(key):
            if self._coordinator is None:
                yield CompactionLease(key, 0, "in-process", 0.0)
                return
            async with self._coordinator.lease(key) as lease:
                yield lease

    def _sync_breaker(self, session_key: str, state: _SessionState) -> None:
        if self._coordinator is None:
            return
        try:
            durable = self._coordinator.read_breaker(session_key)
        except OSError as exc:
            logger.warning(
                "Durable compaction breaker unavailable for {}: {}",
                session_key or "<default>",
                exc,
            )
            return
        state.consecutive_failures = durable.consecutive_failures
        state.checks_since_suppression = durable.checks_since_suppression

    def _update_breaker(
        self,
        session_key: str,
        state: _SessionState,
        update: Callable[[BreakerState], BreakerState],
    ) -> bool:
        if self._coordinator is None:
            return False
        try:
            durable = self._coordinator.update_breaker(session_key, update)
        except OSError as exc:
            logger.warning(
                "Durable compaction breaker update failed for {}: {}",
                session_key or "<default>",
                exc,
            )
            return False
        self._copy_breaker(state, durable)
        return True

    @staticmethod
    def _copy_breaker(state: _SessionState, durable: BreakerState) -> None:
        state.consecutive_failures = durable.consecutive_failures
        state.checks_since_suppression = durable.checks_since_suppression

    def _state(self, session_key: str = "") -> "_SessionState":
        """Per-session counters, created on first use.

        The empty key is a real bucket, used by callers that have no
        session (tests, one-off summarization).
        """
        key = session_key or ""
        state = self._sessions.get(key)
        if state is None:
            state = _SessionState()
            self._sessions[key] = state
            self._evict_stale_sessions()
        return state

    def reset_session(self, session_key: str = "") -> None:
        """Forget everything this service knows about a conversation.

        Called when the conversation itself is reset (``/clear``, ``/new``):
        the failure cooldown, the memory-flush cycle and the compaction count
        all describe history that no longer exists, and carrying them into a
        fresh chat suppresses its first legitimate compaction.

        Never drops a session whose lock is held — an in-flight commit is
        relying on that exclusion.
        """
        state = self._sessions.get(session_key or "")
        if state is not None:
            if state.lock is not None and state.lock.locked():
                # Keep the lock object the in-flight owner relies on, but none
                # of the old conversation's counters.  Returning here used to
                # carry a poisoned breaker and memory-flush cycle across /new.
                state.compaction_count = 0
                state.memory_flush_at_count = None
                state.consecutive_failures = 0
                state.checks_since_suppression = 0
            else:
                self._sessions.pop(session_key or "", None)
        if self._coordinator is not None:
            try:
                self._coordinator.clear_breaker(session_key or "")
            except OSError as exc:
                logger.warning(
                    "Durable compaction breaker reset failed for {}: {}",
                    session_key or "<default>",
                    exc,
                )

    def _evict_stale_sessions(self) -> None:
        """Bound the counter map on a long-lived gateway.

        Sessions come and go for months; a few ints each is small but
        unbounded. Dropping the oldest entries only resets a memory-flush
        cycle and a failure count, both of which re-derive on their own.
        """
        overflow = len(self._sessions) - self.MAX_TRACKED_SESSIONS
        if overflow <= 0:
            return
        for key in list(self._sessions)[:overflow]:
            if not key:  # keep the default bucket
                continue
            state = self._sessions.get(key)
            # Never evict a session mid-compaction: dropping its state would
            # hand the next caller a brand-new lock and undo the exclusion the
            # in-flight commit is relying on.
            if state is not None and state.lock is not None and state.lock.locked():
                continue
            self._sessions.pop(key, None)

    @property
    def model_context_window(self) -> int:
        """The active model's real context window.

        Resolution order:

        1. **Live catalog** — the provider's own ``context_length``, so a
           32K model compacts early and a 1M model doesn't compact at 8%
           full. Cache-only and unwarmed on a cold gateway, hence the
           fallbacks.
        2. **Family heuristics** — enough to be roughly right for the
           common families before the catalog lands.
        3. **Configured value** — the user's explicit setting, and the
           last resort.

        A user who pinned ``compaction.contextWindow`` themselves always
        wins: an operator override must not be second-guessed by a catalog.
        """
        return self.model_context_window_for(self.model)

    def model_context_window_for(self, model: str | None = None) -> int:
        """Resolve a model window without mutating the service's default model."""
        selected_model = model or self.model
        if self.config.context_window_explicit:
            return self.config.context_window

        try:
            from flowly.integrations.model_catalog import get_context_window

            live = get_context_window(selected_model)
            if live and live > 0:
                return live
        except Exception:  # noqa: BLE001 - catalog is best-effort
            pass

        heuristic = _heuristic_context_window(selected_model)
        if heuristic:
            return heuristic
        return self.config.context_window

    @property
    def effective_context_window(self) -> int:
        """The window we can actually USE: the model's real window, clamped
        to the Flowly proxy's PER-MODEL input ceiling when that's the active
        provider (it rejects bigger prompts with 413). The ceiling follows
        the model family, so a 200K Claude gets its 200K — the old flat 80K
        cap choked every large model to a fraction of its window."""
        return self.effective_context_window_for(self.model)

    def effective_context_window_for(self, model: str | None = None) -> int:
        """Usable request window for a per-turn model override.

        Clamped by any provider-observed input ceiling for this model (see
        :meth:`note_observed_input_limit`): a 413 the provider actually sent
        outranks whatever the catalog believes the model could accept.
        """
        selected_model = model or self.model
        window = self.model_context_window_for(selected_model)
        observed = self._observed_input_limits.get(selected_model)
        if observed and observed < window:
            window = observed
        if getattr(self.provider, "provider_name", "") == "flowly":
            return min(window, _flowly_proxy_max_input_tokens(selected_model))
        return window

    def note_observed_input_limit(
        self, model: str | None, limit_tokens: int, *, reason: str = "",
    ) -> int | None:
        """Learn a provider-enforced request input ceiling for ``model``.

        Called when a provider rejected a request as too large (413 /
        ``request_too_large``) even though the request fit the catalog
        window — a TPM tier, per-request cap, or proxy guard the catalog
        cannot see. Recording it makes every later budget (durable pre-turn
        compaction AND pre-dispatch request fitting) shrink to fit, so the
        session stops paying a transient recovery on every turn.

        Lower-only within the process: repeated observations can tighten the
        ceiling but a looser reading never widens it — convergence, not
        oscillation. Readings below :attr:`MIN_OBSERVED_INPUT_LIMIT` or at/
        above the model's raw window are discarded as noise.

        Returns the ceiling now in effect, or ``None`` when the reading was
        unusable and nothing is clamped.
        """
        selected_model = (model or self.model or "").strip()
        if not selected_model:
            return None
        try:
            limit = int(limit_tokens)
        except (TypeError, ValueError):
            return None
        raw_window = self.model_context_window_for(selected_model)
        if limit < self.MIN_OBSERVED_INPUT_LIMIT or limit >= raw_window:
            return self._observed_input_limits.get(selected_model)
        current = self._observed_input_limits.get(selected_model)
        if current is not None and current <= limit:
            return current
        self._observed_input_limits[selected_model] = limit
        logger.warning(
            "Learned provider input ceiling for {}: {} tokens (catalog window "
            "{}){} — future requests budget against it",
            selected_model,
            limit,
            raw_window,
            f" [{reason}]" if reason else "",
        )
        return limit

    def observed_input_limit_for(self, model: str | None = None) -> int | None:
        """The provider-observed input ceiling for ``model``, if one is known."""
        selected_model = (model or self.model or "").strip()
        return self._observed_input_limits.get(selected_model)

    @property
    def effective_reserve_tokens(self) -> int:
        """Headroom kept free for the reply, CLAMPED TO THE WINDOW.

        The configured reserve is sized for the big models most people run
        (20K of a 128K window). Applied literally to a small one it exceeds
        the window outright: a 16,385-token model minus a 20,000-token reserve
        gave a threshold of 1 and a NEGATIVE history budget — so the request
        never fit and compaction refused to run ("overhead leaves no room"),
        every single turn.

        A reserve is a share of the window, not an absolute. Small models get
        the share; large ones keep the configured value, which is smaller than
        the share and therefore wins.
        """
        return self.effective_reserve_tokens_for(self.model)

    def effective_reserve_tokens_for(self, model: str | None = None) -> int:
        """Reply reserve sized against the model actually being called."""
        configured = max(0, self.config.reserve_tokens_floor)
        window = max(1, self.effective_context_window_for(model))
        return max(
            self.MIN_RESERVE_TOKENS,
            min(configured, int(window * self.MAX_RESERVE_SHARE)),
        )

    @property
    def compaction_threshold(self) -> int:
        """Token count at which this session starts compacting."""
        return self.compaction_threshold_for(self.model)

    def compaction_threshold_for(self, model: str | None = None) -> int:
        """Maximum estimated request input while preserving reply headroom."""
        return max(
            1,
            self.effective_context_window_for(model)
            - self.effective_reserve_tokens_for(model),
        )

    def history_budget(self, overhead_tokens: int = 0) -> int:
        """Tokens the conversation itself may occupy.

        The window has to hold three things: the fixed overhead of every
        request (system prompt, tool schemas, the incoming message), the reply
        we reserve room for, and the history. Only the last of those is
        something compaction can shrink.
        """
        return self.compaction_threshold - max(0, overhead_tokens)

    def should_compact(
        self,
        history_tokens: int,
        session_key: str = "",
        overhead_tokens: int = 0,
        observed_total_tokens: int = 0,
        breaker_probe_authorized: bool = False,
    ) -> bool:
        """
        Check if compaction should be triggered.

        Judged on the HISTORY against the room left for it — not on the total
        request size. Judged on the total, a large fixed overhead alone
        tripped the threshold, and the agent then tried to compact a
        two-message chat, failed to reduce anything, and reported that as an
        error every turn. Compaction can only summarise the conversation; it
        cannot shrink the system prompt or the tool schemas.

        ``observed_total_tokens`` is the PROVIDER's own count of the last
        request (``usage.prompt_tokens`` + completion) — ground truth where
        everything else here is an estimate. Local estimators drift by model
        and language (10-20% is normal), and 75 tool schemas amplify the
        error: a session can sit comfortably under the estimated budget while
        its real requests already exceed the window's threshold. When the
        provider says the context is full, believe the provider.

        Backs off after repeated failures (prevents a death spiral where
        compact → fail → compact → fail burns tokens every turn), but the
        back-off is a cooldown, not a life sentence: one probe is allowed
        every ``FAILURE_PROBE_INTERVAL`` checks so a session recovers on
        its own once the provider does.

        Args:
            history_tokens: Tokens the conversation history occupies (estimate).
            session_key: Session these counters belong to.
            overhead_tokens: Everything else the request carries (estimate).
            observed_total_tokens: Provider-reported size of the last turn's
                request, 0 when unknown.
            breaker_probe_authorized: The caller already passed the durable
                backoff gate immediately before acquiring the session lease.
                Used only for the under-lease recheck; avoids consuming the
                same recovery probe twice.

        Returns:
            True if compaction is needed.
        """
        budget = self.history_budget(overhead_tokens)
        if budget <= 0:
            # The request cannot fit no matter how much history we remove.
            # Summarising here burns a call to change nothing; the mid-turn
            # guard and overflow recovery are what handle this.
            logger.warning(
                f"Fixed request overhead ({overhead_tokens} tokens) leaves no room "
                f"in a {self.effective_context_window}-token window — compaction "
                "cannot help; raise the context window or trim tools/prompt"
            )
            return False
        over_by_estimate = history_tokens > budget
        # The observation only justifies compacting when there is meaningful
        # history to reclaim. A real request can exceed the threshold on
        # fixed overhead alone (observed live: 76.4K real with 1.8K of
        # history in a 79K window) — summarising a couple of messages there
        # frees almost nothing and just churns the session.
        over_by_observation = (
            observed_total_tokens > self.compaction_threshold
            and history_tokens > self.MIN_OBSERVED_TRIGGER_HISTORY_TOKENS
        )
        if over_by_observation and not over_by_estimate:
            logger.info(
                f"Compaction triggered by provider-reported usage: "
                f"{observed_total_tokens} tokens exceeds the "
                f"{self.compaction_threshold}-token threshold "
                f"(estimate saw only {history_tokens} history / {budget} budget)"
            )
        if not (over_by_estimate or over_by_observation):
            return False

        state = self._state(session_key)
        self._sync_breaker(session_key or "", state)
        if (
            state.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES
            and not breaker_probe_authorized
        ):
            if not self._update_breaker(
                session_key or "",
                state,
                lambda current: BreakerState(
                    current.consecutive_failures,
                    current.checks_since_suppression + 1,
                ),
            ):
                state.checks_since_suppression += 1
            if state.checks_since_suppression < self.FAILURE_PROBE_INTERVAL:
                logger.warning(
                    f"Compaction suppressed for {session_key or '<default>'} — "
                    f"{state.consecutive_failures} consecutive failures "
                    f"(retry in {self.FAILURE_PROBE_INTERVAL - state.checks_since_suppression} checks)"
                )
                return False
            if not self._update_breaker(
                session_key or "",
                state,
                lambda current: BreakerState(
                    current.consecutive_failures,
                    0,
                ),
            ):
                state.checks_since_suppression = 0
            logger.info(
                f"Compaction probe after back-off for {session_key or '<default>'}"
            )
        return True

    def record_compaction_success(self, session_key: str = "") -> None:
        """Reset failure counter after successful compaction."""
        state = self._state(session_key)
        if not self._update_breaker(
            session_key or "", state, lambda _current: BreakerState()
        ):
            state.consecutive_failures = 0
            state.checks_since_suppression = 0

    def record_compaction_failure(self, session_key: str = "") -> None:
        """Increment failure counter to detect death spirals."""
        state = self._state(session_key)
        if not self._update_breaker(
            session_key or "",
            state,
            lambda current: BreakerState(
                current.consecutive_failures + 1,
                0,
            ),
        ):
            state.consecutive_failures += 1
            state.checks_since_suppression = 0

    def should_memory_flush(
        self,
        history_tokens: int,
        session_key: str = "",
        overhead_tokens: int = 0,
        observed_total_tokens: int = 0,
    ) -> bool:
        """
        Check if memory flush should run before compaction.

        Measured the same way as :meth:`should_compact` — history against the
        room left for it. The flush exists to save durable memories shortly
        BEFORE a compaction, so it has to fire on the same signal; judged on
        the total instead, it ran on every turn of an idle chat whose request
        overhead happened to be large. The provider-observed total joins for
        the same reason: a compaction triggered by real usage must still get
        its flush first, or the summarised turns are never mined for memory.

        Args:
            history_tokens: Tokens the conversation history occupies.
            session_key: Session these counters belong to.
            overhead_tokens: Everything else the request carries.
            observed_total_tokens: Provider-reported size of the last turn's
                request, 0 when unknown.

        Returns:
            True if memory flush should run.
        """
        if not self.config.memory_flush.enabled:
            return False

        # Check if already flushed in this compaction cycle
        state = self._state(session_key)
        if state.memory_flush_at_count == state.compaction_count:
            return False

        budget = self.history_budget(overhead_tokens)
        if budget <= 0:
            return False
        soft = self.config.memory_flush.soft_threshold_tokens
        return (
            history_tokens > budget - soft
            or observed_total_tokens > self.compaction_threshold - soft
        )

    def get_memory_flush_prompt(self) -> tuple[str, str]:
        """
        Get the prompts for memory flush turn.

        Returns:
            Tuple of (user_prompt, system_prompt).
        """
        return (
            self.config.memory_flush.prompt,
            self.config.memory_flush.system_prompt,
        )

    def mark_memory_flush_done(self, session_key: str = "") -> None:
        """Mark that memory flush has been done for this compaction cycle."""
        state = self._state(session_key)
        state.memory_flush_at_count = state.compaction_count

    def is_silent_reply(self, response: str) -> bool:
        """
        Check if response should be silent (not sent to user).

        Args:
            response: The response text.

        Returns:
            True if response starts with NO_REPLY token.
        """
        return response.strip().startswith(SILENT_REPLY_TOKEN)

    def strip_silent_token(self, response: str) -> str:
        """
        Strip the NO_REPLY token from response.

        Args:
            response: The response text.

        Returns:
            Response without the token.
        """
        stripped = response.strip()
        if stripped.startswith(SILENT_REPLY_TOKEN):
            return stripped[len(SILENT_REPLY_TOKEN):].strip()
        return response

    def microcompact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Proactively truncate old tool results to delay compaction.

        Keeps the most recent N tool results at full length,
        truncates older ones to save tokens.

        Args:
            messages: Current message history.

        Returns:
            Messages with old tool results truncated (in-place safe, returns new list).
        """
        cfg = self.config.microcompact
        if not cfg.enabled:
            return messages

        # Lazy import — flowly.agent.loop imports this module at top level,
        # so importing flowly.agent.* here would create an import cycle.
        from flowly.agent.tool_result_spill import extract_spill_pointer

        # Find indices of all tool-role messages
        tool_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool"
        ]

        if len(tool_indices) <= cfg.keep_recent_full:
            return messages  # Nothing to truncate

        # Indices to truncate (all except the most recent N)
        truncate_indices = set(tool_indices[:-cfg.keep_recent_full])

        result = []
        changed = 0
        for i, msg in enumerate(messages):
            if i in truncate_indices:
                content = msg.get("content", "")
                if len(content) > cfg.truncate_chars:
                    truncated = content[:cfg.truncate_chars] + "\n[old tool output truncated]"
                    # A spilled tool result carries a file pointer at the END
                    # of its text — exactly where truncation cuts. Re-append
                    # it so the full output stays reachable via read_file.
                    pointer = extract_spill_pointer(content)
                    if pointer and pointer not in truncated:
                        truncated += pointer
                    result.append({**msg, "content": truncated})
                    changed += 1
                else:
                    result.append(msg)
            else:
                result.append(msg)

        if changed > 0:
            logger.debug(f"Microcompact: truncated {changed} old tool results")

        return result

    def _keep_recent_cap(self, history_budget: int) -> int:
        """How many tokens the preserved tail may occupy.

        A share of the room available for history, floored by the configured
        ceiling. An absolute cap alone is wrong in both directions: on a large
        window it preserves too little to be useful, and on a small one it
        preserves nearly everything, leaving compaction nothing to compress.
        """
        cap = self.config.keep_recent.max_tokens
        if history_budget > 0:
            share = max(0.05, min(self.config.keep_recent.max_share, 0.8))
            cap = min(cap, max(1, int(history_budget * share)))
        return cap

    def _calculate_keep_recent(
        self,
        messages: list[dict[str, Any]],
        history_budget: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Determine which recent messages to preserve after compaction.

        Walks backward one TURN BLOCK at a time — a user message plus every
        assistant/tool message that answers it — accumulating until
        min_tokens AND min_messages are met (or max_tokens would be
        exceeded). Blocks, not individual messages, are the unit: cutting
        mid-block strands a ``tool`` result away from the
        ``assistant.tool_calls`` that produced it, and providers reject
        that sequence with a 400.

        Args:
            messages: The FULL history BEFORE compaction.

        Returns:
            List of recent messages to keep (in original order).
        """
        cfg = self.config.keep_recent
        # Prompt-only provider checkpoints are opaque transport controls, not
        # conversation content. If local compaction takes ownership, summarize
        # the complete raw transcript around them and emit a normal checkpoint.
        messages = [
            message
            for message in messages
            if not message.get(PROVIDER_COMPACTION_CHECKPOINT_KEY)
        ]
        if not messages:
            return []

        # A generated summary is reference data; it must never become the only
        # representation of the most recent user request. Preserve that raw
        # message even when it alone exceeds the preferred tail cap. If the
        # provider cannot accept that request verbatim, the compaction is
        # refused later instead of pretending a lossy paraphrase is equivalent.
        newest_user = next(
            (
                message
                for message in reversed(messages)
                if message.get("role") == "user"
                and not message.get(EPHEMERAL_NUDGE_KEY)
            ),
            None,
        )

        def _with_newest_user(
            selected: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            if newest_user is None or any(item is newest_user for item in selected):
                return selected
            selected_ids = {id(item) for item in selected}
            selected_ids.add(id(newest_user))
            return [item for item in messages if id(item) in selected_ids]

        if not cfg.enabled:
            return _with_newest_user([])

        cap = self._keep_recent_cap(history_budget)
        blocks = split_into_turn_blocks(messages)
        kept_blocks: list[list[dict[str, Any]]] = []
        tokens_acc = 0
        text_msg_count = 0

        for block in reversed(blocks):
            block_tokens = estimate_messages_tokens(block)

            # Hard cap — never split a block to squeeze under it.
            if kept_blocks and tokens_acc + block_tokens > cap:
                break
            if not kept_blocks and block_tokens > cap:
                # Even the newest block alone busts the share cap. Returning
                # nothing here produced "kept 0 recent" live: the agent came
                # back from compaction having verbatim-forgotten the very
                # message it was answering. The tail must never be empty —
                # keep the whole block while it fits in half the history
                # budget; past that, keep the user's question and the final
                # text answer and let the summary carry the tool traffic.
                hard_limit = (
                    max(cap, history_budget // 2) if history_budget > 0 else cap
                )
                if block_tokens <= hard_limit:
                    return _with_newest_user(list(block))
                essential = _essential_turn_tail(block)
                if (
                    essential
                    and estimate_messages_tokens(essential) <= hard_limit
                ):
                    return _with_newest_user(essential)
                return _with_newest_user([])

            kept_blocks.append(block)
            tokens_acc += block_tokens

            for msg in block:
                if msg.get("role") in ("user", "assistant"):
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        text_msg_count += 1

            # Met both minimums — stop
            if tokens_acc >= min(cfg.min_tokens, cap) and text_msg_count >= cfg.min_messages:
                break

        kept_blocks.reverse()
        return _with_newest_user([
            msg for block in kept_blocks for msg in block
        ])

    async def compact(
        self,
        messages: list[dict[str, Any]],
        custom_instructions: str | None = None,
        previous_summary: str | None = None,
        session_key: str = "",
        should_cancel: "Callable[[], bool] | None" = None,
        history_budget: int = 0,
        track_cycle: bool = True,
    ) -> CompactionResult:
        """
        Compact messages by generating a summary.

        Args:
            messages: Messages to compact.
            custom_instructions: Optional custom instructions for summarization.
            previous_summary: Optional previous summary to incorporate.
            session_key: Session whose counters this attempt belongs to.
            should_cancel: Polled between provider round trips; returning True
                aborts the attempt and leaves the history untouched.
            history_budget: Room the conversation may occupy after this runs.
                Sizes the preserved tail — without it the tail is capped by an
                absolute number that can exceed the whole history.
            track_cycle: Whether this is a durable session compaction cycle.
                Mid-turn request fitting is transient and sets this false.

        Returns:
            CompactionResult with summary and statistics.

        Raises:
            CompactionError: The summary could not be produced or would not
                shrink the context. The caller must keep its history intact.
        """
        if not messages:
            return CompactionResult(
                summary=previous_summary or "No prior history.",
                tokens_before=0,
                tokens_after=0,
                messages_removed=0,
            )

        tokens_before = estimate_messages_tokens(messages)

        # A committed checkpoint is state, not another transcript turn. Feed
        # its body through ``previous_summary`` and summarize only the new raw
        # delta. Otherwise each cycle recursively summarizes our own safety
        # preamble and marker, which steadily distorts facts and wastes room.
        inherited_summaries: list[str] = []
        source_messages: list[dict[str, Any]] = []
        for message in messages:
            inherited = extract_summary_text(message)
            if inherited is None:
                source_messages.append(message)
            elif inherited.strip():
                inherited_summaries.append(inherited.strip())
        if previous_summary and previous_summary.strip():
            inherited_summaries.append(previous_summary.strip())
        if inherited_summaries:
            previous_summary = "\n\n".join(dict.fromkeys(inherited_summaries))

        # Size every summarisation call against the window we can actually
        # use — the model's real window, clamped by the active provider's
        # input cap. Budgeting chunks against a configured 128K while the
        # wire chops at 80K is how the summariser itself gets 413'd.
        window = self.effective_context_window

        # Determine which recent messages to preserve verbatim
        kept_messages = self._calculate_keep_recent(source_messages, history_budget)
        kept_count = len(kept_messages)

        # Only summarize messages NOT in the kept set — valid only when the
        # kept tail IS the history's suffix. The essential-tail fallback
        # returns an EXTRACT of the last block (user question + final
        # answer), and suffix arithmetic on an extract silently loses the
        # block's other members from both the summary and the tail. An
        # extracted tail therefore summarizes everything; its verbatim copy
        # rides on top, at the cost of a little duplication.
        is_suffix = (
            0 < kept_count < len(source_messages)
            and kept_messages == source_messages[-kept_count:]
        )
        if is_suffix:
            messages_to_summarize = source_messages[:-kept_count]
            logger.info(
                f"Keeping {kept_count} recent messages "
                f"(~{estimate_messages_tokens(kept_messages)} tokens), "
                f"summarizing {len(messages_to_summarize)}"
            )
        elif kept_count > 0 and kept_count < len(source_messages):
            messages_to_summarize = source_messages
            logger.info(
                f"Keeping the essential tail ({kept_count} messages, "
                f"~{estimate_messages_tokens(kept_messages)} tokens) verbatim; "
                f"summarizing all {len(source_messages)}"
            )
        elif kept_count == len(source_messages) and source_messages:
            newest_block = split_into_turn_blocks(source_messages)[-1]
            protected_tail = _essential_turn_tail(newest_block)
            if protected_tail:
                # The preference-based tail happened to include the whole
                # history (common when compact() is called directly without a
                # history budget). Reduce it to the invariant we actually need:
                # newest raw question + final plain-text answer. Summarize all
                # source messages so tool actions still reach the reference.
                kept_messages = protected_tail
                messages_to_summarize = source_messages
                logger.info(
                    "Recent-tail preference covered the whole history; "
                    "preserving the newest essential turn and summarizing all"
                )
            else:
                # A genuinely userless/system-driven history has no active
                # request to protect.
                messages_to_summarize = source_messages
                kept_messages = []
        else:
            messages_to_summarize = source_messages
            kept_messages = []  # Nothing to keep (all summarized)
        grounding_messages = list(messages_to_summarize)
        dropped_summary: str | None = None
        dropped_chunks = 0
        dropped_messages = 0
        dropped_tokens = 0

        # Safeguard mode: prune if needed.
        # Budget the SUMMARISATION INPUT, not the whole history — the kept
        # tail is preserved verbatim, so including it here would both
        # summarize it a second time (duplicating it into the summary AND
        # the working context) and skew every count we report.
        if self.config.mode == "safeguard":
            pruned = prune_history_for_context_share(
                messages_to_summarize,
                self.effective_context_window,
                self.config.max_history_share,
                parts=2,
            )

            if pruned["dropped_chunks"] > 0:
                logger.info(
                    f"Compaction safeguard: dropped {pruned['dropped_chunks']} "
                    f"chunk(s) ({pruned['dropped_messages']} messages) to fit history budget"
                )
                messages_to_summarize = pruned["messages"]
                dropped_chunks = pruned["dropped_chunks"]
                dropped_messages = pruned["dropped_messages"]
                dropped_tokens = pruned["dropped_tokens"]

                # Summarize dropped messages separately. A failure here is
                # fatal to the whole attempt: these messages are about to
                # leave the working context, so if they don't make it into
                # a summary they are simply lost.
                if pruned["dropped_messages_list"]:
                    dropped_chunk_ratio = compute_adaptive_chunk_ratio(
                        pruned["dropped_messages_list"],
                        window,
                    )
                    dropped_max_chunk_tokens = max(
                        1,
                        int(window * dropped_chunk_ratio),
                    )
                    dropped_summary = await summarize_in_stages(
                        pruned["dropped_messages_list"],
                        self.provider,
                        self.model,
                        self.effective_reserve_tokens,
                        dropped_max_chunk_tokens,
                        window,
                        custom_instructions,
                        previous_summary,
                        should_cancel=should_cancel,
                    )

        # Calculate adaptive chunk ratio
        adaptive_ratio = compute_adaptive_chunk_ratio(
            messages_to_summarize,
            window,
        )
        max_chunk_tokens = max(
            1,
            int(window * adaptive_ratio),
        )

        # Use dropped summary as previous summary if available
        effective_previous = dropped_summary or previous_summary

        # Generate summary. Any failure propagates as CompactionError so the
        # caller keeps its uncompacted history — there is no summary text
        # worth committing in place of a conversation we failed to read.
        summary = await summarize_in_stages(
            messages_to_summarize,
            self.provider,
            self.model,
            self.effective_reserve_tokens,
            max_chunk_tokens,
            window,
            custom_instructions,
            effective_previous,
            should_cancel=should_cancel,
        )

        # The model supplies the rich checkpoint, but the current-task anchor
        # comes directly from the newest real user turn in the compacted
        # region. This prevents a plausible-looking summary from reviving an
        # older request or missing a cancellation/reversal.
        summary = ground_historical_request(summary, grounding_messages)

        # Applied once, against the WHOLE conversation rather than a chunk: a
        # chunk can legitimately have no user turn even when the conversation
        # does, and rejecting on that would fail real compactions.
        if not inherited_summaries:
            reject_invented_user_attribution(summary, source_messages)

        # Measure the exact prompt shape that will be committed, including the
        # reference-only wrapper. Omitting the wrapper here could approve a
        # compaction that actually grows the provider-facing context.
        tokens_after = estimate_messages_tokens([
            {"role": "system", "content": build_summary_content(summary)},
            *kept_messages,
        ])

        # A "compaction" that doesn't shrink anything is not worth the
        # history it destroys. Refuse it and let the caller carry on.
        if tokens_after >= tokens_before:
            raise CompactionError(
                f"compaction would not reduce context "
                f"({tokens_before} → {tokens_after} tokens); refusing to commit"
            )

        if track_cycle:
            self._state(session_key).compaction_count += 1

        return CompactionResult(
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            # What a PERSON would count as messages, because this number is
            # only ever shown to one. The raw protocol delta (tool frames and
            # all) goes to the log below instead.
            messages_removed=max(
                0,
                count_conversational_messages(messages)
                - count_conversational_messages(kept_messages),
            ),
            dropped_chunks=dropped_chunks,
            dropped_messages=dropped_messages,
            dropped_tokens=dropped_tokens,
            kept_messages=kept_messages,
        )

    async def compact_if_needed(
        self,
        messages: list[dict[str, Any]],
        custom_instructions: str | None = None,
        session_key: str = "",
    ) -> tuple[list[dict[str, Any]], CompactionResult | None]:
        """
        Compact messages if threshold exceeded, without touching a session.

        This is the STATELESS form: messages in, messages out. The agent loop
        does not use it — it owns persistence (flushing the display
        transcript, clearing and rewriting the session, emitting lifecycle
        events), which cannot be expressed here.

        Keep the two in step. This method and the loop share the decision
        (:meth:`should_compact`), the summarisation (:meth:`compact`) and the
        failure policy — return the ORIGINAL messages, never a partial
        compaction — so a change to any of those belongs in both.

        Args:
            messages: Current messages.
            custom_instructions: Optional custom instructions.
            session_key: Session whose counters this attempt belongs to.

        Returns:
            Tuple of (possibly compacted messages, CompactionResult or None).
            On failure the ORIGINAL messages come back with a None result —
            never a partially-applied compaction.
        """
        total_tokens = estimate_messages_tokens(messages)

        if not self.should_compact(total_tokens, session_key):
            return messages, None

        logger.info(
            f"Compacting context: {total_tokens} tokens exceeds threshold"
        )

        try:
            result = await self.compact(
                messages, custom_instructions, session_key=session_key,
                history_budget=self.history_budget(),
            )
        except CompactionError as e:
            logger.error(f"Compaction failed, keeping full history: {e}")
            self.record_compaction_failure(session_key)
            return messages, None

        self.record_compaction_success(session_key)

        # Replace messages with summary + kept recent messages
        summary_message = {
            "role": "system",
            "content": build_summary_content(result.summary),
        }

        return [summary_message] + result.kept_messages, result

    @property
    def compaction_count(self) -> int:
        """Total compactions performed across every session."""
        return sum(s.compaction_count for s in self._sessions.values())

    def compaction_count_for(self, session_key: str = "") -> int:
        """Compactions performed for one session."""
        return self._state(session_key).compaction_count
