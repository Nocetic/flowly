"""Tests for the CREDITS_V2 out-of-credits (HTTP 402) terminal classification.

When the Flowly proxy's USD credit ledger is depleted it returns 402 with an
``insufficient_credits`` error. The subagent retry loop must treat that as a
HARD stop — retrying or rotating keys can't make an empty account pay, and a
backoff-retry burst against a 402 is pure waste (and noise). These tests pin
that contract on the classifier the loop drives.
"""

from __future__ import annotations

import pytest

from flowly.agent.error_classifier import (
    ErrorCategory,
    backoff_for,
    classify_response,
    is_insufficient_credits,
)
from flowly.providers.base import LLMResponse


def _err(content: str) -> LLMResponse:
    """An error-carrying response as the provider would emit on failure."""
    return LLMResponse(content=content, tool_calls=[], finish_reason="error")


class TestInsufficientCreditsDetection:
    @pytest.mark.parametrize(
        "msg",
        [
            'Error calling LLM: {"error":{"type":"insufficient_credits"}}',
            "Insufficient credits. Available: $0.00. Please purchase more.",
            "HTTP 402 Payment Required",
            "You are out of credits",
            "openai.APIStatusError: Error code: 402 - insufficient_credits",
        ],
    )
    def test_recognised_as_insufficient_credits(self, msg: str) -> None:
        assert is_insufficient_credits(msg) is True
        assert classify_response(_err(msg)) is ErrorCategory.INSUFFICIENT_CREDITS

    def test_insufficient_credits_is_terminal(self) -> None:
        # None backoff == do not retry.
        assert backoff_for(ErrorCategory.INSUFFICIENT_CREDITS, 1) is None
        assert backoff_for(ErrorCategory.INSUFFICIENT_CREDITS, 5) is None


class TestNoFalsePositives:
    @pytest.mark.parametrize(
        "msg",
        [
            "rate limit exceeded, please slow down",
            "401 invalid api key",
            "maximum context length exceeded",
            "the server is overloaded (503)",
            "some unknown transient blip",
        ],
    )
    def test_other_errors_not_misread_as_credits(self, msg: str) -> None:
        assert is_insufficient_credits(msg) is False
        assert classify_response(_err(msg)) is not ErrorCategory.INSUFFICIENT_CREDITS


class TestCreditsTakesPriority:
    def test_402_wins_even_with_other_noise(self) -> None:
        # A wrapped SDK error can carry several status-ish tokens; the hard
        # 402 stop must win so we don't fall through to a retryable bucket.
        msg = "429-ish wording but really: 402 insufficient_credits, service unavailable"
        assert classify_response(_err(msg)) is ErrorCategory.INSUFFICIENT_CREDITS
