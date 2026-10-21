"""Timers must follow the wall clock across a suspended machine.

A suspended cloud machine (Fly suspend, a laptop lid) stops CLOCK_MONOTONIC:
measured on Fly, a 60 s asyncio.sleep fired 191 s late after a 196 s suspend.
A job due during the suspension must run right after resume, not late by the
whole time asleep.
"""

import asyncio

import pytest

from flowly.utils import wall_clock


def run(coro):
    return asyncio.run(coro)


def test_returns_soon_after_a_wall_clock_jump(monkeypatch):
    now = [1000.0]
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        # The machine was suspended for an hour during this short sleep.
        now[0] += 3600 if len(slept) == 1 else seconds

    monkeypatch.setattr(wall_clock.time, "time", lambda: now[0])
    monkeypatch.setattr(wall_clock.asyncio, "sleep", fake_sleep)
    run(wall_clock.sleep_until_wall(1000.0 + 1800))
    # One bounded chunk, then the deadline is seen as passed.
    assert slept == [wall_clock.WALL_CLOCK_CHUNK_S]


def test_never_sleeps_longer_than_one_chunk_or_past_the_deadline(monkeypatch):
    now = [0.0]
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(wall_clock.time, "time", lambda: now[0])
    monkeypatch.setattr(wall_clock.asyncio, "sleep", fake_sleep)
    run(wall_clock.sleep_until_wall(70.0))
    assert slept == [30.0, 30.0, 10.0]
    assert now[0] == 70.0


def test_a_past_deadline_returns_without_sleeping(monkeypatch):
    async def fail(_seconds):
        pytest.fail("must not sleep")

    monkeypatch.setattr(wall_clock.asyncio, "sleep", fail)
    run(wall_clock.sleep_until_wall(wall_clock.time.time() - 5))


def test_cron_waits_on_the_wall_clock_for_its_next_job(monkeypatch, tmp_path):
    from flowly.cron import service as cron_service

    waited = []

    async def fake_sleep_until(deadline):
        waited.append(deadline)

    monkeypatch.setattr(cron_service, "sleep_until_wall", fake_sleep_until)
    svc = cron_service.CronService(tmp_path / "jobs.json")
    svc._running = True
    monkeypatch.setattr(svc, "_get_next_wake_ms", lambda: 5_000_000)
    monkeypatch.setattr(svc, "_start_scheduler_pass", lambda: waited.append("pass"))

    async def go():
        svc._arm_timer()
        await svc._timer_task if svc._timer_task else None

    run(go())
    assert waited == [5_000.0, "pass"]


def test_heartbeat_waits_on_the_wall_clock(monkeypatch, tmp_path):
    from flowly.heartbeat import service as hb

    waited = []

    async def fake_sleep_until(deadline):
        waited.append(deadline)
        svc._running = False

    monkeypatch.setattr(hb, "sleep_until_wall", fake_sleep_until)
    monkeypatch.setattr(hb.time, "time", lambda: 100.0)
    svc = hb.HeartbeatService(workspace=tmp_path, on_heartbeat=None, interval_s=1800)
    svc._running = True
    run(svc._run_loop())
    assert waited == [1900.0]
