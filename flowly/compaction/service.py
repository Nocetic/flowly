"""Compaction service for managing context compression."""

import asyncio
from dataclasses import dataclass
from typing import Any

from loguru import logger

from flowly.compaction.estimator import estimate_messages_tokens
from flowly.compaction.pruning import (
    prune_history_for_context_share,
    compute_adaptive_chunk_ratio,
    split_into_turn_blocks,
)
from flowly.compaction.summarizer import summarize_in_stages
from flowly.compaction.types import (
    CompactionConfig,
    CompactionError,
    CompactionResult,
    SILENT_REPLY_TOKEN,
    SAFETY_MARGIN,
    build_summary_content,
)
from flowly.providers.base import LLMProvider


def _heuristic_context_window(model: str) -> int | None:
    """Rough context window by model family, for a cold catalog.

    Deliberately conservative: guessing small compacts a bit early, while
    guessing large sails past the provider's limit and 413s mid-turn.
    """
    m = (model or "").lower()
    if not m:
        return None
    if "gemini" in m:
        return 1_000_000
    if "kimi" in m:
        return 262_144
    if "claude" in m or "sonnet" in m or "opus" in m or "haiku" in m:
        return 200_000
    if "gpt-4o" in m or "gpt-4-turbo" in m or "gpt-4.1" in m:
        return 128_000
    if "gpt-3.5" in m:
        return 16_385
    return None


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

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        config: CompactionConfig | None = None,
    ):
        """
        Initialize the compaction service.

        Args:
            provider: LLM provider for summarization.
            model: Model to use for summarization.
            config: Compaction configuration.
        """
        self.provider = provider
        self.model = model
        self.config = config or CompactionConfig()
        # The Flowly proxy hard-caps request input at ~80K tokens
        # (flowly-app MAX_INPUT_TOKENS) regardless of the model's window.
        # Budgeting against the model's 128K while the wire chops at 80K is
        # how mid-turn 413s happen — clamp when the active provider is flowly.
        self.FLOWLY_PROXY_MAX_INPUT_TOKENS = 80_000
        # Compaction bookkeeping is PER SESSION. A single service instance
        # serves every chat on the gateway, so a global counter let one
        # conversation's memory-flush cycle cancel another's, and one
        # conversation's outage suppress compaction for all of them.
        self._sessions: dict[str, _SessionState] = {}

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
        if self.config.context_window_explicit:
            return self.config.context_window

        try:
            from flowly.integrations.model_catalog import get_context_window

            live = get_context_window(self.model)
            if live and live > 0:
                return live
        except Exception:  # noqa: BLE001 - catalog is best-effort
            pass

        heuristic = _heuristic_context_window(self.model)
        if heuristic:
            return heuristic
        return self.config.context_window

    @property
    def effective_context_window(self) -> int:
        """The window we can actually USE: the model's real window, clamped
        to the Flowly proxy's input cap when that's the active provider (it
        rejects bigger prompts with 413 regardless of model)."""
        window = self.model_context_window
        if getattr(self.provider, "provider_name", "") == "flowly":
            return min(window, self.FLOWLY_PROXY_MAX_INPUT_TOKENS)
        return window

    @property
    def compaction_threshold(self) -> int:
        """Token count at which this session starts compacting."""
        return max(1, self.effective_context_window - self.config.reserve_tokens_floor)

    def should_compact(self, total_tokens: int, session_key: str = "") -> bool:
        """
        Check if compaction should be triggered.

        Backs off after repeated failures (prevents a death spiral where
        compact → fail → compact → fail burns tokens every turn), but the
        back-off is a cooldown, not a life sentence: one probe is allowed
        every ``FAILURE_PROBE_INTERVAL`` checks so a session recovers on
        its own once the provider does.

        Args:
            total_tokens: Current total tokens in context.
            session_key: Session these counters belong to.

        Returns:
            True if compaction is needed.
        """
        threshold = self.effective_context_window - self.config.reserve_tokens_floor
        if total_tokens <= threshold:
            return False

        state = self._state(session_key)
        if state.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            state.checks_since_suppression += 1
            if state.checks_since_suppression < self.FAILURE_PROBE_INTERVAL:
                logger.warning(
                    f"Compaction suppressed for {session_key or '<default>'} — "
                    f"{state.consecutive_failures} consecutive failures "
                    f"(retry in {self.FAILURE_PROBE_INTERVAL - state.checks_since_suppression} checks)"
                )
                return False
            state.checks_since_suppression = 0
            logger.info(
                f"Compaction probe after back-off for {session_key or '<default>'}"
            )
        return True

    def record_compaction_success(self, session_key: str = "") -> None:
        """Reset failure counter after successful compaction."""
        state = self._state(session_key)
        state.consecutive_failures = 0
        state.checks_since_suppression = 0

    def record_compaction_failure(self, session_key: str = "") -> None:
        """Increment failure counter to detect death spirals."""
        state = self._state(session_key)
        state.consecutive_failures += 1
        state.checks_since_suppression = 0

    def should_memory_flush(self, total_tokens: int, session_key: str = "") -> bool:
        """
        Check if memory flush should run before compaction.

        Args:
            total_tokens: Current total tokens in context.
            session_key: Session these counters belong to.

        Returns:
            True if memory flush should run.
        """
        if not self.config.memory_flush.enabled:
            return False

        # Check if already flushed in this compaction cycle
        state = self._state(session_key)
        if state.memory_flush_at_count == state.compaction_count:
            return False

        # Calculate soft threshold
        threshold = (
            self.effective_context_window
            - self.config.reserve_tokens_floor
            - self.config.memory_flush.soft_threshold_tokens
        )

        return total_tokens > threshold

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

    def _calculate_keep_recent(
        self, messages: list[dict[str, Any]]
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
        if not cfg.enabled or not messages:
            return []

        blocks = split_into_turn_blocks(messages)
        kept_blocks: list[list[dict[str, Any]]] = []
        tokens_acc = 0
        text_msg_count = 0

        for block in reversed(blocks):
            block_tokens = estimate_messages_tokens(block)

            # Hard cap — never split a block to squeeze under it.
            if kept_blocks and tokens_acc + block_tokens > cfg.max_tokens:
                break
            if not kept_blocks and block_tokens > cfg.max_tokens:
                # Even the newest block alone busts the cap; summarize
                # everything rather than emit a half block.
                return []

            kept_blocks.append(block)
            tokens_acc += block_tokens

            for msg in block:
                if msg.get("role") in ("user", "assistant"):
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        text_msg_count += 1

            # Met both minimums — stop
            if tokens_acc >= cfg.min_tokens and text_msg_count >= cfg.min_messages:
                break

        kept_blocks.reverse()
        return [msg for block in kept_blocks for msg in block]

    async def compact(
        self,
        messages: list[dict[str, Any]],
        custom_instructions: str | None = None,
        previous_summary: str | None = None,
        session_key: str = "",
    ) -> CompactionResult:
        """
        Compact messages by generating a summary.

        Args:
            messages: Messages to compact.
            custom_instructions: Optional custom instructions for summarization.
            previous_summary: Optional previous summary to incorporate.
            session_key: Session whose counters this attempt belongs to.

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
        # Size every summarisation call against the window we can actually
        # use — the model's real window, clamped by the active provider's
        # input cap. Budgeting chunks against a configured 128K while the
        # wire chops at 80K is how the summariser itself gets 413'd.
        window = self.effective_context_window

        # Determine which recent messages to preserve verbatim
        kept_messages = self._calculate_keep_recent(messages)
        kept_count = len(kept_messages)

        # Only summarize messages NOT in the kept set
        if kept_count > 0 and kept_count < len(messages):
            messages_to_summarize = messages[:-kept_count]
            logger.info(
                f"Keeping {kept_count} recent messages "
                f"(~{estimate_messages_tokens(kept_messages)} tokens), "
                f"summarizing {len(messages_to_summarize)}"
            )
        else:
            messages_to_summarize = messages
            kept_messages = []  # Nothing to keep (all summarized)
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
                        self.config.reserve_tokens_floor,
                        dropped_max_chunk_tokens,
                        window,
                        custom_instructions,
                        previous_summary,
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
            self.config.reserve_tokens_floor,
            max_chunk_tokens,
            window,
            custom_instructions,
            effective_previous,
        )

        # Estimate tokens after (summary + kept messages)
        from flowly.compaction.estimator import estimate_tokens
        tokens_after = estimate_tokens(summary) + estimate_messages_tokens(kept_messages)

        # A "compaction" that doesn't shrink anything is not worth the
        # history it destroys. Refuse it and let the caller carry on.
        if tokens_after >= tokens_before:
            raise CompactionError(
                f"compaction would not reduce context "
                f"({tokens_before} → {tokens_after} tokens); refusing to commit"
            )

        self._state(session_key).compaction_count += 1

        return CompactionResult(
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            # Net messages leaving the working context — NOT the size of the
            # summarisation input, which safeguard pruning also shrinks.
            messages_removed=len(messages) - len(kept_messages),
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
