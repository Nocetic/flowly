"""LLM error classification and user-safe provider error presentation.

A compact taxonomy that matches what the agent retry loops and clients
can actually ACT on. Kept deliberately coarse because Flowly's
provider layer is just a single ``LLMResponse`` with no structured
status code. Explicit terminal product states are separated where callers
can offer a concrete recovery action:

    * rate_limit      → long jittered backoff, retry
    * context_overflow → no retry, surface as error so the outer layer
                         can compact and respawn
    * auth            → no retry, fail fast (retries don't fix bad keys)
    * insufficient_credits → no retry, billing action required
    * image_input_unsupported → no retry, choose vision model/remove image
    * transient       → short jittered backoff, retry

Pattern matching uses the same phrase catalogs the key rotator already
relies on (`flowly.providers.key_rotator.classify_error` +
`is_context_overflow`), so a future provider-level upgrade (status_code
preservation) will upgrade every caller at once.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from flowly.providers.key_rotator import classify_error, is_context_overflow

if TYPE_CHECKING:
    from flowly.providers.base import LLMResponse


class ErrorCategory(str, Enum):
    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    AUTH = "auth"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    IMAGE_INPUT_UNSUPPORTED = "image_input_unsupported"
    TRANSIENT = "transient"


# HTTP 402 / "out of credits" from the Flowly proxy's CREDITS_V2 ledger. This is
# a HARD stop for the USER's account balance — NOT a transient fault and NOT a
# per-key issue, so neither retrying nor rotating keys can clear it. Matched on
# the machine-readable code first (most reliable), then human phrasings and the
# bare status. Without this the subagent retry loop treats it as TRANSIENT and
# fires a backoff-retry burst against an account that can't pay for any of it.
_INSUFFICIENT_CREDITS_PHRASES = (
    "insufficient_credits",   # backend error type/code (machine-readable)
    "insufficient credits",   # human message
    "payment required",       # HTTP 402 reason phrase
    "402",                    # bare status code in a wrapped SDK error string
    "out of credits",
)


# Provider SDKs wrap this failure in several different shapes (plain text,
# OpenAI-style JSON, APIStatusError repr). Match the capability statement,
# not a provider/status code: OpenRouter currently returns 404 while other
# OpenAI-compatible endpoints use 400 or 422 for the same terminal condition.
_IMAGE_INPUT_UNSUPPORTED_PHRASES = (
    "no endpoints found that support image input",
    "does not support image input",
    "doesn't support image input",
    "do not support image input",
    "image input is not supported",
    "image inputs are not supported",
    "images are not supported",
    "model does not support images",
    "model doesn't support images",
    "vision input is not supported",
    "image modality is not supported",
    "input modality image is not supported",
    "image_url is only supported",
    "unsupported image input",
    "unsupported content type: image",
    "unsupported content type image",
)


@dataclass(frozen=True)
class ProviderErrorPresentation:
    """Stable, user-safe error contract sent to first-party clients.

    ``detail`` is deliberately absent: raw SDK/provider payloads can contain
    request internals and are too volatile for UI copy. They remain available
    in server logs for diagnostics.
    """

    code: str
    title: str
    message: str
    retryable: bool
    category: ErrorCategory

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "code": self.code,
            "title": self.title,
            "message": self.message,
            "retryable": self.retryable,
            "category": self.category.value,
        }


def is_insufficient_credits(error_msg: str) -> bool:
    """True when the error is a hard out-of-credits (HTTP 402) stop."""
    lowered = error_msg.lower()
    return any(p in lowered for p in _INSUFFICIENT_CREDITS_PHRASES)


def is_image_input_unsupported(error_msg: str) -> bool:
    """True only for an explicit provider statement rejecting image input."""
    lowered = error_msg.lower()
    return any(p in lowered for p in _IMAGE_INPUT_UNSUPPORTED_PHRASES)


def classify_response(response: "LLMResponse") -> ErrorCategory:
    """Classify an error-carrying LLMResponse into a recovery category.

    Callers should only invoke this when ``finish_reason == "error"``;
    the content field is the only signal we have (providers collapse
    HTTP status codes into a string message in `_error_response`).
    Safe default is TRANSIENT so an unrecognised error still gets a
    retry rather than failing immediately.
    """
    msg = response.content or ""
    # Checked first: a 402 is terminal regardless of any other phrasing the
    # wrapped error string might happen to contain.
    if is_insufficient_credits(msg):
        return ErrorCategory.INSUFFICIENT_CREDITS
    if is_image_input_unsupported(msg):
        return ErrorCategory.IMAGE_INPUT_UNSUPPORTED
    if is_context_overflow(msg):
        return ErrorCategory.CONTEXT_OVERFLOW
    reason = classify_error(msg)
    if reason == "auth_error":
        return ErrorCategory.AUTH
    if reason == "rate_limit":
        return ErrorCategory.RATE_LIMIT
    # "overload" and everything else → treat as transient + retry.
    return ErrorCategory.TRANSIENT


def present_provider_error(response: "LLMResponse") -> ProviderErrorPresentation:
    """Convert a provider failure into stable product copy + machine metadata."""
    category = classify_response(response)
    if category == ErrorCategory.IMAGE_INPUT_UNSUPPORTED:
        return ProviderErrorPresentation(
            code="MODEL_IMAGE_INPUT_UNSUPPORTED",
            title="This model can't read images",
            message=(
                "Choose a vision-capable model or remove the image, then try again. "
                "No action was taken."
            ),
            retryable=False,
            category=category,
        )
    if category == ErrorCategory.CONTEXT_OVERFLOW:
        return ProviderErrorPresentation(
            code="MODEL_CONTEXT_LIMIT_EXCEEDED",
            title="This conversation is too large",
            message="Start a new chat or compact the conversation, then try again.",
            retryable=False,
            category=category,
        )
    if category == ErrorCategory.AUTH:
        return ProviderErrorPresentation(
            code="PROVIDER_AUTHENTICATION_FAILED",
            title="Model provider needs attention",
            message="Check the provider connection or API key in Settings, then try again.",
            retryable=False,
            category=category,
        )
    if category == ErrorCategory.INSUFFICIENT_CREDITS:
        return ProviderErrorPresentation(
            code="INSUFFICIENT_CREDITS",
            title="Usage credits are exhausted",
            message="Top up or upgrade your plan to continue.",
            retryable=False,
            category=category,
        )
    if category == ErrorCategory.RATE_LIMIT:
        return ProviderErrorPresentation(
            code="MODEL_RATE_LIMITED",
            title="The model is busy",
            message="Wait a moment and try again.",
            retryable=True,
            category=category,
        )
    return ProviderErrorPresentation(
        code="MODEL_PROVIDER_UNAVAILABLE",
        title="The model couldn't respond",
        message="Try again. If this continues, choose another model or provider.",
        retryable=True,
        category=category,
    )


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Decorrelated exponential backoff with jitter.

    ``attempt`` is 1-indexed (first retry = 1). Returns seconds.
    The jitter is seeded from wall-clock nanoseconds so concurrent
    sessions hitting the same rate limit don't synchronise their retry
    windows (thundering-herd).
    """
    attempt = max(1, attempt)
    if base_delay <= 0:
        return max_delay
    exponent = min(attempt - 1, 63)
    delay = min(base_delay * (2 ** exponent), max_delay)
    seed = (time.time_ns() ^ (attempt * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)
    return delay + jitter


def backoff_for(category: ErrorCategory, attempt: int) -> float | None:
    """Recovery policy for each category.

    Returns the number of seconds to wait before retrying, or ``None``
    if the category is not retryable (caller should exit the loop with
    a clear error message).
    """
    if category == ErrorCategory.CONTEXT_OVERFLOW:
        return None  # retrying with same context won't help
    if category == ErrorCategory.AUTH:
        return None  # same key will still be rejected
    if category == ErrorCategory.INSUFFICIENT_CREDITS:
        return None  # account is out of credits — no retry/rotation clears it
    if category == ErrorCategory.IMAGE_INPUT_UNSUPPORTED:
        return None  # same model + same image will be rejected again
    if category == ErrorCategory.RATE_LIMIT:
        # Long windows, jittered — anti thundering-herd.
        return jittered_backoff(attempt, base_delay=30.0, max_delay=120.0)
    # TRANSIENT: keep current 5/10/20-ish shape but jittered.
    return jittered_backoff(attempt, base_delay=5.0, max_delay=30.0)
