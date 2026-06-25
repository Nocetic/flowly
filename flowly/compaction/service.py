"""Compaction service for managing context compression."""

from typing import Any

from loguru import logger

from flowly.compaction.estimator import (
    estimate_messages_tokens,
    estimate_message_tokens,
)
from flowly.compaction.pruning import (
    prune_history_for_context_share,
    compute_adaptive_chunk_ratio,
)
from flowly.compaction.summarizer import summarize_in_stages
from flowly.compaction.types import (
    CompactionConfig,
    CompactionResult,
    SILENT_REPLY_TOKEN,
    SAFETY_MARGIN,
)
from flowly.providers.base import LLMProvider


class CompactionService:
    """
    Service for managing context compaction.

    Handles:
    - Automatic compaction when context exceeds threshold
    - Memory flush before compaction
    - Safeguard mode with adaptive chunking and pruning
    """

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
        self._compaction_count = 0
        self._memory_flush_compaction_count: int | None = None
        self._consecutive_failures = 0  # prevent compression death spiral

    @property
    def effective_context_window(self) -> int:
        """The window we can actually USE: the model's configured window,
        clamped to the Flowly proxy's input cap when that's the active
        provider (it rejects bigger prompts with 413 regardless of model)."""
        window = self.config.context_window
        if getattr(self.provider, "provider_name", "") == "flowly":
            return min(window, self.FLOWLY_PROXY_MAX_INPUT_TOKENS)
        return window

    def should_compact(self, total_tokens: int) -> bool:
        """
        Check if compaction should be triggered.

        Refuses to compact if 2+ consecutive compactions have failed
        (prevents death spiral where compact → fail → compact → fail).

        Args:
            total_tokens: Current total tokens in context.

        Returns:
            True if compaction is needed.
        """
        if self._consecutive_failures >= 2:
            logger.warning(
                "Compaction suppressed — %d consecutive failures (death spiral prevention)",
                self._consecutive_failures,
            )
            return False
        threshold = self.effective_context_window - self.config.reserve_tokens_floor
        return total_tokens > threshold

    def record_compaction_success(self) -> None:
        """Reset failure counter after successful compaction."""
        self._consecutive_failures = 0

    def record_compaction_failure(self) -> None:
        """Increment failure counter to detect death spirals."""
        self._consecutive_failures += 1

    def should_memory_flush(self, total_tokens: int) -> bool:
        """
        Check if memory flush should run before compaction.

        Args:
            total_tokens: Current total tokens in context.

        Returns:
            True if memory flush should run.
        """
        if not self.config.memory_flush.enabled:
            return False

        # Check if already flushed in this compaction cycle
        if self._memory_flush_compaction_count == self._compaction_count:
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

    def mark_memory_flush_done(self) -> None:
        """Mark that memory flush has been done for this compaction cycle."""
        self._memory_flush_compaction_count = self._compaction_count

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

        Walks backward from the end of messages, accumulating until
        min_tokens AND min_messages thresholds are met (or max_tokens hit).

        Args:
            messages: The FULL history BEFORE compaction.

        Returns:
            List of recent messages to keep (in original order).
        """
        cfg = self.config.keep_recent
        if not cfg.enabled or not messages:
            return []

        kept: list[dict[str, Any]] = []
        tokens_acc = 0
        text_msg_count = 0

        for msg in reversed(messages):
            msg_tokens = estimate_message_tokens(msg)

            # Hard cap
            if tokens_acc + msg_tokens > cfg.max_tokens:
                break

            kept.append(msg)
            tokens_acc += msg_tokens

            if msg.get("role") in ("user", "assistant"):
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    text_msg_count += 1

            # Met both minimums — stop
            if tokens_acc >= cfg.min_tokens and text_msg_count >= cfg.min_messages:
                break

        kept.reverse()
        return kept

    async def compact(
        self,
        messages: list[dict[str, Any]],
        custom_instructions: str | None = None,
        previous_summary: str | None = None,
    ) -> CompactionResult:
        """
        Compact messages by generating a summary.

        Args:
            messages: Messages to compact.
            custom_instructions: Optional custom instructions for summarization.
            previous_summary: Optional previous summary to incorporate.

        Returns:
            CompactionResult with summary and statistics.
        """
        if not messages:
            return CompactionResult(
                summary=previous_summary or "No prior history.",
                tokens_before=0,
                tokens_after=0,
                messages_removed=0,
            )

        tokens_before = estimate_messages_tokens(messages)

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

        # Safeguard mode: prune if needed
        if self.config.mode == "safeguard":
            pruned = prune_history_for_context_share(
                messages,
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

                # Summarize dropped messages separately
                if pruned["dropped_messages_list"]:
                    try:
                        dropped_chunk_ratio = compute_adaptive_chunk_ratio(
                            pruned["dropped_messages_list"],
                            self.config.context_window,
                        )
                        dropped_max_chunk_tokens = max(
                            1,
                            int(self.config.context_window * dropped_chunk_ratio),
                        )
                        dropped_summary = await summarize_in_stages(
                            pruned["dropped_messages_list"],
                            self.provider,
                            self.model,
                            self.config.reserve_tokens_floor,
                            dropped_max_chunk_tokens,
                            self.config.context_window,
                            custom_instructions,
                            previous_summary,
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to summarize dropped messages: {e}"
                        )

        # Calculate adaptive chunk ratio
        adaptive_ratio = compute_adaptive_chunk_ratio(
            messages_to_summarize,
            self.config.context_window,
        )
        max_chunk_tokens = max(
            1,
            int(self.config.context_window * adaptive_ratio),
        )

        # Use dropped summary as previous summary if available
        effective_previous = dropped_summary or previous_summary

        # Generate summary
        try:
            summary = await summarize_in_stages(
                messages_to_summarize,
                self.provider,
                self.model,
                self.config.reserve_tokens_floor,
                max_chunk_tokens,
                self.config.context_window,
                custom_instructions,
                effective_previous,
            )
        except Exception as e:
            logger.error(f"Compaction summarization failed: {e}")
            summary = (
                f"Context contained {len(messages)} messages. "
                "Summary unavailable due to error."
            )

        # Estimate tokens after (summary + kept messages)
        from flowly.compaction.estimator import estimate_tokens
        tokens_after = estimate_tokens(summary) + estimate_messages_tokens(kept_messages)

        # Increment compaction count
        self._compaction_count += 1

        return CompactionResult(
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            messages_removed=len(messages_to_summarize),
            dropped_chunks=dropped_chunks,
            dropped_messages=dropped_messages,
            dropped_tokens=dropped_tokens,
            kept_messages=kept_messages,
        )

    async def compact_if_needed(
        self,
        messages: list[dict[str, Any]],
        custom_instructions: str | None = None,
    ) -> tuple[list[dict[str, Any]], CompactionResult | None]:
        """
        Compact messages if threshold exceeded.

        Args:
            messages: Current messages.
            custom_instructions: Optional custom instructions.

        Returns:
            Tuple of (possibly compacted messages, CompactionResult or None).
        """
        total_tokens = estimate_messages_tokens(messages)

        if not self.should_compact(total_tokens):
            return messages, None

        logger.info(
            f"Compacting context: {total_tokens} tokens exceeds threshold"
        )

        result = await self.compact(messages, custom_instructions)

        # Replace messages with summary + kept recent messages
        summary_message = {
            "role": "system",
            "content": f"[Previous conversation summary]\n\n{result.summary}",
        }

        return [summary_message] + result.kept_messages, result

    @property
    def compaction_count(self) -> int:
        """Get the number of compactions performed."""
        return self._compaction_count
