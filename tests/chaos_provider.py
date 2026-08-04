"""A provider that misbehaves the way real ones do.

Every compaction defect found in live use came from the same blind spot: the
code assumed the provider would answer, answer in time, and answer with
content. It does not. Observed within one afternoon on a single install:

  * a call that sat on the wire for 6.5 minutes (internal retries riding a
    slow upstream) while a session lock was held inside the user's turn;
  * an empty body, because on a reasoning model ``max_tokens`` bounds hidden
    thinking and the answer together and the thinking ate all of it;
  * an error envelope delivered as ordinary assistant content with
    ``finish_reason="stop"``, indistinguishable from a real reply;
  * a token count 13% below what the provider actually charged.

Tests import these so new code meets those behaviours before a user does.
Each mode is a fact we were taught, not a hypothetical.

Usage::

    provider = ChaosProvider(mode="empty")
    with pytest.raises(CompactionError):
        await service.compact(history)
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from flowly.providers.base import LLMResponse

ChaosMode = Literal[
    "healthy",
    "empty",             # returns "" — reasoning ate the output budget
    "whitespace",        # returns "   " — empty once stripped
    "reasoning_only",    # the whole answer is a <think> block
    "error_envelope",    # failure delivered as content, finish_reason="stop"
    "error_finish",      # failure with finish_reason="error"
    "hang",              # never returns
    "slow",              # returns, but far too late
    "raise",             # raises out of chat()
    "truncated",         # finish_reason="length", body cut mid-sentence
    "flaky",             # fails the first N calls, then succeeds
]

# Long enough that any sane per-call bound trips first. A test that waits this
# long has found a missing timeout.
HANG_SECONDS = 3600.0


class ChaosProvider:
    """Drop-in ``LLMProvider`` whose failure mode is chosen up front.

    Records every call so a test can assert on retry counts, output budgets
    and prompt content without a second stub.
    """

    provider_name = "chaos"

    def __init__(
        self,
        mode: ChaosMode = "healthy",
        *,
        summary: str = "The conversation covered a long debugging session.",
        slow_seconds: float = 5.0,
        fail_first: int = 1,
        understate_tokens_by: float = 0.0,
    ) -> None:
        self.mode = mode
        self.summary = summary
        self.slow_seconds = slow_seconds
        self.fail_first = fail_first
        # Fraction by which reported usage UNDERSTATES the real request, for
        # tests that exercise estimate-vs-truth drift. 0.13 reproduces the
        # live reading (estimate ~72K, provider counted 82K).
        self.understate_tokens_by = understate_tokens_by

        self.calls = 0
        self.max_tokens_seen: list[int] = []
        self.prompts: list[str] = []
        self.cancelled = False

    # ── LLMProvider surface ───────────────────────────────────────────────

    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        self.max_tokens_seen.append(int(kwargs.get("max_tokens") or 0))
        for message in kwargs.get("messages") or (args[0] if args else []):
            content = message.get("content")
            if isinstance(content, str):
                self.prompts.append(content)

        mode = self.mode
        if mode == "flaky" and self.calls > self.fail_first:
            mode = "healthy"

        if mode in ("hang", "slow"):
            delay = HANG_SECONDS if mode == "hang" else self.slow_seconds
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                # Proof the caller cut the call off rather than waiting it out
                # — the difference between an abandoned request (still holding
                # its connection) and a cancelled one.
                self.cancelled = True
                raise

        return self._response(mode)

    def _response(self, mode: ChaosMode) -> LLMResponse:
        if mode == "raise":
            raise RuntimeError("provider exploded")
        if mode == "empty":
            return LLMResponse(content="", finish_reason="length")
        if mode == "whitespace":
            return LLMResponse(content="   \n  ", finish_reason="stop")
        if mode == "reasoning_only":
            return LLMResponse(
                content="<think>I should summarise this…</think>",
                finish_reason="stop",
            )
        if mode == "error_envelope":
            # The nastiest shape: a failure that looks exactly like an answer.
            # Committed as a summary, it replaces the user's whole history
            # with the words "Error calling LLM: …".
            return LLMResponse(
                content="Error calling LLM: upstream 502",
                finish_reason="stop",
            )
        if mode == "error_finish":
            return LLMResponse(
                content="Error calling LLM: rate limited", finish_reason="error",
            )
        if mode == "truncated":
            return LLMResponse(content=self.summary[:20], finish_reason="length")
        return LLMResponse(content=self.summary, finish_reason="stop")

    # ── Helpers for trigger/accounting tests ──────────────────────────────

    def reported_usage(self, real_prompt_tokens: int) -> dict[str, int]:
        """What this provider CLAIMS a request cost, given its real size.

        With ``understate_tokens_by`` set, the reported number is lower than
        the truth — the drift that let a session sit "under budget" while its
        real requests already exceeded the window.
        """
        factor = max(0.0, 1.0 - self.understate_tokens_by)
        return {
            "prompt_tokens": int(real_prompt_tokens * factor),
            "completion_tokens": 0,
            "total_tokens": int(real_prompt_tokens * factor),
        }


# Modes that must NEVER produce a committed summary. Parametrize over this in
# any test that commits summariser output somewhere durable.
DESTRUCTIVE_MODES: tuple[ChaosMode, ...] = (
    "empty",
    "whitespace",
    "reasoning_only",
    "error_envelope",
    "error_finish",
    "raise",
)
