"""Token estimation for messages.

Uses tiktoken with model-aware overheads.  Anthropic models use a
different tokenizer internally but cl100k_base is a close enough
proxy (typically within 5-10% of actual Claude token counts).
"""

from typing import Any

import tiktoken

from flowly.providers.base import (
    PROVIDER_COMPACTION_CHECKPOINT_KEY,
    PROVIDER_REPLAY_KEY,
)

# Cache the encoder
_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    """Get or create the tiktoken encoder."""
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


# Model-specific overheads.  These are empirical values observed
# from provider usage responses.
_MODEL_OVERHEADS: dict[str, dict[str, int]] = {
    "claude": {
        "per_message": 6,      # Claude wraps each message with ~6 formatting tokens
        "image_small": 1600,   # <512px
        "image_medium": 2400,  # 512-1024px
        "image_large": 4800,   # >1024px
        "tool_call": 15,       # function name + args wrapper
    },
    "gpt": {
        "per_message": 4,      # GPT uses ~4 tokens per message
        "image_small": 85,     # low detail
        "image_medium": 170,   # auto detail
        "image_large": 765,    # high detail
        "tool_call": 10,
    },
}

_DEFAULT_OVERHEADS = _MODEL_OVERHEADS["claude"]  # safe default (Claude counts higher)

# Active model — set by AgentLoop at startup
_active_model: str = ""


def set_active_model(model: str) -> None:
    """Set the active model for estimation.  Called once by AgentLoop."""
    global _active_model
    _active_model = model.lower() if model else ""


def _overheads() -> dict[str, int]:
    """Return overheads matching the active model."""
    if "gpt" in _active_model or "o1" in _active_model or "o3" in _active_model:
        return _MODEL_OVERHEADS["gpt"]
    return _DEFAULT_OVERHEADS


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in a text string."""
    if not text:
        return 0
    return len(_get_encoder().encode(text))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate tokens for a single message with model-aware overheads."""
    oh = _overheads()
    tokens = oh["per_message"]

    # Content
    content = message.get("content", "")
    if isinstance(content, str):
        tokens += estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    tokens += estimate_tokens(part.get("text", ""))
                elif part.get("type") == "image_url":
                    tokens += oh["image_medium"]

    # Tool calls
    tool_calls = message.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            if isinstance(tc, dict):
                func = tc.get("function", {})
                tokens += estimate_tokens(func.get("name", ""))
                tokens += estimate_tokens(func.get("arguments", ""))
                tokens += oh["tool_call"]

    # Stateless Responses requests replay encrypted reasoning/output items.
    # Count their actual wire strings conservatively so a long reasoning chain
    # cannot make the request materially larger than the local budget reports.
    replay = message.get(PROVIDER_REPLAY_KEY)
    replay_items = replay.get("items") if isinstance(replay, dict) else None
    if isinstance(replay_items, list):
        for item in replay_items:
            if not isinstance(item, dict):
                continue
            encrypted = item.get("encrypted_content")
            if isinstance(encrypted, str):
                tokens += estimate_tokens(encrypted)
            summary = item.get("summary")
            if isinstance(summary, list):
                for part in summary:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str):
                            tokens += estimate_tokens(text)

    return tokens


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """
    Estimate total tokens for a list of messages.

    Args:
        messages: List of message dicts.

    Returns:
        Total estimated token count.
    """
    checkpoint_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], dict)
            and messages[index].get(PROVIDER_COMPACTION_CHECKPOINT_KEY)
        ),
        None,
    )
    if checkpoint_index is None:
        return sum(estimate_message_tokens(msg) for msg in messages)

    # The latest encrypted checkpoint replaces earlier conversational input,
    # but the Responses ``instructions`` field is rebuilt from system rows on
    # every call. Price those instructions plus only the post-checkpoint tail.
    # Opaque encrypted bytes are not text tokens and deliberately contribute
    # just one protocol-row overhead through estimate_message_tokens(marker).
    instruction_tokens = sum(
        estimate_message_tokens(message)
        for message in messages[:checkpoint_index]
        if message.get("role") in {"system", "developer"}
    )
    tail_tokens = sum(
        estimate_message_tokens(message)
        for message in messages[checkpoint_index:]
    )
    return instruction_tokens + tail_tokens
