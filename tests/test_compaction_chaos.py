"""Compaction against a provider that misbehaves.

Every failure mode in ``tests/chaos_provider.py`` was observed in live use,
not imagined. This file runs the REAL compaction service against each of them
and asserts the one property that matters above all others:

    a conversation is never traded for a summary we failed to produce.

Compaction is the only routine operation that deliberately destroys history.
Everything else can retry; a bad commit here is unrecoverable from the working
context. So the bar is not "usually works" — it is "fails safe, every time,
for every way a provider can let us down".
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.compaction import summarizer as summarizer_module
from flowly.compaction.service import CompactionService
from flowly.compaction.types import CompactionConfig, CompactionError, MemoryFlushConfig
from tests.chaos_provider import DESTRUCTIVE_MODES, ChaosProvider


def _service(provider: ChaosProvider, **overrides) -> CompactionService:
    config = CompactionConfig(
        mode="default",
        context_window=100_000,
        context_window_explicit=True,
        reserve_tokens_floor=3_000,  # the live value that starved the call
        memory_flush=MemoryFlushConfig(enabled=False),
        **overrides,
    )
    return CompactionService(provider=provider, model="chaos/model", config=config)


def _history(turns: int = 20) -> list[dict]:
    filler = "the deploy log shows a retry storm on shard nine "
    out: list[dict] = []
    for i in range(turns):
        out.append({"role": "user", "content": f"q{i} {filler * 6}"})
        out.append({"role": "assistant", "content": f"a{i} {filler * 6}"})
    return out


# ── Fail safe, every mode ─────────────────────────────────────────────────


@pytest.mark.parametrize("mode", DESTRUCTIVE_MODES)
async def test_a_broken_provider_never_produces_a_summary(mode):
    """Not one of these may come back as text a caller would commit."""
    service = _service(ChaosProvider(mode=mode))
    history = _history()
    snapshot = [dict(m) for m in history]

    with pytest.raises(CompactionError):
        await service.compact(history)

    assert history == snapshot, "compaction mutated the caller's history"


@pytest.mark.parametrize("mode", DESTRUCTIVE_MODES)
async def test_the_stateless_form_returns_the_original_messages(mode):
    """``compact_if_needed`` reports failure by handing back what it was
    given — never a partial compaction, never an error string."""
    service = _service(ChaosProvider(mode=mode))
    history = _history()

    result_messages, result = await service.compact_if_needed(history)

    assert result is None
    assert result_messages == history


async def test_an_error_envelope_is_not_mistaken_for_a_summary():
    """The nastiest shape: `finish_reason="stop"` with an error string as the
    body. Committed, it replaces the whole conversation with the words
    'Error calling LLM: …' — which is exactly what happened in production
    before the summary boundary started validating."""
    provider = ChaosProvider(mode="error_envelope")
    service = _service(provider)

    with pytest.raises(CompactionError) as excinfo:
        await service.compact(_history())

    assert "Error calling LLM" not in str(excinfo.value.__cause__ or "")
    assert provider.calls > 0


async def test_a_summary_that_is_only_reasoning_counts_as_empty():
    with pytest.raises(CompactionError):
        await _service(ChaosProvider(mode="reasoning_only")).compact(_history())


# ── Bounded in time, abortable mid-call ───────────────────────────────────


async def test_a_hung_provider_cannot_hold_the_turn(monkeypatch):
    """The live incident: a call sat on the wire for 6.5 minutes while the
    session lock was held INSIDE the user's turn — their message was blocked
    and Stop did nothing. A per-call bound turns that into an ordinary
    compaction failure."""
    monkeypatch.setattr(summarizer_module, "SUMMARY_CALL_TIMEOUT_SECONDS", 0.2)
    provider = ChaosProvider(mode="hang")
    service = _service(provider)

    with pytest.raises(CompactionError, match="timed out"):
        await asyncio.wait_for(service.compact(_history()), timeout=5.0)

    assert provider.cancelled, (
        "the provider call was abandoned but never cancelled — it would keep "
        "running and holding its connection"
    )


async def test_stop_takes_effect_while_the_call_is_still_in_flight():
    """``should_cancel`` used to be polled only BETWEEN round trips, so Stop
    during a slow call changed nothing until the call chose to return."""
    provider = ChaosProvider(mode="slow", slow_seconds=30.0)
    service = _service(provider)
    cancelled = False

    async def cancel_shortly() -> None:
        nonlocal cancelled
        await asyncio.sleep(0.1)
        cancelled = True

    asyncio.create_task(cancel_shortly())

    with pytest.raises(CompactionError, match="cancelled"):
        await asyncio.wait_for(
            service.compact(_history(), should_cancel=lambda: cancelled),
            timeout=5.0,
        )

    assert provider.cancelled


# ── Recovering from the recoverable ───────────────────────────────────────


async def test_an_empty_body_is_retried_with_more_room():
    """On a reasoning model ``max_tokens`` bounds hidden thinking AND the
    answer together, so a small reserve yields an empty body — flakily. One
    retry with doubled room is cheaper than failing the compaction."""
    provider = ChaosProvider(mode="flaky", fail_first=1)
    # "flaky" returns healthy after the first call; make that first call the
    # starved-empty shape by starting in the empty mode.
    provider.mode = "empty"
    original_response = provider._response

    def response(mode):
        # First call empty, later calls healthy — mimics "the reasoning fit
        # this time" rather than a deterministic failure.
        return original_response("healthy" if provider.calls > 1 else "empty")

    provider._response = response  # type: ignore[method-assign]
    service = _service(provider)

    result = await service.compact(_history())

    assert result.summary, "a recoverable emptiness was reported as a failure"
    assert provider.calls == 2, "expected exactly one retry"
    assert provider.max_tokens_seen[1] == provider.max_tokens_seen[0] * 2


async def test_the_output_budget_is_never_the_bare_reserve():
    """A 3000-token reserve is a sane reply budget and a starvation budget for
    a summary on a reasoning model. The floor is what stops the configured
    value being handed straight to the provider."""
    provider = ChaosProvider(mode="healthy")
    service = _service(provider)

    await service.compact(_history())

    assert provider.max_tokens_seen, "no call was made"
    assert min(provider.max_tokens_seen) >= summarizer_module.MIN_SUMMARY_OUTPUT_TOKENS


async def test_a_healthy_provider_still_compacts():
    """The guard rails must not have made the happy path unreachable."""
    service = _service(ChaosProvider(mode="healthy"))
    history = _history()

    result = await service.compact(history)

    assert result.tokens_after < result.tokens_before
    assert result.messages_removed > 0
    assert result.summary


# ── Accounting drift ──────────────────────────────────────────────────────


def test_the_trigger_believes_the_provider_over_the_estimate():
    """Reported usage understating the truth is how a session sat 'under
    budget' while its real requests already exceeded the window."""
    provider = ChaosProvider(mode="healthy", understate_tokens_by=0.13)
    service = _service(provider)
    real_request = 82_000

    reported = provider.reported_usage(real_request)
    assert reported["total_tokens"] < real_request  # the drift, made explicit

    # Estimate says there is room; the provider's own count says otherwise.
    assert service.should_compact(
        5_000, "s", overhead_tokens=0,
        observed_total_tokens=service.compaction_threshold + 1,
    )
