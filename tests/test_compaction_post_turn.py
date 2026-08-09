"""Post-turn compaction and mid-summarisation message safety.

Two behaviours live here:

  * The pre-turn check only runs when a message ARRIVES. A session whose
    final tool-heavy turn pushed it over budget used to sit there full until
    the user's next message — and then made THAT message pay the whole
    summarisation wait. ``_post_turn_compaction`` runs the same check in the
    background once the reply is out.
  * Summarisation takes real wall-clock time. Anything appended to the
    session in that window (the user's next message, a concurrent device's
    turn) is not in the summary, and the commit used to ``clear()`` it away.
    ``_commit_compaction`` now carries that appended tail across the rewrite.
"""

import asyncio
import contextlib

import pytest

from flowly.agent.loop import AgentLoop
from flowly.bus.events import InboundMessage
from flowly.compaction.service import CompactionService
from flowly.compaction.types import (
    CompactionConfig,
    CompactionResult,
    MemoryFlushConfig,
    is_summary_message,
)
from flowly.providers.base import LLMResponse
from flowly.session.manager import Session


class _Provider:
    """Returns a valid summary; counts calls so tests can assert 'no LLM spend'."""

    provider_name = "stub"

    def __init__(self):
        self.calls = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content="The conversation covered a long debugging session.",
            finish_reason="stop",
        )


class _Sessions:
    """The SessionManager surface the compaction paths touch."""

    def __init__(self, session: Session):
        self.session = session
        self.saves = 0
        self.boundaries: list[str] = []

    def get_or_create(self, key: str) -> Session:
        return self.session

    def flush_full(self, session: Session) -> None:
        pass

    def append_context_boundary(self, session: Session, compaction_id: str = "") -> None:
        # The divider the direct-gateway surfaces read back from disk.
        self.boundaries.append(compaction_id)

    def mark_full_synced(self, session: Session) -> None:
        pass

    def save(self, session: Session) -> None:
        self.saves += 1


class _Context:
    def build_system_prompt(self, **kwargs) -> str:
        return "system prompt"

    def invalidate_memory_snapshot(self, key: str) -> None:
        pass


class _Harness:
    """Real AgentLoop compaction methods over stub collaborators."""

    context_messages = 100
    _memory_manager = None
    model = "m"

    _commit_compaction = AgentLoop._commit_compaction
    _history_with_summary_anchor = AgentLoop._history_with_summary_anchor
    _system_prompt_tokens = AgentLoop._system_prompt_tokens
    _compaction_generation = staticmethod(AgentLoop._compaction_generation)
    _new_compaction_id = staticmethod(AgentLoop._new_compaction_id)
    compaction_cycle = AgentLoop.compaction_cycle
    context_epoch = AgentLoop.context_epoch
    _observed_total_tokens = AgentLoop._observed_total_tokens
    reset_conversation = AgentLoop.reset_conversation
    _post_turn_compaction = AgentLoop._post_turn_compaction
    _schedule_post_turn_compaction = AgentLoop._schedule_post_turn_compaction

    def __init__(self, session: Session, context_window: int = 2_000):
        self.sessions = _Sessions(session)
        self.context = _Context()
        self.provider = _Provider()
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
        self.events: list[str] = []
        # (phase, compactionId) pairs — the id is what lets a client tell
        # whether a "completed" closes the "started" it is showing.
        self.cycles: list[tuple[str, str]] = []
        self._post_turn_compaction_tasks: dict[str, asyncio.Task] = {}
        self._last_turn_total_tokens: dict[str, int] = {}
        self._context_epoch: dict[str, int] = {}

    def _tool_schema_tokens(self, platform: str | None = None) -> int:
        return 0

    def _routed_prompt_surface(self, platform: str | None = None):
        # This harness models no tool registry, so it reports an unknown
        # surface — the prompt builder's own "assume everything" default.
        return None, None

    async def _emit_compaction_event(
        self, session_key, phase, *a, compaction_id: str = "",
    ) -> None:
        self.events.append(phase)
        self.cycles.append((phase, compaction_id))

    async def _run_memory_flush(self, session, channel, chat_id) -> None:
        raise AssertionError("memory flush is disabled in this harness")


def _big_session(turns: int = 40) -> Session:
    session = Session(key="web:conv-1")
    filler = "the deploy log shows a retry storm on shard nine "
    for i in range(turns):
        session.add_message("user", f"q{i} {filler * 6}")
        session.add_message("assistant", f"a{i} {filler * 6}")
    return session


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    """The pass waits for the turn's reply to reach the client before it
    announces (see POST_TURN_SETTLE_SECONDS). Tests drive it directly, so the
    wait is pure latency here — the ordering it protects is asserted in
    test_the_settle_lets_the_reply_land_first."""
    import flowly.agent.loop as loop_module

    monkeypatch.setattr(loop_module, "POST_TURN_SETTLE_SECONDS", 0)


def _msg() -> InboundMessage:
    return InboundMessage(channel="web", sender_id="u", chat_id="conv-1", content="hi")


# ── The post-turn pass ────────────────────────────────────────────────────


async def test_post_turn_pass_compacts_an_over_budget_session():
    session = _big_session()
    harness = _Harness(session)

    await harness._post_turn_compaction(_msg())

    assert is_summary_message(session.messages[0]), (
        "the session was left full; the user's next message would pay the wait"
    )
    assert harness._compaction_generation(session) == 1
    assert harness.events[0] == "started"
    assert harness.events[-1] == "completed"
    # Both phases carry the SAME cycle id — that correlation is the whole
    # point of compactionId, and it is also what keys the relay's transcript
    # boundary row (so a retried event cannot draw two dividers).
    ids = {cid for _, cid in harness.cycles}
    assert len(ids) == 1 and ids != {""}, harness.cycles


async def test_post_turn_pass_is_a_noop_under_budget():
    session = Session(key="web:conv-1")
    session.add_message("user", "hello")
    session.add_message("assistant", "hi there")
    harness = _Harness(session, context_window=100_000)
    before = [dict(m) for m in session.messages]

    await harness._post_turn_compaction(_msg())

    assert session.messages == before
    assert harness.events == [], "an idle check must not flash UI notices"
    assert harness.provider.calls == 0


async def test_post_turn_pass_defers_to_a_concurrent_compaction():
    """While we wait on the session lock, someone else compacts. The re-check
    inside the lock must notice and spend nothing."""
    session = _big_session()
    harness = _Harness(session)

    async with harness.compaction.session_lock(session.key):
        task = asyncio.create_task(harness._post_turn_compaction(_msg()))
        await asyncio.sleep(0.05)  # let it emit "started" and block on the lock
        # A manual /compact wins the race: session shrinks, generation bumps.
        session.clear()
        session.add_message(
            "system", "[Previous conversation summary]\n\nAll prior work.",
        )
        session.metadata["compaction_count"] = 1
        session.metadata["last_compaction_summary"] = "All prior work."
    await task

    assert harness.provider.calls == 0, (
        "summarised history that a concurrent compaction had already replaced"
    )
    assert harness.events == [], (
        "a pass that queued behind another must stay silent — a second "
        "started/completed pair makes the UI notice flap (the live "
        "'broken shimmer')"
    )
    assert harness._compaction_generation(session) == 1, "no second commit"


async def test_post_turn_failure_leaves_the_session_untouched():
    class _Exploding(_Provider):
        async def chat(self, *args, **kwargs) -> LLMResponse:
            self.calls += 1
            raise RuntimeError("provider down")

    session = _big_session()
    harness = _Harness(session)
    harness.provider = _Exploding()
    harness.compaction.provider = harness.provider
    if hasattr(harness.compaction, "summarizer"):
        harness.compaction.summarizer.provider = harness.provider
    before = [dict(m) for m in session.messages]

    await harness._post_turn_compaction(_msg())

    assert session.messages == before
    assert harness.events[-1] == "failed"


async def test_scheduler_runs_at_most_one_pass_per_session():
    session = _big_session(turns=2)
    harness = _Harness(session, context_window=100_000)
    started = []
    release = asyncio.Event()

    async def _slow_pass(msg):
        started.append(msg.session_key)
        await release.wait()

    harness._post_turn_compaction = _slow_pass  # bypass the bound real method

    msg = _msg()
    harness._schedule_post_turn_compaction(msg)
    harness._schedule_post_turn_compaction(msg)
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.gather(*harness._post_turn_compaction_tasks.values())

    assert started == ["web:conv-1"], (
        "a second turn ending must not stack another pass behind the first"
    )


# ── The provider's count is ground truth ──────────────────────────────────


async def test_observed_usage_triggers_when_the_estimate_says_fits():
    """The live failure: estimate ~72K in a 79K window ("fits"), provider
    counted 82K (over). Estimators drift by model and language — when the
    provider says the window is full, the estimate doesn't get a veto."""
    session = _big_session()  # ~5.6K estimated — under budget, over the floor
    harness = _Harness(session, context_window=100_000)
    harness._last_turn_total_tokens[session.key] = 100_500  # provider: over

    await harness._post_turn_compaction(_msg())

    assert is_summary_message(session.messages[0]), (
        "the provider reported an overflowing window and nothing happened"
    )
    assert harness.events[-1] == "completed"


async def test_a_commit_clears_the_observation():
    """The observed count described the PRE-compaction context. Kept, it
    would re-trigger compaction against the fresh summary forever."""
    session = _big_session()
    harness = _Harness(session, context_window=100_000)
    harness._last_turn_total_tokens[session.key] = 100_500

    await harness._post_turn_compaction(_msg())
    calls_after_first = harness.provider.calls

    await harness._post_turn_compaction(_msg())

    assert session.key not in harness._last_turn_total_tokens
    assert harness.provider.calls == calls_after_first, (
        "a stale observation re-compacted an already-summarised session"
    )


def test_service_trigger_prefers_the_provider_over_the_estimate():
    service = _Harness(_big_session(turns=1), context_window=100_000).compaction
    floor = service.MIN_OBSERVED_TRIGGER_HISTORY_TOKENS

    # Estimate comfortably under budget, provider over the threshold, and
    # enough history to make summarising worthwhile.
    assert service.should_compact(floor + 1_000, "s", overhead_tokens=0,
                                  observed_total_tokens=100_500)
    # Both under: stays quiet.
    assert not service.should_compact(floor + 1_000, "s", overhead_tokens=0,
                                      observed_total_tokens=50_000)
    # Provider over, but almost no history — the overflow is fixed overhead,
    # which summarising cannot shrink (observed live at 1.8K history).
    assert not service.should_compact(1_800, "s", overhead_tokens=0,
                                      observed_total_tokens=100_500)


# ── Mid-summarisation appends survive the commit ──────────────────────────


def _result() -> CompactionResult:
    return CompactionResult(
        summary="Earlier: a long debugging session.",
        tokens_before=5_000,
        tokens_after=100,
        messages_removed=80,
        kept_messages=[{"role": "user", "content": "kept question"}],
    )


def test_commit_preserves_messages_appended_during_summarisation():
    session = _big_session(turns=3)
    harness = _Harness(session)
    snapshot_len = len(session.messages)
    # These land while the summariser is running — they are NOT in the summary.
    session.add_message("user", "one more thing — check the cron job")
    session.add_message("assistant", "On it.")

    history = harness._commit_compaction(
        session, _result(), session.key, source_message_count=snapshot_len,
    )

    contents = [m.get("content") for m in session.messages]
    assert "one more thing — check the cron job" in contents
    assert "On it." in contents
    assert contents.index("kept question") < contents.index("On it."), (
        "late arrivals must follow the kept tail, in order"
    )
    # The returned working history sees them too, in bare LLM shape.
    assert history[-1] == {"role": "assistant", "content": "On it."}
    assert is_summary_message(session.messages[0])


def test_commit_without_a_snapshot_behaves_exactly_as_before():
    session = _big_session(turns=3)
    harness = _Harness(session)

    history = harness._commit_compaction(session, _result(), session.key)

    roles = [m["role"] for m in session.messages]
    assert roles == ["system", "user"], "summary + kept tail only"
    assert history[-1] == {"role": "user", "content": "kept question"}


# ── Reasoning models must not starve the summary ──────────────────────────


class _ReasoningStarvedProvider(_Provider):
    """Empty body unless the output budget is generous — the observed live
    failure mode: max_tokens bounds hidden reasoning AND the answer together,
    and a 3000-token reserve let the reasoning eat all of it."""

    def __init__(self, needs: int):
        super().__init__()
        self.needs = needs
        self.budgets_seen: list[int] = []

    async def chat(self, *args, **kwargs):
        from flowly.providers.base import LLMResponse

        self.calls += 1
        budget = int(kwargs.get("max_tokens") or 0)
        self.budgets_seen.append(budget)
        if budget < self.needs:
            return LLMResponse(content="", finish_reason="length")
        return LLMResponse(
            content="A grounded summary of the debugging session.",
            finish_reason="stop",
        )


async def test_summary_call_never_runs_on_a_starving_budget():
    from flowly.compaction.summarizer import (
        MIN_SUMMARY_OUTPUT_TOKENS,
        generate_summary,
    )

    provider = _ReasoningStarvedProvider(needs=MIN_SUMMARY_OUTPUT_TOKENS)
    text = await generate_summary(
        [{"role": "user", "content": "long story"}],
        provider,
        "m",
        reserve_tokens=3000,  # the live config that starved the call
    )

    assert "grounded summary" in text
    assert provider.budgets_seen[0] >= MIN_SUMMARY_OUTPUT_TOKENS


async def test_an_empty_body_gets_one_doubled_retry_then_fails_loudly():
    from flowly.compaction.summarizer import (
        MIN_SUMMARY_OUTPUT_TOKENS,
        generate_summary,
    )
    from flowly.compaction.types import CompactionError

    # Succeeds only on the doubled retry.
    provider = _ReasoningStarvedProvider(needs=MIN_SUMMARY_OUTPUT_TOKENS * 2)
    text = await generate_summary(
        [{"role": "user", "content": "long story"}], provider, "m",
        reserve_tokens=3000,
    )
    assert "grounded summary" in text
    assert provider.budgets_seen == [
        MIN_SUMMARY_OUTPUT_TOKENS,
        MIN_SUMMARY_OUTPUT_TOKENS * 2,
    ]

    # Never succeeds: exactly two attempts, then the error propagates so the
    # caller keeps its uncompacted history.
    hopeless = _ReasoningStarvedProvider(needs=10**9)
    try:
        await generate_summary(
            [{"role": "user", "content": "long story"}], hopeless, "m",
            reserve_tokens=3000,
        )
        raise AssertionError("an empty summary was accepted")
    except CompactionError:
        pass
    assert hopeless.calls == 2


# ── The background slot must always free itself ───────────────────────────


async def test_a_wedged_pass_does_not_disable_the_session_forever():
    """One pass per session is the rule; a pass that never returns would make
    that rule permanent. Every call inside is individually bounded, so the
    overall cap is a backstop — but without it, a single unforeseen wedge
    means no post-turn compaction for that session until a restart."""
    import flowly.agent.loop as loop_module

    session = _big_session(turns=2)
    harness = _Harness(session, context_window=100_000)
    monkey = loop_module.POST_TURN_COMPACTION_TIMEOUT_SECONDS
    loop_module.POST_TURN_COMPACTION_TIMEOUT_SECONDS = 0.1
    try:
        async def _wedged(msg):
            await asyncio.sleep(3600)

        harness._post_turn_compaction = _wedged

        msg = _msg()
        harness._schedule_post_turn_compaction(msg)
        wedged_task = harness._post_turn_compaction_tasks[msg.session_key]
        await asyncio.sleep(0.3)

        assert wedged_task.done(), "the wedged pass was never cut off"
        assert msg.session_key not in harness._post_turn_compaction_tasks, (
            "the session's background slot stayed taken"
        )

        # And the session can schedule again.
        harness._schedule_post_turn_compaction(msg)
        assert msg.session_key in harness._post_turn_compaction_tasks
        second = harness._post_turn_compaction_tasks[msg.session_key]
        # Let the task take its first step before cancelling: cancelled before
        # it ever runs, the coroutine it wraps is never awaited and Python
        # reports that as a warning — noise that hides real ones.
        await asyncio.sleep(0)
        second.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await second
    finally:
        loop_module.POST_TURN_COMPACTION_TIMEOUT_SECONDS = monkey


# ── Lifecycle ownership is atomic ─────────────────────────────────────────


async def test_two_passes_starting_together_announce_one_cycle():
    """The TOCTOU the first fix missed. Emitting an event YIELDS (it writes to
    a socket), so a pass that checks `lock.locked()` and then announces gives a
    second pass a window in which the lock still looks free — both announce,
    both close, and the notice flaps between phases though one compaction ran.

    The rule that fixes it: the cycle belongs to whoever HOLDS the lock."""
    session = _big_session()
    harness = _Harness(session)

    async def yielding_emit(session_key, phase, *a, compaction_id: str = ""):
        await asyncio.sleep(0)  # what a real WS send does
        harness.events.append(phase)
        harness.cycles.append((phase, compaction_id))

    harness._emit_compaction_event = yielding_emit

    await asyncio.gather(
        harness._post_turn_compaction(_msg()),
        harness._post_turn_compaction(_msg()),
    )

    assert harness.events == ["started", "completed"], (
        f"one compaction, one UI cycle — got {harness.events}"
    )
    assert harness._compaction_generation(session) == 1
    assert harness.provider.calls > 0
    assert len({cid for _, cid in harness.cycles}) == 1, (
        f"the surviving cycle must be one identity: {harness.cycles}"
    )


async def test_a_started_cycle_is_always_closed():
    """Whatever happens after 'started', the notice must not be left running —
    a stuck shimmer is how a delivered reply got hidden once already."""
    class _Exploding(_Provider):
        async def chat(self, *args, **kwargs) -> LLMResponse:
            self.calls += 1
            raise RuntimeError("provider down")

    session = _big_session()
    harness = _Harness(session)
    harness.provider = _Exploding()
    harness.compaction.provider = harness.provider

    await harness._post_turn_compaction(_msg())

    assert harness.events[0] == "started"
    assert harness.events[-1] in ("completed", "failed")


async def test_a_commit_marks_the_boundary_for_disk_based_history():
    """Relay clients read the divider from a Firestore row the relay writes;
    direct-gateway clients read history from disk. Both must get one, keyed by
    the same cycle id, or the two transports show different transcripts."""
    session = _big_session()
    harness = _Harness(session)

    await harness._post_turn_compaction(_msg())

    assert len(harness.sessions.boundaries) == 1
    cycle_ids = {cid for _, cid in harness.cycles}
    assert harness.sessions.boundaries[0] in cycle_ids, (
        "the persisted divider is not the cycle the client was told about"
    )


async def test_the_settle_lets_the_reply_land_first(monkeypatch):
    """The pass is scheduled from _process_message's finally, which runs
    BEFORE the caller publishes the turn's reply. Announcing immediately let
    the "started" notice overtake the reply on the wire — and clients treat
    arriving content as proof a compaction finished, so the notice was wiped
    the instant the reply landed (live: a 15s compaction, no visible shimmer).
    """
    import flowly.agent.loop as loop_module

    monkeypatch.setattr(loop_module, "POST_TURN_SETTLE_SECONDS", 0.05)
    session = _big_session()
    harness = _Harness(session)
    order: list[str] = []

    async def emit(session_key, phase, *a, compaction_id: str = ""):
        order.append(f"event:{phase}")
        harness.events.append(phase)
        harness.cycles.append((phase, compaction_id))

    harness._emit_compaction_event = emit

    async def deliver_reply():
        # What _process_turn does immediately after scheduling this pass.
        await asyncio.sleep(0)
        order.append("reply")

    await asyncio.gather(
        harness._post_turn_compaction(_msg()),
        deliver_reply(),
    )

    assert order[0] == "reply", (
        f"the compaction announced before the turn's own reply: {order}"
    )


# ── A reset always wins over an in-flight compaction ──────────────────────


async def test_clearing_the_chat_beats_a_compaction_already_running():
    """The user hits /clear while a compaction sits in its provider call. Its
    summary describes turns they just discarded — committing it would put the
    old conversation back on top of the empty one they asked for.

    The generation counter cannot catch this: a reset wipes the very metadata
    it compares, so before and after both read 0. Hence the epoch, which lives
    outside the session."""
    session = _big_session()
    harness = _Harness(session)
    barrier = asyncio.Event()
    original = harness.provider.chat

    async def parked(*a, **k):
        await barrier.wait()
        return await original(*a, **k)

    harness.provider.chat = parked
    task = asyncio.create_task(harness._post_turn_compaction(_msg()))
    await asyncio.sleep(0.05)  # now inside the provider call

    harness.reset_conversation(session.key)

    barrier.set()
    await task

    assert session.messages == [], (
        f"the cleared conversation came back: {[m.get('role') for m in session.messages]}"
    )
    assert not session.metadata.get("last_compaction_summary")
    assert harness.sessions.boundaries == [], (
        "a boundary was drawn for a conversation that no longer exists"
    )
    # The terminal is dropped on purpose — see
    # test_a_refused_commit_still_closes_the_cycle_with_its_id.
    assert harness.events == ["started"]


async def test_a_reset_bumps_the_epoch_even_while_the_lock_is_held():
    """reset_session() deliberately skips a locked session (an in-flight
    commit relies on that exclusion), so the epoch cannot live there either."""
    session = _big_session()
    harness = _Harness(session)

    async with harness.compaction.session_lock(session.key):
        before = harness.context_epoch(session.key)
        harness.reset_conversation(session.key)

        assert harness.context_epoch(session.key) == before + 1


# ── Cached input still occupies the window ────────────────────────────────


def test_native_anthropic_cached_input_counts_toward_the_observed_size():
    """Native Anthropic reports the cached prefix outside ``input_tokens``.
    The cache still occupies the model window, so it must be added once."""
    session = _big_session(turns=1)
    harness = _Harness(session)
    harness.provider.provider_name = "anthropic"

    class _Outcome:
        metadata = {"usage": {
            "prompt_tokens": 2_000,
            "cache_read_tokens": 75_000,
            "cache_write_tokens": 0,
            "completion_tokens": 1_000,
            "total_tokens": 3_000,
        }}

    harness._note_turn_usage = AgentLoop._note_turn_usage.__get__(harness)
    harness._note_turn_usage(session.key, _Outcome())

    assert harness._last_turn_total_tokens[session.key] == 78_000


def test_openai_shaped_cached_input_is_not_counted_twice():
    """OpenRouter/OpenAI ``prompt_tokens`` already includes cached input.
    Adding its cache breakdown again made a 78K request look like 153K and
    triggered needless compaction on cache-heavy conversations."""
    session = _big_session(turns=1)
    harness = _Harness(session)
    harness.provider.provider_name = "openrouter"

    class _Outcome:
        metadata = {"usage": {
            "prompt_tokens": 77_000,
            "cache_read_tokens": 75_000,
            "cache_write_tokens": 0,
            "completion_tokens": 1_000,
            "total_tokens": 78_000,
        }}

    harness._note_turn_usage = AgentLoop._note_turn_usage.__get__(harness)
    harness._note_turn_usage(session.key, _Outcome())

    assert harness._last_turn_total_tokens[session.key] == 78_000


async def test_the_observed_size_survives_the_process():
    """`flowly agent -m` runs ONE process per message, so an in-memory reading
    is gone before the next turn can use it — the provider-observed trigger
    could never fire there. A restarted gateway lost it the same way."""
    session = _big_session(turns=1)
    harness = _Harness(session, context_window=100_000)

    class _Outcome:
        metadata = {"usage": {"prompt_tokens": 80_000, "completion_tokens": 1_000}}

    harness._note_turn_usage = AgentLoop._note_turn_usage.__get__(harness)
    harness._note_turn_usage(session.key, _Outcome())

    assert session.metadata["last_turn_total_tokens"] == 81_000

    # A fresh process: nothing in memory, everything on the session.
    reborn = _Harness(session, context_window=100_000)
    assert reborn._observed_total_tokens(session.key, session) == 81_000


async def test_a_reset_drops_the_persisted_reading_too():
    """It describes a context the user just discarded."""
    session = _big_session(turns=1)
    harness = _Harness(session, context_window=100_000)
    session.metadata["last_turn_total_tokens"] = 81_000

    harness.reset_conversation(session.key)

    assert "last_turn_total_tokens" not in session.metadata


async def test_a_refused_commit_still_closes_the_cycle_with_its_id():
    """The manual path committed outside its try, so a reset landing
    mid-summary escaped to the caller with the notice still spinning. Every
    announced cycle gets a terminal, and it carries the SAME id."""
    session = _big_session()
    harness = _Harness(session)
    barrier = asyncio.Event()
    original = harness.provider.chat

    async def parked(*a, **k):
        await barrier.wait()
        return await original(*a, **k)

    harness.provider.chat = parked
    task = asyncio.create_task(harness._post_turn_compaction(_msg()))
    await asyncio.sleep(0.05)
    harness.reset_conversation(session.key)
    barrier.set()
    await task

    phases = [p for p, _ in harness.cycles]
    assert phases == ["started"], (
        f"expected the terminal to be dropped for a reset conversation: {phases}"
    )
    # Deliberate: the reset already ended this story for every client — it
    # clears the live status, and the /clear turn's own reply clears the
    # notice on each device. Recording a terminal here would hand the FRESH
    # chat an old compaction's outcome through the re-entry handshake.
    assert harness.cycles[0][1], "the cycle still has to be identifiable"
