"""The context-window numbers the bot puts on the wire for its clients.

A client cannot compute either of these for itself:

  * ``usage`` arrives in the active provider's dialect, and ``prompt_tokens``
    means different things in each. Read raw, a cached native-Anthropic turn
    looks nearly empty.
  * The real ceiling comes from the catalogue for whatever provider is
    configured. The desktop could only reach the Flowly proxy's catalogue, so
    a BYOK model id resolved to nothing and its indicator disappeared.

So the loop answers both and the channels forward them. These tests pin the
arithmetic and the "omit when unknown" contract that keeps older clients on
their own fallback.
"""

import asyncio

from flowly.agent.loop import AgentLoop, context_occupancy_tokens
from flowly.compaction.service import (
    CompactionService,
    _flowly_proxy_max_input_tokens,
)
from flowly.compaction.types import CompactionConfig, MemoryFlushConfig
from flowly.integrations import model_catalog as mc
from flowly.integrations.model_catalog import Model
from flowly.tui.client import ChatFinal, GatewayClient
from flowly.tui.panes.status import StatusBar, _model_budget, _TokenBar


class _Provider:
    def __init__(self, provider_name: str = "stub"):
        self.provider_name = provider_name


class _Harness:
    """The real window resolver over a stub provider."""

    model = "m"

    _effective_context_window = AgentLoop._effective_context_window

    def __init__(self, provider_name: str = "stub", context_window: int = 200_000):
        self.provider = _Provider(provider_name)
        self.compaction = CompactionService(
            provider=self.provider,
            model="m",
            config=CompactionConfig(
                mode="default",
                context_window=context_window,
                context_window_explicit=True,
                reserve_tokens_floor=100,
                memory_flush=MemoryFlushConfig(enabled=False),
            ),
        )


# ── Occupancy: one number, whatever dialect the provider speaks ───────────


def test_native_anthropic_cached_prefix_counts_toward_occupancy():
    """``input_tokens`` is the uncached remainder only. A client dividing it
    by the window drew a 1%-full ring on a conversation that was nearly full —
    the whole reason the indicator looked broken on Anthropic BYOK."""
    tokens = context_occupancy_tokens({
        "prompt_tokens": 2_000,
        "cache_read_tokens": 175_000,
        "cache_write_tokens": 3_000,
        "completion_tokens": 1_000,
        "total_tokens": 3_000,
    }, "anthropic")

    assert tokens == 181_000


def test_openai_shaped_cache_is_not_counted_twice():
    """OpenAI-compatible ``prompt_tokens`` ALREADY includes the cached input.
    Applying the Anthropic correction here would inflate a 78K turn to 153K."""
    tokens = context_occupancy_tokens({
        "prompt_tokens": 77_000,
        "cache_read_tokens": 75_000,
        "cache_write_tokens": 0,
        "completion_tokens": 1_000,
        "total_tokens": 78_000,
    }, "openrouter")

    assert tokens == 78_000


def test_the_reply_occupies_the_window_too():
    """The completion is history the NEXT request carries. Counting only the
    prompt makes the ring lag a full turn behind what the model will see."""
    assert context_occupancy_tokens(
        {"prompt_tokens": 50_000, "completion_tokens": 4_000}, "openrouter",
    ) == 54_000


def test_an_aggregate_only_provider_still_reports_something():
    """Custom / older OpenAI-compatible endpoints sometimes send nothing but
    ``total_tokens``. A zero here would hide the indicator entirely."""
    assert context_occupancy_tokens({"total_tokens": 12_345}, "custom") == 12_345


def test_an_unknown_provider_is_treated_as_openai_shaped():
    """The dialect that needs the correction is the named exception; every
    other endpoint we can reach reports OpenAI-shaped usage. Guessing the
    other way inflates every custom endpoint's ring."""
    usage = {"prompt_tokens": 77_000, "cache_read_tokens": 75_000}

    assert context_occupancy_tokens(usage, "") == 77_000
    assert context_occupancy_tokens(usage, "ollama") == 77_000


def test_no_usage_means_no_answer_rather_than_a_wrong_one():
    """Zero is the wire's "I don't know" — the client keeps its own fallback
    instead of being told the context is empty."""
    assert context_occupancy_tokens({}, "openrouter") == 0
    assert context_occupancy_tokens(None, "openrouter") == 0
    assert context_occupancy_tokens({"prompt_tokens": "junk"}, "openrouter") == 0


# ── Ceiling: resolved where the provider is actually known ────────────────


def test_the_window_comes_from_the_compaction_service():
    """One resolver, not two. The service already walks live catalogue →
    family heuristic → configured value and clamps to the proxy's input
    ceiling; re-deriving it per client is what produced the mismatch."""
    harness = _Harness("anthropic", context_window=200_000)

    assert harness._effective_context_window() == 200_000
    assert harness._effective_context_window() == harness.compaction.effective_context_window


def test_an_unresolvable_window_is_reported_as_unknown():
    """``0`` must not reach a client as a ceiling — dividing by it would paint
    a permanently-full ring. The channels drop the field instead."""
    harness = _Harness("openrouter")

    class _Broken:
        @property
        def effective_context_window(self):
            raise RuntimeError("catalogue exploded")

    harness.compaction = _Broken()

    assert harness._effective_context_window() == 0


# ── Finding the model in the catalogue at all ─────────────────────────────


def test_a_dated_snapshot_resolves_to_its_undated_catalogue_entry():
    """Providers pin a date onto an id (`-0731`) while the catalogue lists the
    undated family entry. An exact-match lookup missed, so a 1M model fell all
    the way through to the 128K default — the reported symptom."""
    mc._CACHE["test"] = [Model(
        id="deepseek/deepseek-v4-flash", name="V4 Flash", context_window=1_048_576)]
    try:
        assert mc.get_context_window("deepseek/deepseek-v4-flash-0731") == 1_048_576
        assert mc.get_context_window("deepseek/deepseek-v4-flash-20260731") == 1_048_576
        assert mc.get_context_window("deepseek/deepseek-v4-flash") == 1_048_576
    finally:
        mc._CACHE.pop("test", None)


def test_a_version_component_is_not_mistaken_for_a_date():
    """`claude-sonnet-4-5` must never degrade to `claude-sonnet-4` — that is a
    DIFFERENT model, and answering for it would be worse than not answering.
    Only a run of four or more digits reads as a snapshot date."""
    mc._CACHE["test"] = [
        Model(id="anthropic/claude-sonnet-4", name="S4", context_window=200_000),
        Model(id="anthropic/claude-sonnet-4.5", name="S4.5", context_window=500_000),
    ]
    try:
        # Resolves to 4.5 via the dash/dot rule, not to 4 via suffix stripping.
        assert mc.get_context_window("anthropic/claude-sonnet-4-5") == 500_000
        # Dated snapshot of 4.5 still lands on 4.5.
        assert mc.get_context_window("anthropic/claude-sonnet-4-5-20250929") == 500_000
    finally:
        mc._CACHE.pop("test", None)


def test_an_unknown_model_still_reports_nothing():
    """Stripping must not turn a miss into a wrong hit."""
    mc._CACHE["test"] = [Model(
        id="deepseek/deepseek-v4-flash", name="V4", context_window=1_048_576)]
    try:
        assert mc.get_context_window("someone/other-model-0731") is None
    finally:
        mc._CACHE.pop("test", None)


# ── The proxy ceiling the bot mirrors ─────────────────────────────────────


def test_the_proxy_ceiling_follows_the_real_window_when_known():
    """The bot mirrors the proxy's per-model input ceiling. Both sides now read
    the real context length first; the four-family table alone capped a 1M
    model at 128K and compacted it eight times too early."""
    mc._CACHE["test"] = [Model(
        id="deepseek/deepseek-v4-flash", name="V4", context_window=1_048_576)]
    try:
        assert _flowly_proxy_max_input_tokens(
            "deepseek/deepseek-v4-flash-0731") == 1_048_576
    finally:
        mc._CACHE.pop("test", None)


def test_a_cold_catalogue_falls_back_to_the_family_table():
    """Cache-only on both sides. A cold bot must land at or below the proxy's
    answer — never above it, or it builds prompts the proxy 413s."""
    mc._CACHE.pop("test", None)

    assert _flowly_proxy_max_input_tokens("deepseek/deepseek-v4-flash-0731") == 128_000
    assert _flowly_proxy_max_input_tokens("anthropic/claude-haiku-4.5") == 200_000


# ── The TUI is a client of that wire, like the desktop ────────────────────


async def _final_event(payload: dict) -> ChatFinal:
    """Run one gateway `chat`/`final` frame through the real parser."""
    client = GatewayClient.__new__(GatewayClient)
    client._inbox = asyncio.Queue()
    await client._dispatch({"type": "event", "event": "chat", "data": payload})
    return await client._inbox.get()


async def test_the_tui_reads_the_wire_instead_of_guessing_the_dialect():
    """The status bar used to derive occupancy from `prompt_tokens` and so
    under-reported on native Anthropic exactly like the desktop did. It is a
    client of the same event — it should take the answer, not recompute it."""
    ev = await _final_event({
        "state": "final",
        "runId": "r1",
        "sessionKey": "cli:1",
        "message": {"content": [{"type": "text", "text": "hi"}],
                    "usage": {"prompt_tokens": 2_000, "completion_tokens": 1_000}},
        "contextTokens": 181_000,
        "contextWindow": 200_000,
    })

    assert ev.context_tokens == 181_000
    assert ev.context_window == 200_000


async def test_an_older_gateway_leaves_the_tui_on_its_own_numbers():
    """No fields ⇒ None, not 0: the app then falls back to `prompt_tokens`
    and the model-family budget, which is the pre-existing behaviour."""
    ev = await _final_event({
        "state": "final",
        "runId": "r1",
        "sessionKey": "cli:1",
        "message": {"content": [], "usage": {"prompt_tokens": 500}},
    })

    assert ev.context_tokens is None
    assert ev.context_window is None


async def test_a_zero_or_junk_reading_is_treated_as_absent():
    """0 would mean "empty context" / "no room at all" rather than "unknown"."""
    ev = await _final_event({
        "state": "final",
        "runId": "r1",
        "sessionKey": "cli:1",
        "message": {"content": []},
        "contextTokens": 0,
        "contextWindow": "200000",
    })

    assert ev.context_tokens is None
    assert ev.context_window is None


def _rendered(bar: _TokenBar, monkeypatch) -> str:
    """What the bar would paint. `update()` needs a live app, so capture it."""
    painted: list[str] = []
    monkeypatch.setattr(bar, "update", painted.append)
    bar._refresh()
    return painted[-1] if painted else ""


def test_the_bar_prefers_the_reported_ceiling_over_its_own_guess(monkeypatch):
    """`_model_budget` falls back to a flat 200K for anything it cannot place,
    which is every BYOK model whose catalogue this process has not cached. A
    32K model drawn against 200K tells the user they have room they don't."""
    bar = _TokenBar()
    bar.model = "some-byok-model"
    bar.tokens_in = 16_000
    bar.budget_override = 32_768

    text = _rendered(bar, monkeypatch)

    assert "32.8K" in text and "200.0K" not in text
    assert "49%" in text  # 16_000 / 32_768, not 16_000 / 200_000 → 8%


def test_the_bar_keeps_guessing_when_nothing_was_reported(monkeypatch):
    """An older gateway reports no ceiling; the bar must behave exactly as it
    did before, not collapse to zero and hide itself."""
    bar = _TokenBar()
    bar.model = "anthropic/claude-opus-4.8"
    bar.tokens_in = 20_000

    assert bar.budget_override == 0
    assert _model_budget("anthropic/claude-opus-4.8") == 200_000
    assert "200.0K" in _rendered(bar, monkeypatch)


def test_switching_model_drops_a_ceiling_that_no_longer_applies(monkeypatch):
    """A reported ceiling belongs to the model that was running. Keeping a
    200K window after switching to a 32K model overstates the room until the
    next turn happens to land."""
    status = StatusBar()
    monkeypatch.setattr(status, "_sync_context_header", lambda **kwargs: None)
    status.context_budget = 200_000

    status.model = "some-small-model"

    assert status.context_budget == 0
