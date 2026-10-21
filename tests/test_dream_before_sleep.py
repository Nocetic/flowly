"""Memory learning is never skipped by a sleeping machine ("dream before sleep").

The dreamer's idle pass used to depend on an in-memory timestamp and a timer
that fires 30 minutes after the last user message: exactly when a cloud machine
goes to sleep. The timestamp now lives on disk, the agent reports a pass it
owes, and the pass runs before the machine sleeps.
"""

from __future__ import annotations

import asyncio
import json
import time

from flowly.agent.loop import AgentLoop


class FakeGov:
    def __init__(self, dirty: bool = False):
        self.dirty = dirty

    def is_dirty(self) -> bool:
        return self.dirty

    def clear_dirty(self) -> None:
        self.dirty = False


class FakeConsolidateTool:
    def __init__(self):
        self.runs = 0

    async def execute(self, dry_run: bool = False):
        self.runs += 1
        return "ok"


class FakeDreamer:
    def __init__(self):
        self.runs = 0

    def run(self, *, max_messages: int = 500):
        self.runs += 1

        class Result:
            ran = True
            candidates = 0
            processed_messages = 0
            watermark = 0

        return Result()


def _loop(tmp_path, *, dirty=False, dreamer=True) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._state_dir = tmp_path
    loop._running = True
    loop._turns_in_progress = 0
    loop._memory_gov = FakeGov(dirty)
    loop._consolidate_tool = FakeConsolidateTool()
    loop._auto_consolidate = True
    loop._consolidate_lock = None
    loop._dreamer = FakeDreamer() if dreamer else None
    loop._dreamer_lock = asyncio.Lock() if dreamer else None
    loop._dreamer_enabled = dreamer
    loop._dreamer_idle_minutes = 30
    loop._dreamer_daily_time = "03:30"
    loop._dreamer_max_messages = 500
    loop._dreamer_last_user_ts = 0.0
    loop._dreamer_idle_fired_for = 0.0
    loop._load_dreamer_activity()
    return loop


def test_user_activity_survives_a_restart(tmp_path):
    first = _loop(tmp_path)
    first._note_user_activity(1_700_000_000.0)
    assert json.loads((tmp_path / "dreamer_activity.json").read_text())["lastUserAt"] == 1_700_000_000.0

    second = _loop(tmp_path)
    assert second._dreamer_last_user_ts == 1_700_000_000.0
    assert second.power_snapshot()["dreamOwed"] is True
    assert second.power_snapshot()["lastUserActivityAt"] == 1_700_000_000.0


def test_a_corrupt_activity_file_is_ignored(tmp_path):
    (tmp_path / "dreamer_activity.json").write_text("{not json", encoding="utf-8")
    loop = _loop(tmp_path)
    assert loop._dreamer_last_user_ts == 0.0
    assert loop.power_snapshot()["dreamOwed"] is False


async def test_dream_before_sleep_runs_the_owed_pass_once(tmp_path):
    loop = _loop(tmp_path)
    loop._note_user_activity(time.time() - 60)
    assert loop.power_snapshot()["dreamOwed"] is True

    await loop.dream_before_sleep()
    assert loop._dreamer.runs == 1
    assert loop.power_snapshot()["dreamOwed"] is False
    # The pass is remembered across a restart too.
    assert _loop(tmp_path).power_snapshot()["dreamOwed"] is False

    await loop.dream_before_sleep()
    assert loop._dreamer.runs == 1


async def test_dirty_memory_is_consolidated_before_sleep(tmp_path):
    loop = _loop(tmp_path, dirty=True)
    assert loop.power_snapshot()["dreamOwed"] is True
    await loop.dream_before_sleep()
    assert loop._consolidate_tool.runs == 1
    assert loop.power_snapshot()["dreamOwed"] is False


def test_nightly_dream_is_due_only_when_there_is_something_to_learn(tmp_path):
    loop = _loop(tmp_path)
    assert loop.power_snapshot()["nextDreamAt"] is None
    loop._note_user_activity(time.time() - 60)
    next_dream = loop.power_snapshot()["nextDreamAt"]
    assert next_dream is not None and 0 < next_dream - time.time() <= 24 * 3600


def test_no_dream_owed_without_a_dreamer(tmp_path):
    loop = _loop(tmp_path, dreamer=False)
    loop._memory_gov = None
    loop._note_user_activity(time.time())
    snapshot = loop.power_snapshot()
    assert snapshot["dreamOwed"] is False and snapshot["nextDreamAt"] is None


async def test_turns_in_progress_are_counted(tmp_path):
    loop = _loop(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def admitted(msg):
        started.set()
        await release.wait()
        return None

    loop._process_admitted_message = admitted  # type: ignore[method-assign]
    task = asyncio.create_task(loop._process_message(object()))
    await started.wait()
    assert loop.power_snapshot()["turns"] == 1
    release.set()
    await task
    assert loop.power_snapshot()["turns"] == 0


async def test_idle_timer_fires_for_activity_from_before_a_restart(tmp_path, monkeypatch):
    first = _loop(tmp_path)
    first._note_user_activity(time.time() - 3600)
    loop = _loop(tmp_path)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            loop._running = False

    monkeypatch.setattr("flowly.agent.loop.asyncio.sleep", fake_sleep)
    await loop._dreamer_idle_timer()
    assert loop._dreamer.runs == 1
    assert loop.power_snapshot()["dreamOwed"] is False
