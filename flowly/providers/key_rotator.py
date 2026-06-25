"""API key rotator — cycles through multiple keys when one fails.

Usage:
    rotator = KeyRotator(keys=["sk-key1", "sk-key2", "sk-key3"], provider="anthropic")
    key = rotator.current_key()
    # ... call fails with auth/rate-limit error ...
    next_key = rotator.rotate(reason="rate_limit")

Keys are marked with a cooldown on failure; after cooldown_seconds they
become eligible again.  If ALL keys are in cooldown, the rotator returns
the least-recently-failed key rather than raising.
"""

import time
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger


_DEFAULT_COOLDOWN_S = 60  # 1 minute before re-trying a failed key


@dataclass
class _KeyState:
    key: str
    index: int
    fail_count: int = 0
    cooldown_until: float = 0.0  # epoch seconds

    def is_available(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    def mark_failed(self, cooldown_s: float) -> None:
        self.fail_count += 1
        self.cooldown_until = time.monotonic() + cooldown_s


class KeyRotator:
    """Thread-safe (asyncio-safe) API key rotator.

    Args:
        keys: List of API keys to rotate through.  May contain a single key.
        provider: Provider name (used for logging/audit).
        cooldown_seconds: How long to back off a failed key.
        on_rotate: Optional callback(provider, reason, from_idx, to_idx).
    """

    def __init__(
        self,
        keys: list[str],
        provider: str = "unknown",
        cooldown_seconds: float = _DEFAULT_COOLDOWN_S,
        on_rotate: Callable[[str, str, int, int], None] | None = None,
    ):
        if not keys:
            raise ValueError("KeyRotator requires at least one key")
        self._states = [_KeyState(key=k, index=i) for i, k in enumerate(keys)]
        self._provider = provider
        self._cooldown_s = cooldown_seconds
        self._current_idx = 0
        self._on_rotate = on_rotate

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def current_key(self) -> str:
        """Return the currently selected API key."""
        return self._states[self._current_idx].key

    def rotate(self, reason: str = "error") -> str:
        """Mark the current key as failed and return the next available key.

        If no key is available (all in cooldown), returns the key with the
        earliest cooldown expiry instead of raising.
        """
        from_idx = self._current_idx
        self._states[from_idx].mark_failed(self._cooldown_s)

        next_idx = self._pick_next()
        self._current_idx = next_idx

        if next_idx != from_idx:
            logger.warning(
                f"[KeyRotator] {self._provider}: rotating key "
                f"#{from_idx} → #{next_idx} (reason={reason})"
            )
        else:
            logger.warning(
                f"[KeyRotator] {self._provider}: all keys in cooldown, "
                f"re-using key #{next_idx} (reason={reason})"
            )

        if self._on_rotate:
            try:
                self._on_rotate(self._provider, reason, from_idx, next_idx)
            except Exception:
                pass  # Never crash on audit callback

        return self._states[next_idx].key

    def has_multiple_keys(self) -> bool:
        return len(self._states) > 1

    def key_count(self) -> int:
        return len(self._states)

    def available_count(self) -> int:
        return sum(1 for s in self._states if s.is_available())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pick_next(self) -> int:
        """Return the index of the next available key (round-robin)."""
        n = len(self._states)
        for offset in range(1, n + 1):
            idx = (self._current_idx + offset) % n
            if self._states[idx].is_available():
                return idx

        # All keys in cooldown — pick the one with earliest expiry
        return min(
            range(n),
            key=lambda i: self._states[i].cooldown_until,
        )


# ------------------------------------------------------------------
# Helper: detect rotation-worthy errors from LiteLLM error strings
# ------------------------------------------------------------------

_AUTH_PHRASES = frozenset([
    "invalid api key",
    "authentication",
    "unauthorized",
    "401",
    "403 forbidden",
    "permission denied",
    "incorrect api key",
])

_RATE_PHRASES = frozenset([
    "rate limit",
    "ratelimit",
    "too many requests",
    "429",
    "quota exceeded",
    "resource_exhausted",
])

_OVERLOAD_PHRASES = frozenset([
    "overloaded",
    "529",
    "service unavailable",
    "503",
])


def classify_error(error_msg: str) -> str | None:
    """Return rotation reason string if error warrants rotation, else None."""
    lowered = error_msg.lower()
    if any(p in lowered for p in _AUTH_PHRASES):
        return "auth_error"
    if any(p in lowered for p in _RATE_PHRASES):
        return "rate_limit"
    if any(p in lowered for p in _OVERLOAD_PHRASES):
        return "overload"
    return None


def is_context_overflow(error_msg: str) -> bool:
    """Return True if the error is a context-length overflow.

    Also detects Anthropic's 429 "request too large for your tier"
    which means the prompt exceeds the user's context-length tier.
    """
    lowered = error_msg.lower()
    phrases = [
        "context_length_exceeded",
        "maximum context length",
        "too many tokens",
        "prompt is too long",
        "input is too long",
        "reduce the length",
        "reduce your prompt",
        "context window",
        "token limit",
        "request too large",          # Anthropic 429 tier limit
        "context too large",          # Flowly proxy 413 (MAX_INPUT_TOKENS guard)
        "context_too_large",          # same — machine-readable error code
        "exceeds the maximum",        # generic overflow
        "too large for your tier",    # Anthropic specific
    ]
    return any(p in lowered for p in phrases)
