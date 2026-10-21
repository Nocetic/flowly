"""The heartbeat tells the Flowly server when it is next due, and runs on demand.

A cloud machine sleeps between heartbeats. The server wakes it for the next one
only if Core says when that is: never when HEARTBEAT.md has no active task, and
never outside the active hours. A heartbeat the server triggers must not run a
second time from Core's own timer moments later.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from flowly.heartbeat import service as hb
from flowly.heartbeat.service import HEARTBEAT_OK_TOKEN, HeartbeatService

ACTIVE = "## Active Tasks\n\n- [ ] Check the inbox\n"


def _service(tmp_path: Path, *, tasks: str | None = ACTIVE, active_hours=None, reply=HEARTBEAT_OK_TOKEN):
    if tasks is not None:
        (tmp_path / "HEARTBEAT.md").write_text(tasks, encoding="utf-8")
    calls: list[str] = []

    async def on_heartbeat(prompt: str) -> str:
        calls.append(prompt)
        return reply

    service = HeartbeatService(workspace=tmp_path, on_heartbeat=on_heartbeat, interval_s=1800,
                               active_hours=active_hours)
    return service, calls


def test_no_next_due_until_the_loop_runs(tmp_path):
    service, _ = _service(tmp_path)
    assert service.next_due_at() is None


def test_next_due_is_the_next_tick_when_there_are_active_tasks(tmp_path):
    service, _ = _service(tmp_path)
    service._running = True
    service._next_tick_at = 5_000.0
    assert service.next_due_at() == 5_000.0


def test_no_next_due_without_active_tasks(tmp_path):
    service, _ = _service(tmp_path, tasks="# Heartbeat Tasks\n\n## Active Tasks\n\n## Completed\n- [x] old\n")
    service._running = True
    service._next_tick_at = 5_000.0
    assert service.next_due_at() is None


def test_no_next_due_when_disabled(tmp_path):
    service, _ = _service(tmp_path)
    service.enabled = False
    service._running = True
    service._next_tick_at = 5_000.0
    assert service.next_due_at() is None


def test_next_due_skips_ticks_outside_active_hours(tmp_path):
    tz = ZoneInfo("Europe/Istanbul")
    service, _ = _service(tmp_path, active_hours={"start": "09:00", "end": "23:00", "timezone": "Europe/Istanbul"})
    service._running = True
    # The next tick is at 02:00; ticks follow every 30 minutes, the first one
    # inside the window is 09:00.
    service._next_tick_at = datetime(2026, 10, 22, 2, 0, tzinfo=tz).timestamp()
    assert service.next_due_at() == datetime(2026, 10, 22, 9, 0, tzinfo=tz).timestamp()


def test_next_due_inside_active_hours_is_the_next_tick(tmp_path):
    tz = ZoneInfo("Europe/Istanbul")
    service, _ = _service(tmp_path, active_hours={"start": "09:00", "end": "23:00", "timezone": "Europe/Istanbul"})
    service._running = True
    tick = datetime(2026, 10, 22, 12, 15, tzinfo=tz).timestamp()
    service._next_tick_at = tick
    assert service.next_due_at() == tick


async def test_trigger_reports_ok_and_postpones_the_own_timer(tmp_path, monkeypatch):
    service, calls = _service(tmp_path)
    changes: list[float | None] = []
    service.set_schedule_listener(lambda: changes.append(service._next_tick_at))
    service._running = True
    service._next_tick_at = 1_000.0
    monkeypatch.setattr(hb.time, "time", lambda: 2_000.0)

    assert await service.trigger() == "ok"
    assert len(calls) == 1
    # The periodic tick moves a full interval past the triggered run.
    assert service._next_tick_at == 2_000.0 + 1800
    assert changes and changes[-1] == 3_800.0


async def test_trigger_reports_acted_and_no_tasks(tmp_path):
    service, calls = _service(tmp_path, reply="I sent the summary.")
    assert await service.trigger() == "acted"
    (tmp_path / "HEARTBEAT.md").write_text("## Active Tasks\n", encoding="utf-8")
    assert await service.trigger() == "no-tasks"
    assert len(calls) == 1


async def test_a_running_heartbeat_is_busy_and_not_run_twice(tmp_path):
    release = asyncio.Event()
    started = asyncio.Event()
    (tmp_path / "HEARTBEAT.md").write_text(ACTIVE, encoding="utf-8")
    calls = []

    async def on_heartbeat(prompt: str) -> str:
        calls.append(prompt)
        started.set()
        await release.wait()
        return HEARTBEAT_OK_TOKEN

    service = HeartbeatService(workspace=tmp_path, on_heartbeat=on_heartbeat, interval_s=1800)
    first = asyncio.create_task(service.trigger())
    await started.wait()
    assert service.running is True
    assert await service.trigger() == "busy"
    release.set()
    assert await first == "ok"
    assert service.running is False
    assert len(calls) == 1


async def test_the_loop_does_not_tick_when_a_trigger_pushed_it_out(tmp_path, monkeypatch):
    service, calls = _service(tmp_path)
    now = [100.0]
    waits: list[float] = []

    async def fake_sleep_until(deadline):
        waits.append(deadline)
        if len(waits) == 1:
            # A server trigger ran during the wait and pushed the tick out.
            now[0] = deadline - 10
            await service.trigger()
            now[0] = deadline
        else:
            service._running = False

    monkeypatch.setattr(hb, "sleep_until_wall", fake_sleep_until)
    monkeypatch.setattr(hb.time, "time", lambda: now[0])
    service._running = True
    await service._run_loop()
    # One run (the trigger), and the loop waited again for the new deadline.
    assert len(calls) == 1
    assert waits == [1900.0, 1890.0 + 1800]


@pytest.mark.parametrize("at,expected", [
    (datetime(2026, 10, 22, 8, 59, tzinfo=ZoneInfo("UTC")), False),
    (datetime(2026, 10, 22, 9, 0, tzinfo=ZoneInfo("UTC")), True),
    (datetime(2026, 10, 22, 23, 30, tzinfo=ZoneInfo("UTC")), False),
])
def test_active_hours_at_a_given_time(at, expected):
    assert hb._is_within_active_hours("09:00", "23:00", "UTC", at=at.timestamp()) is expected
