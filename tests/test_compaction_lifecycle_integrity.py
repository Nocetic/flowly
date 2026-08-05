"""Every announced compaction cycle is closed, and by the right event.

The wire contract says a `started` is followed by exactly one terminal
carrying the SAME `compactionId` (docs/chat-wire-protocol.md §5). Three paths
announce, and each had a way to walk off without closing:

  * the manual path committed OUTSIDE its try, so a refusal to commit (a reset
    landing mid-summary) escaped to the caller with the notice still spinning;
  * the post-turn path's outermost failure emitted a terminal with no id, so a
    client keying on the id could never match it to what it was showing;
  * nothing at all closed a cycle whose process died mid-summary, and a
    "started" was reported to re-entering clients forever.

These also pin the two claims a previous test only appeared to prove: that a
reset clears the live status, and that the provider's observed request size
reaches DISK rather than living in one process's memory.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from flowly.agent import compaction_status
from flowly.agent.loop import AgentLoop
from flowly.session.manager import SessionManager


@pytest.fixture(autouse=True)
def _clean_status():
    compaction_status._STATE.clear()
    yield
    compaction_status._STATE.clear()


# ── The running state cannot be reported forever ──────────────────────────


def test_an_unclosed_cycle_stops_being_reported():
    compaction_status.record("s", "started", 90_000, 0, 0, now=0.0)

    assert compaction_status.get("s", now=600.0) is not None, (
        "a genuinely long summary must still report as running"
    )
    assert compaction_status.get(
        "s", now=compaction_status.RUNNING_MAX_AGE_SECONDS + 1,
    ) is None


def test_the_cycle_id_reaches_a_re_entering_client():
    compaction_status.record(
        "s", "started", 90_000, 0, 0, now=0.0, compaction_id="cmp_live",
    )

    state = compaction_status.get("s", now=1.0)

    assert state["compactionId"] == "cmp_live"


# ── A reset clears the live status, not just the history ──────────────────


def test_reset_clears_a_running_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly import profile

    if hasattr(profile, "_cached_home"):
        profile._cached_home = None

    class _Harness:
        reset_conversation = AgentLoop.reset_conversation
        context_epoch = AgentLoop.context_epoch

        def __init__(self):
            self.sessions = SessionManager(workspace=tmp_path)
            self._context_epoch: dict[str, int] = {}
            self._last_turn_total_tokens: dict[str, int] = {}

            class _Ctx:
                def invalidate_memory_snapshot(self, key): pass

            self.context = _Ctx()

            class _Compaction:
                def reset_session(self, key): pass

            self.compaction = _Compaction()

    harness = _Harness()
    session = harness.sessions.get_or_create("cli:x")
    session.add_message("user", "hello")
    compaction_status.record("cli:x", "started", 90_000, 0, 0, now=0.0)

    harness.reset_conversation("cli:x")

    assert compaction_status.get("cli:x", now=1.0) is None, (
        "chat.inflight would hand a running compaction for a conversation "
        "that no longer exists"
    )


# ── The observed request size must reach disk ─────────────────────────────


def test_observed_usage_is_readable_by_a_new_process(tmp_path, monkeypatch):
    """`flowly agent -m` runs ONE process per message. A previous test passed
    the same Session object to a second harness, which proved only that Python
    objects keep their attributes — not that anything was written."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly import profile

    if hasattr(profile, "_cached_home"):
        profile._cached_home = None

    manager = SessionManager(workspace=tmp_path)
    session = manager.get_or_create("cli:probe")
    session.add_message("user", "hello")
    manager.save(session)

    class _Harness:
        _note_turn_usage = AgentLoop._note_turn_usage

        def __init__(self, sessions):
            self.sessions = sessions
            self._last_turn_total_tokens: dict[str, int] = {}

    class _Outcome:
        metadata = {"usage": {"prompt_tokens": 80_000, "completion_tokens": 1_000}}

    _Harness(manager)._note_turn_usage("cli:probe", _Outcome())

    # A genuinely fresh reader: new manager, nothing cached, straight off disk.
    reborn = SessionManager(workspace=tmp_path)
    reloaded = reborn.get_or_create("cli:probe")

    assert reloaded.metadata.get("last_turn_total_tokens") == 81_000, (
        "the reading never reached disk, so a per-message CLI process and a "
        "restarted gateway both start blind"
    )


# ── The one-shot CLI does not throw away its own background work ──────────


async def test_a_caller_can_wait_for_the_background_pass():
    class _Harness:
        await_post_turn_compaction = AgentLoop.await_post_turn_compaction

        def __init__(self):
            self._post_turn_compaction_tasks: dict[str, asyncio.Task] = {}

    harness = _Harness()
    finished = []

    async def work():
        await asyncio.sleep(0.05)
        finished.append(True)

    harness._post_turn_compaction_tasks["s"] = asyncio.create_task(work())

    await harness.await_post_turn_compaction("s")

    assert finished == [True], (
        "asyncio.run() would have cancelled this on the way out, so the "
        "one-shot CLI got the post-turn check but never the work"
    )


async def test_waiting_is_a_noop_when_nothing_is_running():
    class _Harness:
        await_post_turn_compaction = AgentLoop.await_post_turn_compaction

        def __init__(self):
            self._post_turn_compaction_tasks: dict[str, asyncio.Task] = {}

    await _Harness().await_post_turn_compaction("nothing")


async def test_a_failing_background_pass_does_not_break_the_caller():
    class _Harness:
        await_post_turn_compaction = AgentLoop.await_post_turn_compaction

        def __init__(self):
            self._post_turn_compaction_tasks: dict[str, asyncio.Task] = {}

    harness = _Harness()

    async def boom():
        raise RuntimeError("provider down")

    harness._post_turn_compaction_tasks["s"] = asyncio.create_task(boom())

    await harness.await_post_turn_compaction("s")  # must not raise
