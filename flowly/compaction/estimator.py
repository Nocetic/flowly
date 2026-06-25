"""Token estimation for messages.

Uses tiktoken with model-aware overheads.  Anthropic models use a
different tokenizer internally but cl100k_base is a close enough
proxy (typically within 5-10% of actual Claude token counts).
"""

import tiktoken
from typing import Any

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

    return tokens


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """
    Estimate total tokens for a list of messages.

    Args:
        messages: List of message dicts.

    Returns:
        Total estimated token count.
    """
    return sum(estimate_message_tokens(msg) for msg in messages)
