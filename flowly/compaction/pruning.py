"""Message pruning and chunking utilities."""

from typing import Any

from flowly.compaction.estimator import estimate_messages_tokens, estimate_message_tokens
from flowly.compaction.types import (
    BASE_CHUNK_RATIO,
    MIN_CHUNK_RATIO,
    SAFETY_MARGIN,
    DEFAULT_PARTS,
)


def normalize_parts(parts: int, message_count: int) -> int:
    """Normalize parts count to valid range."""
    if parts <= 1:
        return 1
    return min(max(1, parts), max(1, message_count))


def split_messages_by_token_share(
    messages: list[dict[str, Any]],
    parts: int = DEFAULT_PARTS,
) -> list[list[dict[str, Any]]]:
    """
    Split messages into chunks by token share.

    Args:
        messages: List of messages to split.
        parts: Number of parts to split into.

    Returns:
        List of message chunks.
    """
    if not messages:
        return []

    normalized_parts = normalize_parts(parts, len(messages))
    if normalized_parts <= 1:
        return [messages]

    total_tokens = estimate_messages_tokens(messages)
    target_tokens = total_tokens / normalized_parts
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0

    for message in messages:
        message_tokens = estimate_message_tokens(message)
        if (
            len(chunks) < normalized_parts - 1
            and current
            and current_tokens + message_tokens > target_tokens
        ):
            chunks.append(current)
            current = []
            current_tokens = 0

        current.append(message)
        current_tokens += message_tokens

    if current:
        chunks.append(current)

    return chunks


def chunk_messages_by_max_tokens(
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> list[list[dict[str, Any]]]:
    """
    Chunk messages by maximum token count per chunk.

    Args:
        messages: List of messages to chunk.
        max_tokens: Maximum tokens per chunk.

    Returns:
        List of message chunks.
    """
    if not messages:
        return []

    chunks: list[list[dict[str, Any]]] = []
    current_chunk: list[dict[str, Any]] = []
    current_tokens = 0

    for message in messages:
        message_tokens = estimate_message_tokens(message)

        if current_chunk and current_tokens + message_tokens > max_tokens:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

        current_chunk.append(message)
        current_tokens += message_tokens

        # Split oversized messages to avoid unbounded chunk growth
        if message_tokens > max_tokens:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def compute_adaptive_chunk_ratio(
    messages: list[dict[str, Any]],
    context_window: int,
) -> float:
    """
    Compute adaptive chunk ratio based on average message size.

    When messages are large, use smaller chunks to avoid exceeding limits.

    Args:
        messages: List of messages.
        context_window: Context window size in tokens.

    Returns:
        Chunk ratio (0.15 to 0.4).
    """
    if not messages:
        return BASE_CHUNK_RATIO

    total_tokens = estimate_messages_tokens(messages)
    avg_tokens = total_tokens / len(messages)

    # Apply safety margin for estimation inaccuracy
    safe_avg_tokens = avg_tokens * SAFETY_MARGIN
    avg_ratio = safe_avg_tokens / context_window

    # If average message is > 10% of context, reduce chunk ratio
    if avg_ratio > 0.1:
        reduction = min(avg_ratio * 2, BASE_CHUNK_RATIO - MIN_CHUNK_RATIO)
        return max(MIN_CHUNK_RATIO, BASE_CHUNK_RATIO - reduction)

    return BASE_CHUNK_RATIO


def is_oversized_for_summary(
    message: dict[str, Any],
    context_window: int,
) -> bool:
    """
    Check if a single message is too large to summarize.

    If single message > 50% of context, it can't be summarized safely.

    Args:
        message: Message to check.
        context_window: Context window size in tokens.

    Returns:
        True if message is oversized.
    """
    tokens = estimate_message_tokens(message) * SAFETY_MARGIN
    return tokens > context_window * 0.5


def prune_history_for_context_share(
    messages: list[dict[str, Any]],
    max_context_tokens: int,
    max_history_share: float = 0.5,
    parts: int = DEFAULT_PARTS,
) -> dict[str, Any]:
    """
    Prune history to fit within context share budget.

    Args:
        messages: List of messages to prune.
        max_context_tokens: Maximum context window tokens.
        max_history_share: Maximum share of context for history (0.1-0.9).
        parts: Number of parts for splitting.

    Returns:
        Dict with:
            - messages: Kept messages
            - dropped_messages_list: Dropped messages
            - dropped_chunks: Number of chunks dropped
            - dropped_messages: Number of messages dropped
            - dropped_tokens: Tokens in dropped messages
            - kept_tokens: Tokens in kept messages
            - budget_tokens: Token budget
    """
    budget_tokens = max(1, int(max_context_tokens * max_history_share))
    kept_messages = list(messages)
    all_dropped_messages: list[dict[str, Any]] = []
    dropped_chunks = 0
    dropped_messages_count = 0
    dropped_tokens = 0

    normalized_parts = normalize_parts(parts, len(kept_messages))

    while kept_messages and estimate_messages_tokens(kept_messages) > budget_tokens:
        chunks = split_messages_by_token_share(kept_messages, normalized_parts)
        if len(chunks) <= 1:
            break

        # Drop oldest chunk
        dropped, *rest = chunks
        dropped_chunks += 1
        dropped_messages_count += len(dropped)
        dropped_tokens += estimate_messages_tokens(dropped)
        all_dropped_messages.extend(dropped)

        # Flatten remaining chunks
        kept_messages = [msg for chunk in rest for msg in chunk]

    return {
        "messages": kept_messages,
        "dropped_messages_list": all_dropped_messages,
        "dropped_chunks": dropped_chunks,
        "dropped_messages": dropped_messages_count,
        "dropped_tokens": dropped_tokens,
        "kept_tokens": estimate_messages_tokens(kept_messages),
        "budget_tokens": budget_tokens,
    }
