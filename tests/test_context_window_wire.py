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

from flowly.agent.loop import AgentLoop, context_occupancy_tokens
from flowly.compaction.service import CompactionService
from flowly.compaction.types import CompactionConfig, MemoryFlushConfig


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
