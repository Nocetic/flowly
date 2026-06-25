"""Message summarization for compaction."""

from typing import Any

from loguru import logger

from flowly.compaction.estimator import estimate_messages_tokens, estimate_message_tokens
from flowly.compaction.pruning import (
    chunk_messages_by_max_tokens,
    split_messages_by_token_share,
    is_oversized_for_summary,
)
from flowly.compaction.types import (
    DEFAULT_SUMMARY_FALLBACK,
    DEFAULT_PARTS,
    MERGE_SUMMARIES_INSTRUCTIONS,
    SAFETY_MARGIN,
)
from flowly.providers.base import LLMProvider


def _strip_tool_results_for_compaction(
    messages: list[dict[str, Any]],
    max_chars: int = 1000,
) -> list[dict[str, Any]]:
    """Strip verbose tool results before summarization to reduce token cost."""
    stripped = []
    for msg in messages:
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if len(content) > max_chars:
                stripped.append({
                    **msg,
                    "content": content[:max_chars] + "\n[tool output truncated for compaction]",
                })
            else:
                stripped.append(msg)
        else:
            stripped.append(msg)
    return stripped


SUMMARIZE_SYSTEM_PROMPT = """You are a conversation summarizer. Create a structured summary of the conversation.

Your summary MUST include these sections:

## Decisions
Key decisions made and their rationale.

## Open TODOs
Tasks still pending, with current status (in-progress, blocked, waiting).

## Last Request
What the user last asked for and what was being done about it.

## Tool Results & Actions Taken
Important tool outputs, file changes, commands executed, and their results. Include key findings from web searches, file reads, and system commands.

## Exact Identifiers
Preserve ALL identifiers exactly as written — UUIDs, URLs, file paths, API keys, tokens, hostnames, IPs, ports. Do not shorten or reconstruct them.

## Constraints
Any constraints, rules, or requirements mentioned.

Keep the summary concise and actionable. Prioritize recent context over older history. Never lose information about what actions were taken and what results they produced."""

SUMMARIZE_USER_PROMPT = """Summarize the following conversation. Follow the required section format strictly.

{conversation}

{custom_instructions}

Previous context (if any):
{previous_summary}"""


async def generate_summary(
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    model: str,
    reserve_tokens: int,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
) -> str:
    """
    Generate a summary of messages using the LLM.

    Args:
        messages: Messages to summarize.
        provider: LLM provider.
        model: Model to use.
        reserve_tokens: Tokens to reserve for output.
        custom_instructions: Optional custom instructions.
        previous_summary: Optional previous summary to incorporate.

    Returns:
        Summary text.
    """
    if not messages:
        return previous_summary or DEFAULT_SUMMARY_FALLBACK

    # Format conversation for summarization
    conversation_parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            conversation_parts.append(f"[{role}]: {content}")
        elif isinstance(content, list):
            # Extract text from multi-part content
            text_parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if text_parts:
                conversation_parts.append(f"[{role}]: {' '.join(text_parts)}")

    conversation_text = "\n\n".join(conversation_parts)

    # Build prompt
    user_prompt = SUMMARIZE_USER_PROMPT.format(
        conversation=conversation_text,
        custom_instructions=custom_instructions or "No additional instructions.",
        previous_summary=previous_summary or "No previous context.",
    )

    # Call LLM
    response = await provider.chat(
        messages=[
            {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model=model,
        max_tokens=reserve_tokens,
    )

    return response.content or DEFAULT_SUMMARY_FALLBACK


async def summarize_chunks(
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    model: str,
    reserve_tokens: int,
    max_chunk_tokens: int,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
) -> str:
    """
    Summarize messages by chunking them first.

    Args:
        messages: Messages to summarize.
        provider: LLM provider.
        model: Model to use.
        reserve_tokens: Tokens to reserve for output.
        max_chunk_tokens: Maximum tokens per chunk.
        custom_instructions: Optional custom instructions.
        previous_summary: Optional previous summary.

    Returns:
        Summary text.
    """
    if not messages:
        return previous_summary or DEFAULT_SUMMARY_FALLBACK

    chunks = chunk_messages_by_max_tokens(messages, max_chunk_tokens)
    summary = previous_summary

    for chunk in chunks:
        summary = await generate_summary(
            chunk,
            provider,
            model,
            reserve_tokens,
            custom_instructions,
            summary,
        )

    return summary or DEFAULT_SUMMARY_FALLBACK


async def summarize_with_fallback(
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    model: str,
    reserve_tokens: int,
    max_chunk_tokens: int,
    context_window: int,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
) -> str:
    """
    Summarize with progressive fallback for handling oversized messages.

    Args:
        messages: Messages to summarize.
        provider: LLM provider.
        model: Model to use.
        reserve_tokens: Tokens to reserve for output.
        max_chunk_tokens: Maximum tokens per chunk.
        context_window: Context window size.
        custom_instructions: Optional custom instructions.
        previous_summary: Optional previous summary.

    Returns:
        Summary text.
    """
    if not messages:
        return previous_summary or DEFAULT_SUMMARY_FALLBACK

    # Try full summarization first
    try:
        return await summarize_chunks(
            messages,
            provider,
            model,
            reserve_tokens,
            max_chunk_tokens,
            custom_instructions,
            previous_summary,
        )
    except Exception as e:
        logger.warning(f"Full summarization failed, trying partial: {e}")

    # Fallback 1: Summarize only small messages, note oversized ones
    small_messages: list[dict[str, Any]] = []
    oversized_notes: list[str] = []

    for msg in messages:
        if is_oversized_for_summary(msg, context_window):
            role = msg.get("role", "message")
            tokens = estimate_message_tokens(msg)
            oversized_notes.append(
                f"[Large {role} (~{tokens // 1000}K tokens) omitted from summary]"
            )
        else:
            small_messages.append(msg)

    if small_messages:
        try:
            partial_summary = await summarize_chunks(
                small_messages,
                provider,
                model,
                reserve_tokens,
                max_chunk_tokens,
                custom_instructions,
                previous_summary,
            )
            notes = "\n\n" + "\n".join(oversized_notes) if oversized_notes else ""
            return partial_summary + notes
        except Exception as e:
            logger.warning(f"Partial summarization also failed: {e}")

    # Final fallback: Just note what was there
    return (
        f"Context contained {len(messages)} messages "
        f"({len(oversized_notes)} oversized). "
        "Summary unavailable due to size limits."
    )


async def summarize_in_stages(
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    model: str,
    reserve_tokens: int,
    max_chunk_tokens: int,
    context_window: int,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
    parts: int = DEFAULT_PARTS,
    min_messages_for_split: int = 4,
) -> str:
    """
    Summarize messages in stages for better quality.

    Splits messages into parts, summarizes each, then merges.

    Args:
        messages: Messages to summarize.
        provider: LLM provider.
        model: Model to use.
        reserve_tokens: Tokens to reserve for output.
        max_chunk_tokens: Maximum tokens per chunk.
        context_window: Context window size.
        custom_instructions: Optional custom instructions.
        previous_summary: Optional previous summary.
        parts: Number of parts to split into.
        min_messages_for_split: Minimum messages needed for splitting.

    Returns:
        Summary text.
    """
    if not messages:
        return previous_summary or DEFAULT_SUMMARY_FALLBACK

    # Strip verbose tool results before summarization
    messages = _strip_tool_results_for_compaction(messages)

    min_messages = max(2, min_messages_for_split)
    total_tokens = estimate_messages_tokens(messages)

    # Use simple summarization for small message sets
    if (
        parts <= 1
        or len(messages) < min_messages
        or total_tokens <= max_chunk_tokens
    ):
        return await summarize_with_fallback(
            messages,
            provider,
            model,
            reserve_tokens,
            max_chunk_tokens,
            context_window,
            custom_instructions,
            previous_summary,
        )

    # Split messages by token share
    splits = [
        chunk for chunk in split_messages_by_token_share(messages, parts) if chunk
    ]

    if len(splits) <= 1:
        return await summarize_with_fallback(
            messages,
            provider,
            model,
            reserve_tokens,
            max_chunk_tokens,
            context_window,
            custom_instructions,
            previous_summary,
        )

    # Summarize each part
    partial_summaries: list[str] = []
    for chunk in splits:
        summary = await summarize_with_fallback(
            chunk,
            provider,
            model,
            reserve_tokens,
            max_chunk_tokens,
            context_window,
            custom_instructions,
            previous_summary=None,  # Don't chain previous for parts
        )
        partial_summaries.append(summary)

    if len(partial_summaries) == 1:
        return partial_summaries[0]

    # Merge partial summaries
    summary_messages = [
        {"role": "user", "content": summary} for summary in partial_summaries
    ]

    merge_instructions = (
        f"{MERGE_SUMMARIES_INSTRUCTIONS}\n\nAdditional focus:\n{custom_instructions}"
        if custom_instructions
        else MERGE_SUMMARIES_INSTRUCTIONS
    )

    return await summarize_with_fallback(
        summary_messages,
        provider,
        model,
        reserve_tokens,
        max_chunk_tokens,
        context_window,
        merge_instructions,
        previous_summary,
    )


async def summarize_messages(
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    model: str,
    context_window: int = 128_000,
    reserve_tokens: int = 4096,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
) -> str:
    """
    High-level API to summarize messages.

    Automatically handles chunking and staging.

    Args:
        messages: Messages to summarize.
        provider: LLM provider.
        model: Model to use.
        context_window: Context window size.
        reserve_tokens: Tokens to reserve for output.
        custom_instructions: Optional custom instructions.
        previous_summary: Optional previous summary.

    Returns:
        Summary text.
    """
    # Calculate max chunk tokens (40% of context window, with safety margin)
    max_chunk_tokens = int(context_window * 0.4 / SAFETY_MARGIN)

    return await summarize_in_stages(
        messages,
        provider,
        model,
        reserve_tokens,
        max_chunk_tokens,
        context_window,
        custom_instructions,
        previous_summary,
    )
