"""Message pruning and chunking utilities."""

from typing import Any

from flowly.compaction.estimator import estimate_message_tokens, estimate_messages_tokens
from flowly.compaction.types import (
    BASE_CHUNK_RATIO,
    DEFAULT_PARTS,
    MIN_CHUNK_RATIO,
    SAFETY_MARGIN,
)


def split_into_turn_blocks(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group messages into atomic turn blocks.

    A block is one user message plus every assistant/tool message that
    answers it. Anything preceding the first user message (a leading
    system prompt, a compaction summary) forms its own leading block.

    Blocks are the safe unit for keeping or dropping history: an
    ``assistant`` message carrying ``tool_calls`` and the ``tool``
    results answering it must travel together or the provider rejects
    the sequence.
    """
    blocks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "user" and current:
            blocks.append(current)
            current = []
        current.append(msg)
    if current:
        blocks.append(current)
    return blocks


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

    expanded: list[dict[str, Any]] = []
    for message in messages:
        expanded.extend(_split_oversized_text_message(message, max_tokens))

    for message in expanded:
        message_tokens = estimate_message_tokens(message)

        if current_chunk and current_tokens + message_tokens > max_tokens:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

        current_chunk.append(message)
        current_tokens += message_tokens

        # Non-text payloads can still be indivisible (for example one image or
        # a large structured tool call). Keep those atomic; string content was
        # already split above without dropping a byte.
        if message_tokens > max_tokens:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _split_oversized_text_message(
    message: dict[str, Any],
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Split one huge text message into lossless summary-only fragments.

    Message-count chunking alone cannot bound a single pasted log or document.
    A provider then rejects the *summarizer* request before compaction can save
    the main turn. Character boundaries are selected with the real estimator
    and never persisted, so every source character remains represented while
    each fragment fits the summarizer budget.
    """
    if max_tokens <= 0 or estimate_message_tokens(message) <= max_tokens:
        return [message]
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return [message]

    fragments: list[dict[str, Any]] = []
    start = 0
    while start < len(content):
        low = start + 1
        high = len(content)
        best = start
        while low <= high:
            mid = (low + high) // 2
            candidate = dict(message)
            candidate["content"] = content[start:mid]
            if fragments:
                # Tool calls describe the assistant message once; duplicating
                # them on every text fragment would fabricate repeated actions.
                candidate.pop("tool_calls", None)
            if estimate_message_tokens(candidate) <= max_tokens:
                best = mid
                low = mid + 1
            else:
                high = mid - 1

        if best <= start:
            # The text is not the oversized component (for example enormous
            # tool-call arguments). The transcript renderer bounds those
            # separately, so retain this atomic message rather than lose it.
            return [message]

        end = best
        if end < len(content):
            search_floor = start + max(1, int((end - start) * 0.8))
            newline = content.rfind("\n", search_floor, end)
            space = content.rfind(" ", search_floor, end)
            boundary = max(newline, space)
            if boundary > start:
                # Keep the separator in one fragment so joining fragment
                # contents reconstructs the original byte-for-byte.
                end = boundary + 1

        fragment = dict(message)
        fragment["content"] = content[start:end]
        if fragments:
            fragment.pop("tool_calls", None)
        fragments.append(fragment)
        start = end

    return fragments


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

# ---------------------------------------------------------------------------
# Tool-result truncation shared by the main loop, the subagent loop and the
# subagent checkpoint replay. Pure functions of their arguments: a replay
# that calls them with the recorded parameters rebuilds the same list.
# ---------------------------------------------------------------------------

# (after iteration, keep_last, max_old_chars): from iteration 6 the old tool
# results of a long tool loop are cut to 300 chars keeping the last 3 whole,
# from iteration 9 to 150 keeping 2. Late enough to keep a multi-step browser
# flow's context.
ITERATION_STRIP_SCHEDULE: tuple[tuple[int, int, int], ...] = ((5, 3, 300), (8, 2, 150))


def iteration_strip_passes(iteration: int) -> list[tuple[int, int]]:
    """The ``(keep_last, max_old_chars)`` passes a tool loop applies at ``iteration``."""
    return [(keep, chars) for after, keep, chars in ITERATION_STRIP_SCHEDULE if iteration > after]


def microcompact_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_recent_full: int,
    truncate_chars: int,
) -> list[dict[str, Any]]:
    """Cut every tool result but the newest ``keep_recent_full`` to ``truncate_chars``.

    A spilled tool result carries a file pointer at the END of its text —
    exactly where truncation cuts — so it is re-appended and the full output
    stays reachable via read_file. Returns ``messages`` itself when nothing
    changes, a new list otherwise. Idempotent for the same parameters.
    """
    # Lazy import — flowly.agent imports this package at module load.
    from flowly.agent.tool_result_spill import extract_spill_pointer

    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep_recent_full:
        return messages
    truncate = set(tool_indices[:-keep_recent_full])
    result: list[dict[str, Any]] = []
    changed = False
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if i in truncate and isinstance(content, str) and len(content) > truncate_chars:
            truncated = content[:truncate_chars] + "\n[old tool output truncated]"
            pointer = extract_spill_pointer(content)
            if pointer and pointer not in truncated:
                truncated += pointer
            if truncated != content:
                result.append({**msg, "content": truncated})
                changed = True
                continue
        result.append(msg)
    return result if changed else messages


def strip_old_tool_results(
    messages: list[dict[str, Any]],
    keep_last: int = 3,
    max_old_chars: int = 200,
    keep_last_images: int = 1,
) -> list[dict[str, Any]]:
    """Truncate old tool results, keeping only the last N in full.

    Two separate retention windows:
      - keep_last: how many recent text-only tool results to keep in
        full. Older ones get truncated to max_old_chars.
      - keep_last_images: how many recent image-bearing tool results
        (browser_tab screenshots) to keep visually. Older ones drop
        the image content block entirely; the text part stays. One
        screenshot is ~10-15K tokens, so keeping more than 1-2 in
        context blows past the model's window in a few turns.

    Returns a NEW list to avoid mutating session-shared message dicts (Fix #11).
    The main loop and the subagent loop both call it (``iteration_strip_passes``),
    and a subagent checkpoint replays it, so it must stay a pure function.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if not tool_indices:
        return messages

    # Identify which tool messages carry an image block. For these we
    # keep only the most recent N visually; older ones lose the image.
    image_indices = [
        i for i in tool_indices
        if isinstance(messages[i].get("content"), list)
        and any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in messages[i].get("content", [])
        )
    ]
    image_to_strip = set(image_indices[:-keep_last_images]) if len(image_indices) > keep_last_images else set()

    # Text-truncate window (existing behavior).
    to_truncate = set(tool_indices[:-keep_last]) if len(tool_indices) > keep_last else set()

    if not to_truncate and not image_to_strip:
        return messages

    result = list(messages)
    for i in to_truncate | image_to_strip:
        msg = result[i]
        content = msg.get("content", "")

        if i in image_to_strip and isinstance(content, list):
            # Drop image blocks; keep text. If a text block is also old
            # enough to truncate, do that to its text in the same pass.
            new_content_parts: list[dict[str, Any]] = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    continue  # drop the image
                if isinstance(p, dict) and p.get("type") == "text":
                    txt = p.get("text", "")
                    if i in to_truncate and len(txt) > max_old_chars:
                        txt = txt[:max_old_chars] + "\n[earlier result truncated, image dropped]"
                    new_content_parts.append({"type": "text", "text": txt})
                else:
                    new_content_parts.append(p)
            # If only one text block left, collapse to plain string for
            # providers that prefer string content on tool messages.
            if len(new_content_parts) == 1 and new_content_parts[0].get("type") == "text":
                result[i] = {**msg, "content": new_content_parts[0]["text"]}
            else:
                result[i] = {**msg, "content": new_content_parts}
        elif i in to_truncate and isinstance(content, str) and len(content) > max_old_chars:
            result[i] = {**msg, "content": content[:max_old_chars] + "\n[earlier result truncated]"}

    return result
