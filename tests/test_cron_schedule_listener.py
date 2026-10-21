"""Cron tells listeners when its next due time may have changed.

The Flowly server wakes a sleeping cloud machine for the next scheduled job, so
Core must announce a new earliest job the moment one is added, edited or removed.
"""

from __future__ import annotations

from flowly.cron.service import CronService
from flowly.cron.types import CronSchedule


async def test_listeners_hear_about_added_and_removed_jobs(tmp_path):
    service = CronService(tmp_path / "jobs.json")
    await service.start()
    heard: list[int | None] = []
    service.add_schedule_listener(lambda: heard.append(service.next_due_at_ms()))
    try:
        job = service.add_job(
            name="report", schedule=CronSchedule(kind="every", every_ms=3_600_000), message="send the report",
        )
        assert heard and heard[-1] == job.state.next_run_at_ms
        assert service.next_due_at_ms() == job.state.next_run_at_ms
        service.remove_job(job.id)
        assert heard[-1] is None
        assert service.next_due_at_ms() is None
    finally:
        service.stop()


async def test_a_failing_listener_does_not_break_scheduling(tmp_path):
    service = CronService(tmp_path / "jobs.json")
    await service.start()

    def broken() -> None:
        raise RuntimeError("listener bug")

    service.add_schedule_listener(broken)
    try:
        job = service.add_job(
            name="report", schedule=CronSchedule(kind="every", every_ms=3_600_000), message="x",
        )
        assert service.next_due_at_ms() == job.state.next_run_at_ms
    finally:
        service.stop()
