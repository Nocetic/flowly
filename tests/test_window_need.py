"""How large a context window the agent needs (flowly/compaction/window_need.py).

Measured, not guessed: the fixed overhead every request carries (system
instructions and tool schemas) was 55-68K tokens on a real Desktop agent on
2026-11-06, so a fixed "32K is enough" let an 8K local model through.
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.compaction import window_need
from flowly.compaction.window_need import (
    FLOOR, MIN_CONVERSATION_ROOM, UNMEASURED_REQUIRED, load_size, observed_fixed_overhead, record_fixed_overhead,
    required_context, required_window,
)


@pytest.fixture(autouse=True)
def _clean():
    window_need.reset_for_tests()
    yield
    window_need.reset_for_tests()


def test_the_need_is_the_overhead_the_reply_and_room_to_talk():
    need = required_window(68_013, 20_000)
    assert need >= 68_013 + 20_000 + MIN_CONVERSATION_ROOM
    assert need % 1024 == 0 and need - (68_013 + 20_000 + MIN_CONVERSATION_ROOM) < 1024
    # A lean agent still gets a sane floor.
    assert required_window(1_000, 2_000) == FLOOR


def test_before_any_request_the_need_is_the_cautious_default():
    assert observed_fixed_overhead() is None
    assert required_context(20_000) == (UNMEASURED_REQUIRED, False)


def test_the_largest_recent_overhead_counts():
    for tokens in (12_000, 68_013, 55_619):
        record_fixed_overhead(tokens)
    assert observed_fixed_overhead() == 68_013
    need, measured = required_context(20_000)
    assert measured is True and need == required_window(68_013, 20_000)


def test_old_observations_age_out_of_the_window():
    record_fixed_overhead(90_000)
    for _ in range(window_need._KEEP):
        record_fixed_overhead(10_000)
    assert observed_fixed_overhead() == 10_000


def test_a_restarted_gateway_still_knows_the_need():
    record_fixed_overhead(55_619)
    record_fixed_overhead(68_013)
    # A new process: memory gone, the file left.
    with window_need._lock:
        window_need._observed.clear()
        window_need._seeded = False
        window_need._persisted = None
    assert observed_fixed_overhead() == 68_013
    assert required_context(20_000) == (required_window(68_013, 20_000), True)


def test_the_file_is_written_only_when_the_largest_figure_changes(monkeypatch):
    writes: list[int] = []
    real = window_need._persist_locked

    def counting(value):
        if value != window_need._persisted:
            writes.append(value)
        real(value)

    monkeypatch.setattr(window_need, "_persist_locked", counting)
    for tokens in (50_000, 40_000, 50_000, 60_000, 30_000):
        record_fixed_overhead(tokens)
    assert writes == [50_000, 60_000]


def test_a_broken_file_is_ignored():
    path = window_need._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert observed_fixed_overhead() is None
    path.write_text('{"fixedOverhead": 99999999999}', encoding="utf-8")
    window_need.reset_for_tests()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"fixedOverhead": 99999999999}', encoding="utf-8")
    assert observed_fixed_overhead() is None


def test_nonsense_is_not_recorded():
    for bad in (0, -5, None, "x", True, 10**9):
        record_fixed_overhead(bad)  # type: ignore[arg-type]
    assert observed_fixed_overhead() is None


def test_a_server_loads_the_next_common_size_within_the_models_reach():
    assert load_size(96_256, 131_072) == 98_304
    assert load_size(60_000, 131_072) == 65_536
    assert load_size(96_256, 100_000) == 98_304
    assert load_size(96_256, 90_000) == 90_000        # its whole reach; the caller sees it is not enough
    assert load_size(96_256, None) == 98_304
    assert load_size(300_000, None) == 300_032         # past the table: rounded up, never below the need


def test_every_request_the_coordinator_budgets_is_measured():
    from flowly.compaction.request_guard import ProviderRequestCoordinator

    class _Compaction:
        def compaction_threshold_for(self, model):
            return 100_000

    coordinator = ProviderRequestCoordinator(_Compaction())  # type: ignore[arg-type]
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "x" * 400, "parameters": {}}}
             for i in range(20)]
    messages = [{"role": "system", "content": "s" * 8_000}, {"role": "user", "content": "selam"}]
    fit = asyncio.run(coordinator.fit(messages, tools, "m"))
    assert fit.fits
    assert observed_fixed_overhead() == fit.before.fixed_prefix_tokens + fit.before.tool_schema_tokens > 0


# ── what the owner reads when the window is too small ───────────────────────


def _too_small_fit(window: int = 8_192):
    from flowly.compaction.request_guard import ProviderRequestCoordinator

    class _Compaction:
        class config:
            reserve_tokens_floor = 20_000

        def compaction_threshold_for(self, model):
            return window - 2_000

        def effective_context_window_for(self, model):
            return window

    compaction = _Compaction()
    coordinator = ProviderRequestCoordinator(compaction)  # type: ignore[arg-type]
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "x" * 2_000, "parameters": {}}}
             for i in range(30)]
    messages = [{"role": "system", "content": "s" * 20_000}, {"role": "user", "content": "selam"}]
    return asyncio.run(coordinator.fit(messages, tools, "google/gemma-4-e2b")), compaction


def test_a_window_smaller_than_the_agent_says_so_with_the_numbers():
    from flowly.agent.error_classifier import ErrorCategory, classify_response, present_provider_error
    from flowly.compaction.request_guard import unfit_response

    fit, compaction = _too_small_fit()
    assert not fit.fits
    response = unfit_response(fit, compaction, "google/gemma-4-e2b")

    # Every recovery rule stays the one for a request too large.
    assert classify_response(response) is ErrorCategory.INPUT_TOO_LARGE
    shown = present_provider_error(response)
    assert shown.code == "MODEL_CONTEXT_WINDOW_TOO_SMALL"
    overhead = fit.before.fixed_prefix_tokens + fit.before.tool_schema_tokens
    assert "8,192" in shown.message and f"{overhead:,}" in shown.message
    assert f"{required_window(overhead, 20_000):,}" in shown.message
    assert "compact" not in shown.message.lower()


def test_a_long_conversation_still_reads_as_one():
    from flowly.agent.error_classifier import present_provider_error
    from flowly.compaction.request_guard import RequestBudget, RequestFit, unfit_response

    budget = RequestBudget(input_limit=100_000, messages_tokens=150_000, tool_schema_tokens=10_000,
                           fixed_prefix_tokens=5_000, compactable_tokens=140_000)
    fit = RequestFit([], budget, budget, failure="request has no compactable conversation history")

    class _Compaction:
        def effective_context_window_for(self, model):
            return 128_000

    shown = present_provider_error(unfit_response(fit, _Compaction(), "m"))
    assert shown.code == "MODEL_INPUT_LIMIT_EXCEEDED"
