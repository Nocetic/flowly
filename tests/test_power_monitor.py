"""Core answers the Flowly server's sleep questions (PROTOCOL.md B4).

A cloud machine may sleep only when nothing is running and nothing is due soon,
and never before memory learning has run ("dream before sleep").
"""

from __future__ import annotations

import asyncio

import pytest

from flowly.power.monitor import (
    DUE_SOON_SECONDS,
    JUST_WOKE_SECONDS,
    RESUME_GAP_SECONDS,
    PowerMonitor,
)


class Clock:
    def __init__(self, wall: float = 1_000_000.0):
        self.wall_now = wall
        self.mono_now = 50.0

    def wall(self) -> float:
        return self.wall_now

    def mono(self) -> float:
        return self.mono_now

    def advance(self, seconds: float, *, suspended: bool = False) -> None:
        self.wall_now += seconds
        if not suspended:
            self.mono_now += seconds


class FakeAgent:
    def __init__(self):
        self.turns = 0
        self.dream_owed = False
        self.dream_running = False
        self.next_dream_at: float | None = None
        self.last_user_at: float | None = None
        self.dreams = 0
        self.release: asyncio.Event | None = None

    def power_snapshot(self) -> dict:
        return {
            "turns": self.turns,
            "dreamOwed": self.dream_owed,
            "dreamRunning": self.dream_running,
            "nextDreamAt": self.next_dream_at,
            "lastUserActivityAt": self.last_user_at,
        }

    async def dream_before_sleep(self) -> None:
        self.dream_running = True
        try:
            if self.release is not None:
                await self.release.wait()
            self.dreams += 1
            self.dream_owed = False
        finally:
            self.dream_running = False


class FakeCron:
    def __init__(self):
        self.runs: list[dict] = []
        self.next_ms: int | None = None
        self.listeners = []

    def running_runs(self):
        return list(self.runs)

    def next_due_at_ms(self):
        return self.next_ms

    def add_schedule_listener(self, callback):
        self.listeners.append(callback)


class FakeHeartbeat:
    def __init__(self):
        self.running = False
        self.next_at: float | None = None
        self.listener = None
        self.outcome = "ok"

    def next_due_at(self):
        return self.next_at

    def set_schedule_listener(self, callback):
        self.listener = callback

    async def trigger(self):
        return self.outcome


def _monitor(**kwargs):
    clock = Clock()
    agent, cron, heartbeat = FakeAgent(), FakeCron(), FakeHeartbeat()
    flushed: list[bool] = []
    monitor = PowerMonitor(agent=agent, cron=cron, heartbeat=heartbeat,
                           flush=lambda: flushed.append(True), wall=clock.wall, monotonic=clock.mono, **kwargs)
    # Past the start-up grace period unless a test says otherwise.
    clock.advance(JUST_WOKE_SECONDS + 1)
    return monitor, clock, agent, cron, heartbeat, flushed


def test_an_idle_agent_reports_idle():
    monitor, *_ = _monitor()
    status = monitor.status()
    assert status == {"idle": True, "busy": [], "nextDueAt": None, "lastActivityAt": None}


def test_each_kind_of_work_has_its_busy_code():
    monitor, clock, agent, cron, heartbeat, _ = _monitor()
    agent.turns = 1
    cron.runs = [{"jobId": "j"}]
    heartbeat.running = True
    agent.dream_running = True
    monitor.add_busy_probe("screen", lambda: True)
    monitor.add_busy_probe("maintenance", lambda: True)
    status = monitor.status()
    assert status["idle"] is False
    assert status["busy"] == ["turn", "run", "heartbeat", "dream", "screen", "maintenance"]


def test_a_failing_probe_counts_as_busy_rather_than_sleeping_through_work():
    monitor, *_ = _monitor()

    def broken():
        raise RuntimeError("probe bug")

    monitor.add_busy_probe("screen", broken)
    assert monitor.status()["busy"] == ["screen"]


def test_unknown_probe_codes_are_rejected():
    monitor, *_ = _monitor()
    with pytest.raises(ValueError):
        monitor.add_busy_probe("coffee", lambda: True)


def test_recent_activity_uses_the_idle_minutes_asked_for():
    monitor, clock, *_ = _monitor()
    monitor.touch()
    clock.advance(5 * 60)
    assert monitor.status()["busy"] == []
    assert monitor.status(idle_minutes=10)["busy"] == ["recent-activity"]
    assert monitor.status(idle_minutes=4)["busy"] == []
    assert monitor.status()["lastActivityAt"] == int((clock.wall_now - 5 * 60) * 1000)


def test_last_activity_includes_the_agents_last_user_message():
    monitor, clock, agent, *_ = _monitor()
    agent.last_user_at = clock.wall_now - 30
    assert monitor.status(idle_minutes=1)["busy"] == ["recent-activity"]
    assert monitor.status()["lastActivityAt"] == int(agent.last_user_at * 1000)


def test_just_woke_after_start_and_after_reconnect():
    clock = Clock()
    monitor = PowerMonitor(wall=clock.wall, monotonic=clock.mono)
    assert monitor.status()["busy"] == ["just-woke"]
    clock.advance(JUST_WOKE_SECONDS + 1)
    assert monitor.status()["busy"] == []
    monitor.mark_connected()
    assert monitor.status()["busy"] == ["just-woke"]


def test_a_suspend_is_seen_as_a_wake_and_ends_the_sleeping_state():
    monitor, clock, *_ = _monitor()
    monitor._sleeping = True
    monitor.status()  # baseline for the clocks
    clock.advance(RESUME_GAP_SECONDS + 600, suspended=True)
    status = monitor.status()
    assert monitor.sleeping is False
    assert "just-woke" in status["busy"]


def test_next_due_is_the_earliest_of_cron_heartbeat_and_nightly_dream():
    monitor, clock, agent, cron, heartbeat, _ = _monitor()
    cron.next_ms = int((clock.wall_now + 7200) * 1000)
    heartbeat.next_at = clock.wall_now + 3600
    agent.next_dream_at = clock.wall_now + 10_000
    assert monitor.next_due_at_ms() == int((clock.wall_now + 3600) * 1000)
    heartbeat.next_at = None
    assert monitor.next_due_at_ms() == cron.next_ms


def test_due_soon_blocks_sleep():
    monitor, clock, agent, cron, *_ = _monitor()
    cron.next_ms = int((clock.wall_now + DUE_SOON_SECONDS - 5) * 1000)
    assert monitor.status()["busy"] == ["due-soon"]


async def test_sleep_refuses_while_busy_without_side_effects():
    monitor, clock, agent, cron, heartbeat, flushed = _monitor()
    agent.turns = 1
    assert await monitor.sleep(idle_minutes=10) == {"ok": False, "busy": ["turn"]}
    assert monitor.sleeping is False
    assert flushed == []


async def test_sleep_accepts_when_idle_and_flushes_first():
    monitor, clock, agent, cron, heartbeat, flushed = _monitor()
    heartbeat.next_at = clock.wall_now + 3600
    result = await monitor.sleep(idle_minutes=10)
    assert result == {"ok": True, "nextDueAt": int((clock.wall_now + 3600) * 1000)}
    assert monitor.sleeping is True
    assert flushed == [True]
    monitor.wake()
    assert monitor.sleeping is False


async def test_dream_before_sleep_runs_the_owed_pass_then_allows_sleep():
    monitor, clock, agent, *_ = _monitor()
    agent.dream_owed = True
    agent.release = asyncio.Event()
    assert monitor.status()["busy"] == ["dream"]

    first = await monitor.sleep(idle_minutes=10)
    assert first == {"ok": False, "busy": ["dream"]}
    assert monitor.sleeping is False
    # Asking again while it runs neither sleeps nor starts a second pass.
    assert await monitor.sleep(idle_minutes=10) == {"ok": False, "busy": ["dream"]}
    agent.release.set()
    await monitor.wait_for_dream()
    assert agent.dreams == 1
    assert await monitor.sleep(idle_minutes=10) == {"ok": True, "nextDueAt": None}


async def test_dream_is_not_started_when_other_work_is_running():
    monitor, clock, agent, *_ = _monitor()
    agent.dream_owed = True
    agent.turns = 1
    assert await monitor.sleep(idle_minutes=10) == {"ok": False, "busy": ["turn", "dream"]}
    assert agent.dreams == 0
    assert monitor._dream_task is None


async def test_a_dream_that_cannot_settle_does_not_keep_the_machine_awake_forever():
    monitor, clock, agent, *_ = _monitor()
    agent.dream_owed = True

    async def failing():
        raise RuntimeError("extractor down")

    agent.dream_before_sleep = failing
    assert (await monitor.sleep(idle_minutes=10))["ok"] is False
    await monitor.wait_for_dream()
    # The attempt happened; with no new activity since, it no longer blocks.
    assert monitor.status()["busy"] == []
    assert (await monitor.sleep(idle_minutes=10))["ok"] is True
    # New user activity makes the owed dream count again.
    monitor.wake()
    clock.advance(1)
    monitor.touch()
    clock.advance(1)
    assert "dream" in monitor.status()["busy"]


async def test_next_due_listeners_hear_only_changes():
    monitor, clock, agent, cron, heartbeat, _ = _monitor()
    heard: list[int | None] = []
    monitor.add_next_due_listener(heard.append)
    await monitor.check_next_due()
    assert heard == [None]
    await monitor.check_next_due()
    assert heard == [None]
    heartbeat.next_at = clock.wall_now + 600
    # Services call their schedule listener when their own schedule moves.
    heartbeat.listener()
    await asyncio.sleep(0)
    assert heard == [None, int((clock.wall_now + 600) * 1000)]
    cron.next_ms = int((clock.wall_now + 60) * 1000)
    cron.listeners[0]()
    await asyncio.sleep(0)
    assert heard[-1] == cron.next_ms
    await monitor.check_next_due(force=True)
    assert heard[-1] == cron.next_ms and len(heard) == 4


async def test_heartbeat_trigger_reports_outcome_and_next_due():
    monitor, clock, agent, cron, heartbeat, _ = _monitor()
    heartbeat.next_at = clock.wall_now + 1800
    assert await monitor.trigger_heartbeat() == {
        "ok": True, "outcome": "ok", "nextDueAt": int((clock.wall_now + 1800) * 1000),
    }


async def test_heartbeat_trigger_without_a_heartbeat_service():
    clock = Clock()
    monitor = PowerMonitor(wall=clock.wall, monotonic=clock.mono)
    assert await monitor.trigger_heartbeat() == {"ok": False, "outcome": "unavailable", "nextDueAt": None}
