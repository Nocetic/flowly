"""Regression coverage for durable cron lifecycle and retained results."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from flowly.channels import feature_rpc
from flowly.cron import service as cron_service
from flowly.cron.service import CronService, CronStoreLoadError
from flowly.cron.types import CronSchedule


def _legacy_job(
    *,
    job_id: str,
    enabled: bool,
    kind: str,
    at_ms: int | None = None,
    last_run_at_ms: int | None = None,
    next_run_at_ms: int | None = None,
) -> dict:
    return {
        "id": job_id,
        "name": job_id,
        "enabled": enabled,
        "schedule": {
            "kind": kind,
            "atMs": at_ms,
            "everyMs": 60_000 if kind == "every" else None,
            "expr": None,
            "tz": None,
        },
        "payload": {"kind": "agent_turn", "message": "hello"},
        "state": {
            "nextRunAtMs": next_run_at_ms,
            "lastRunAtMs": last_run_at_ms,
            "lastStatus": "ok" if last_run_at_ms else None,
            "retryAttempt": 0,
        },
        "createdAtMs": 1,
        "updatedAtMs": last_run_at_ms or 1,
        "repeatTimes": 1 if kind == "at" else None,
        "repeatCompleted": 1 if last_run_at_ms else 0,
    }


async def _fire_scheduled(service: CronService, job) -> None:
    """Run through the scheduler path rather than the manual-run path."""
    job.state.next_run_at_ms = cron_service._now_ms() - 1
    await service._run_due_jobs()


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    """Wait for an event-loop condition without fixed timer assumptions."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not satisfied before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_one_shot_success_becomes_durable_completed_history(tmp_path: Path):
    store = tmp_path / "jobs.json"
    service = CronService(store)
    job = service.add_job(
        "one-shot",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="retained answer")

    await _fire_scheduled(service, job)

    assert job.lifecycle == "completed"
    assert job.enabled is False
    assert job.state.next_run_at_ms is None
    assert job.state.last_run_id
    assert service.list_jobs() == []
    assert service.list_jobs(include_disabled=True) == [job]

    restarted = CronService(store)
    (persisted,) = restarted.list_jobs(include_disabled=True)
    assert persisted.lifecycle == "completed"
    assert persisted.state.last_run_id == job.state.last_run_id
    assert restarted._get_next_wake_ms() is None


@pytest.mark.asyncio
async def test_start_stop_restart_keeps_completed_job_out_of_timer(tmp_path: Path):
    store = tmp_path / "jobs.json"
    service = CronService(store)
    job = service.add_job(
        "restart-smoke",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="answer")
    await _fire_scheduled(service, job)

    restarted = CronService(store)
    await restarted.start()
    try:
        (persisted,) = restarted.list_jobs(include_disabled=True)
        assert persisted.lifecycle == "completed"
        assert restarted._get_next_wake_ms() is None
        assert restarted._timer_task is None
    finally:
        restarted.stop()


@pytest.mark.asyncio
async def test_timer_rearming_mutations_do_not_cancel_an_active_run(tmp_path: Path):
    """The QA reproducer plus every mutation that re-arms the wake timer."""
    service = CronService(tmp_path / "jobs.json")
    due = cron_service._now_ms() + 75
    active = service.add_job(
        "running A",
        CronSchedule(kind="at", at_ms=due),
        "A",
    )
    other = service.add_job(
        "unrelated B",
        CronSchedule(kind="at", at_ms=due + 3_600_000),
        "B",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    calls = 0
    cancellations = 0
    observed_run_ids: list[str] = []

    async def run(job):
        nonlocal calls, cancellations
        if job.id != active.id:
            return "other"
        calls += 1
        observed_run_ids.append(service.current_run(job.id)["runId"])
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellations += 1
            raise
        return "OK"

    async def on_complete(_name, data):
        if data["jobId"] == active.id:
            completed.set()

    service.on_job = run
    service.on_complete = on_complete
    await service.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        run_id = service.current_run(active.id)["runId"]

        added = service.add_job(
            "added C",
            CronSchedule(kind="at", at_ms=due + 7_200_000),
            "C",
        )
        service.update_job(added.id, {"name": "edited C", "message": "edited"})
        service.enable_job(added.id, False)
        service.enable_job(added.id, True)
        assert service.remove_job(other.id) is True
        assert service.remove_job(other.id, purge=True) is True
        await service.reload()

        assert service.current_run(active.id)["runId"] == run_id
        assert cancellations == 0
        assert calls == 1

        release.set()
        await asyncio.wait_for(completed.wait(), timeout=2)
        await _wait_until(
            lambda: service._scheduler_task is None
            or service._scheduler_task.done()
        )
        await asyncio.sleep(0.1)

        assert calls == 1
        assert cancellations == 0
        assert observed_run_ids == [run_id]
        records = service._read_run_records(active.id)
        assert len(records) == 1
        assert records[0]["runId"] == run_id
        assert records[0]["status"] == "ok"
        assert service.current_run(active.id) is None
    finally:
        release.set()
        await service.shutdown(grace_period_s=0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["archive", "purge", "pause", "reschedule"])
async def test_mutated_later_job_is_not_run_from_stale_due_snapshot(
    tmp_path: Path,
    mutation: str,
):
    due = cron_service._now_ms() + 75
    service = CronService(tmp_path / "jobs.json")
    first = service.add_job(
        "held A",
        CronSchedule(kind="at", at_ms=due),
        "A",
    )
    later = service.add_job(
        "also due B",
        CronSchedule(kind="at", at_ms=due),
        "B",
    )
    first_started = asyncio.Event()
    release = asyncio.Event()
    first_completed = asyncio.Event()
    later_calls = 0

    async def run(job):
        nonlocal later_calls
        if job.id == first.id:
            first_started.set()
            await release.wait()
            return "A done"
        later_calls += 1
        return "B should not run"

    async def on_complete(_name, data):
        if data["jobId"] == first.id:
            first_completed.set()

    service.on_job = run
    service.on_complete = on_complete
    await service.start()
    try:
        await asyncio.wait_for(first_started.wait(), timeout=2)
        if mutation == "archive":
            assert service.remove_job(later.id) is True
        elif mutation == "purge":
            assert service.remove_job(later.id, purge=True) is True
        elif mutation == "pause":
            assert service.enable_job(later.id, False) is later
        else:
            assert service.update_job(later.id, {
                "schedule": CronSchedule(
                    kind="at",
                    at_ms=cron_service._now_ms() + 3_600_000,
                ),
            }) is later

        release.set()
        await asyncio.wait_for(first_completed.wait(), timeout=2)
        await _wait_until(
            lambda: service._scheduler_task is None
            or service._scheduler_task.done()
        )
        assert later_calls == 0
        assert service.current_run(later.id) is None
    finally:
        release.set()
        await service.shutdown(grace_period_s=0.5)


@pytest.mark.asyncio
async def test_shutdown_drains_active_timer_run_within_grace_period(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "graceful shutdown",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 75),
        "hello",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    cancellations = 0

    async def run(_job):
        nonlocal cancellations
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellations += 1
            raise
        return "finished during grace"

    service.on_job = run
    await service.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    run_id = service.current_run(job.id)["runId"]
    shutdown = asyncio.create_task(service.shutdown(grace_period_s=1))
    await asyncio.sleep(0.05)
    assert cancellations == 0
    release.set()
    await asyncio.wait_for(shutdown, timeout=2)

    records = service._read_run_records(job.id)
    assert len(records) == 1
    assert records[0]["runId"] == run_id
    assert records[0]["status"] == "ok"
    assert cancellations == 0


@pytest.mark.asyncio
async def test_forced_shutdown_persists_exact_cancelled_timer_run(tmp_path: Path):
    store = tmp_path / "jobs.json"
    service = CronService(store)
    job = service.add_job(
        "forced shutdown",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 75),
        "hello",
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()
    completions: list[dict] = []

    async def run(_job):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def on_complete(_name, data):
        completions.append(data)

    service.on_job = run
    service.on_complete = on_complete
    await service.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    run_id = service.current_run(job.id)["runId"]

    await service.shutdown(grace_period_s=0)

    assert cancelled.is_set()
    assert service.current_run(job.id) is None
    assert len(completions) == 1
    assert completions[0]["runId"] == run_id
    assert completions[0]["status"] == "error"
    assert completions[0]["cancelled"] is True
    records = service._read_run_records(job.id, include_content=True)
    assert len(records) == 1
    assert records[0]["runId"] == run_id
    assert records[0]["status"] == "error"
    assert "Cancelled during scheduler shutdown" in records[0]["content"]

    restarted_calls = 0

    async def should_not_run(_job):
        nonlocal restarted_calls
        restarted_calls += 1
        return "unexpected"

    restarted = CronService(store, on_job=should_not_run)
    await restarted.start()
    try:
        persisted = restarted.list_jobs(include_disabled=True)[0]
        assert persisted.lifecycle == "completed"
        assert persisted.state.last_run_id == run_id
        assert restarted._timer_task is None
        await asyncio.sleep(0.1)
        assert restarted_calls == 0
    finally:
        await restarted.shutdown(grace_period_s=0)


@pytest.mark.asyncio
async def test_shutdown_retires_detached_rpc_reservation_before_child_starts(
    tmp_path: Path,
    monkeypatch,
):
    service = CronService(tmp_path / "jobs.json")
    monkeypatch.setattr(feature_rpc, "_cron_provider", lambda: service)
    job = service.add_job(
        "detached race",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    callback_calls = 0

    async def run(_job):
        nonlocal callback_calls
        callback_calls += 1
        return "must not start"

    service.on_job = run
    response = await feature_rpc.cron_run({
        "id": job.id,
        "force": True,
        "wait": False,
    })

    # cron_run created the child, but has not yielded an event-loop turn to
    # it. Shutdown therefore sees an exact advertised reservation with no
    # owner and retires it atomically.
    await service.shutdown(grace_period_s=0)
    await asyncio.sleep(0)

    assert response["started"] is True
    assert callback_calls == 0
    assert service.current_run(job.id) is None
    records = service._read_run_records(job.id, include_content=True)
    assert len(records) == 1
    assert records[0]["runId"] == response["runId"]
    assert records[0]["status"] == "error"
    assert "Cancelled during scheduler shutdown" in records[0]["content"]
    # A cancelled manual fire records history without consuming or changing
    # the recurring schedule's lifecycle.
    assert job.lifecycle == "scheduled"
    assert job.repeat_completed == 0


@pytest.mark.asyncio
async def test_terminal_failure_is_retained_and_completes_repeat_slot(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "failing",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "hello",
    )

    async def fail(_job):
        raise RuntimeError("provider unavailable")

    service.on_job = fail
    await _fire_scheduled(service, job)

    assert job.lifecycle == "completed"
    assert job.state.last_status == "error"
    assert job.state.last_error == "provider unavailable"
    assert job.repeat_completed == 1
    records = service._read_run_records(job.id, include_content=True)
    assert records[0]["status"] == "error"
    assert "provider unavailable" in records[0]["content"]


@pytest.mark.asyncio
async def test_retry_exhaustion_consumes_one_slot_then_completes(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "retry-exhaustion",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "hello",
        retry_max_attempts=1,
        retry_backoff_ms=[60_000],
    )

    async def fail(_job):
        raise RuntimeError("still unavailable")

    service.on_job = fail
    await _fire_scheduled(service, job)
    assert job.lifecycle == "scheduled"
    assert job.state.retry_attempt == 1
    assert job.repeat_completed == 0

    await _fire_scheduled(service, job)
    assert job.lifecycle == "completed"
    assert job.state.retry_attempt == 0
    assert job.repeat_completed == 1
    assert len(service._read_run_records(job.id)) == 2


@pytest.mark.asyncio
async def test_n_run_limit_counts_mixed_terminal_outcomes(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "three-runs",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
        repeat_times=3,
    )
    outcomes = iter(["first", RuntimeError("second failed"), "third"])

    async def run(_job):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    service.on_job = run
    await _fire_scheduled(service, job)
    assert job.lifecycle == "scheduled"
    await _fire_scheduled(service, job)
    assert job.lifecycle == "scheduled"
    assert job.state.last_status == "error"
    await _fire_scheduled(service, job)

    assert job.lifecycle == "completed"
    assert job.enabled is False
    assert job.state.next_run_at_ms is None
    assert job.repeat_completed == 3
    assert [record["status"] for record in service._read_run_records(job.id)] == [
        "ok",
        "error",
        "ok",
    ]


@pytest.mark.asyncio
async def test_retry_backoff_survives_reload_exactly(tmp_path: Path):
    store = tmp_path / "jobs.json"
    service = CronService(store)
    job = service.add_job(
        "retry",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "hello",
        retry_max_attempts=2,
        retry_backoff_ms=[123_456],
    )

    async def fail(_job):
        raise RuntimeError("temporary")

    service.on_job = fail
    await _fire_scheduled(service, job)
    retry_due = job.state.next_run_at_ms
    assert job.lifecycle == "scheduled"
    assert job.state.retry_attempt == 1

    reloaded = CronService(store)
    reloaded._load_store()
    reloaded._recompute_next_runs()
    (persisted,) = reloaded.list_jobs(include_disabled=True)
    assert persisted.state.retry_attempt == 1
    assert persisted.state.next_run_at_ms == retry_due


def test_legacy_migration_distinguishes_paused_pending_and_completed(tmp_path: Path):
    now = cron_service._now_ms()
    store = tmp_path / "jobs.json"
    store.write_text(json.dumps({
        "version": 1,
        "jobs": [
            _legacy_job(job_id="paused", enabled=False, kind="every"),
            _legacy_job(
                job_id="overdue",
                enabled=True,
                kind="at",
                at_ms=now - 60_000,
                next_run_at_ms=None,
            ),
            _legacy_job(
                job_id="done",
                enabled=True,
                kind="at",
                at_ms=now - 120_000,
                last_run_at_ms=now - 110_000,
            ),
        ],
    }), encoding="utf-8")

    service = CronService(store)
    service._load_store()
    service._recompute_next_runs()
    by_id = {
        job.id: job
        for job in service.list_jobs(include_disabled=True, include_archived=True)
    }

    assert by_id["paused"].lifecycle == "paused"
    assert by_id["paused"].enabled is False
    assert by_id["overdue"].lifecycle == "scheduled"
    assert by_id["overdue"].state.next_run_at_ms == now - 60_000
    assert by_id["done"].lifecycle == "completed"
    assert by_id["done"].state.next_run_at_ms is None

    service._save_store()
    reloaded = CronService(store)
    again = {
        job.id: job.lifecycle
        for job in reloaded.list_jobs(include_disabled=True, include_archived=True)
    }
    assert again == {"paused": "paused", "overdue": "scheduled", "done": "completed"}


@pytest.mark.asyncio
async def test_manual_rerun_of_completed_job_stays_completed(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "rerun",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "hello",
    )
    calls = 0

    async def run(_job):
        nonlocal calls
        calls += 1
        return f"answer {calls}"

    service.on_job = run
    await _fire_scheduled(service, job)
    scheduled_count = job.repeat_completed
    assert await service.run_job(job.id, force=True) is True

    assert calls == 2
    assert job.lifecycle == "completed"
    assert job.state.next_run_at_ms is None
    assert job.repeat_completed == scheduled_count
    assert len(service._read_run_records(job.id)) == 2


@pytest.mark.asyncio
async def test_same_job_manual_runs_are_serialized(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "serial",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def run(_job):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "ok"

    service.on_job = run
    first = asyncio.create_task(service.run_job(job.id))
    await entered.wait()
    assert await service.run_job(job.id) is False
    release.set()
    assert await first is True
    assert calls == 1


@pytest.mark.asyncio
async def test_manual_run_racing_due_timer_is_serialized_without_losing_due_fire(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "manual-timer-race",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    job.state.next_run_at_ms = cron_service._now_ms() - 1
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def run(_job):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return "ok"

    service.on_job = run
    manual = asyncio.create_task(service.run_job(job.id))
    await entered.wait()
    assert service._get_next_wake_ms() is None
    due_before = job.state.next_run_at_ms
    await service._run_due_jobs()
    assert calls == 1
    assert job.state.next_run_at_ms == due_before

    release.set()
    assert await manual is True
    await service._run_due_jobs()
    assert calls == 2


@pytest.mark.asyncio
async def test_same_millisecond_runs_have_distinct_durable_ids(tmp_path: Path, monkeypatch):
    fixed = 1_800_000_000_000
    monkeypatch.setattr(cron_service, "_now_ms", lambda: fixed)
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "fast",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="ok")

    assert await service.run_job(job.id) is True
    assert await service.run_job(job.id) is True

    records = service._read_run_records(job.id)
    assert len(records) == 2
    assert len({record["runId"] for record in records}) == 2
    assert len(list((tmp_path / "output" / job.id).glob("*.md"))) == 2


@pytest.mark.asyncio
async def test_completion_event_observes_persisted_terminal_state_and_no_active_run(tmp_path: Path):
    store = tmp_path / "jobs.json"
    service = CronService(store)
    job = service.add_job(
        "event-order",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="ok")
    observed = {}

    async def completed(_name, data):
        reloaded = CronService(store)
        (disk_job,) = reloaded.list_jobs(include_disabled=True)
        observed.update(
            lifecycle=disk_job.lifecycle,
            last_run_id=disk_job.state.last_run_id,
            active=service.current_run(job.id),
            event=data,
        )

    service.on_complete = completed
    await _fire_scheduled(service, job)

    assert observed["lifecycle"] == "completed"
    assert observed["last_run_id"] == observed["event"]["runId"]
    assert observed["active"] is None
    assert observed["event"]["outputPersisted"] is True


@pytest.mark.asyncio
async def test_archive_write_failure_is_visible_and_suppresses_completed_event(tmp_path: Path, monkeypatch):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "archive-failure",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="ok")
    events = []
    service.on_complete = lambda name, data: asyncio.sleep(0, result=events.append((name, data)))

    def fail_archive(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_save_job_output", fail_archive)
    await _fire_scheduled(service, job)

    assert events == []
    assert "disk full" in (job.state.last_output_error or "")
    persisted = CronService(service.store_path).list_jobs(include_disabled=True)[0]
    assert "disk full" in (persisted.state.last_output_error or "")


@pytest.mark.asyncio
async def test_latest_archive_failure_is_not_hidden_by_older_output(tmp_path: Path, monkeypatch):
    service = CronService(tmp_path / "jobs.json")
    monkeypatch.setattr(feature_rpc, "_cron_provider", lambda: service)
    job = service.add_job(
        "latest-failed",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="first answer")
    assert await service.run_job(job.id) is True
    first_run_id = job.state.last_run_id

    def fail_archive(*_args, **_kwargs):
        raise OSError("latest output was not saved")

    monkeypatch.setattr(service, "_save_job_output", fail_archive)
    service.on_job = lambda _job: asyncio.sleep(0, result="second answer")
    assert await service.run_job(job.id) is True
    failed_run_id = job.state.last_run_id

    latest = feature_rpc.cron_output({"id": job.id})
    assert latest["status"] == "error"
    assert "latest output was not saved" in latest["error"]
    assert latest["outputs"][0]["runId"] == first_run_id

    older = feature_rpc.cron_output({"id": job.id, "runId": first_run_id})
    assert older["status"] == "available"
    assert older["error"] is None
    failed = feature_rpc.cron_output({"id": job.id, "runId": failed_run_id})
    assert failed["status"] == "error"


@pytest.mark.asyncio
async def test_run_metadata_recovers_terminal_state_after_job_store_failure(tmp_path: Path, monkeypatch):
    fixed_now = 1_800_000_000_000
    monkeypatch.setattr(cron_service, "_now_ms", lambda: fixed_now)
    store = tmp_path / "jobs.json"
    service = CronService(store)
    job = service.add_job(
        "recover",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="ok")

    def fail_store():
        raise OSError("jobs store unavailable")

    monkeypatch.setattr(service, "_save_store", fail_store)
    job.state.next_run_at_ms = cron_service._now_ms() - 1
    with pytest.raises(OSError, match="jobs store unavailable"):
        await service._run_due_jobs()

    recovered_service = CronService(store)
    (recovered,) = recovered_service.list_jobs(include_disabled=True)
    assert recovered.lifecycle == "completed"
    assert recovered.enabled is False
    assert recovered.state.next_run_at_ms is None
    assert recovered.state.last_run_id


def test_orphan_legacy_markdown_is_recovered_as_inert_history(tmp_path: Path):
    job_dir = tmp_path / "output" / "lost-job"
    job_dir.mkdir(parents=True)
    (job_dir / "2026-01-01_12-00-00.md").write_text(
        "# Cron Job: Lost report\n\n"
        "**Job ID:** lost-job\n"
        "**Run Time:** 2026-01-01T12:00:00\n\n"
        "## Response\n\nRecovered answer\n",
        encoding="utf-8",
    )

    service = CronService(tmp_path / "jobs.json")
    jobs = service.list_jobs(include_disabled=True, include_archived=True)

    assert len(jobs) == 1
    assert jobs[0].id == "lost-job"
    assert jobs[0].name == "Lost report"
    assert jobs[0].lifecycle == "archived"
    assert jobs[0].enabled is False
    assert jobs[0].state.next_run_at_ms is None


@pytest.mark.asyncio
async def test_retention_expiry_keeps_metadata_and_reports_expired(tmp_path: Path, monkeypatch):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "expiry",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="answer")
    await service.run_job(job.id)
    output = next((tmp_path / "output" / job.id).glob("*.md"))
    old = time.time() - (3 * 86400)
    os.utime(output, (old, old))
    monkeypatch.setattr(cron_service, "_OUTPUT_RETENTION_DAYS", 1)

    service._prune_archive()

    assert not output.exists()
    records = service._read_run_records(job.id, include_content=True)
    assert records[0]["expired"] is True
    assert records[0]["content"] is None

    monkeypatch.setattr(feature_rpc, "_cron_provider", lambda: service)
    result = feature_rpc.cron_output({"id": job.id, "runId": records[0]["runId"]})
    assert result["status"] == "expired"
    assert result["job"]["id"] == job.id
    assert result["outputs"][0]["runId"] == records[0]["runId"]


@pytest.mark.asyncio
async def test_retention_zero_disables_age_cleanup(tmp_path: Path, monkeypatch):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "forever",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="answer")
    await service.run_job(job.id)
    output = next((tmp_path / "output" / job.id).glob("*.md"))
    old = time.time() - (500 * 86400)
    os.utime(output, (old, old))
    monkeypatch.setattr(cron_service, "_OUTPUT_RETENTION_DAYS", 0)

    service._prune_archive()

    assert output.exists()


@pytest.mark.asyncio
async def test_archive_and_explicit_purge_have_separate_behavior(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "cleanup",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    service.on_job = lambda _job: asyncio.sleep(0, result="answer")
    await service.run_job(job.id)
    output_dir = tmp_path / "output" / job.id

    assert service.remove_job(job.id) is True
    assert output_dir.exists()
    archived = service.list_jobs(include_disabled=True, include_archived=True)[0]
    assert archived.lifecycle == "archived"
    assert service.list_jobs(include_disabled=True) == []

    assert service.remove_job(job.id, purge=True) is True
    assert not output_dir.exists()
    assert service.list_jobs(include_disabled=True, include_archived=True) == []


def test_schedule_change_is_transactional_and_normalizes_one_shot_fields(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "original",
        CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000),
        "old prompt",
    )
    job.lifecycle = "completed"
    job.enabled = False
    job.state.next_run_at_ms = None
    service._save_store()

    with pytest.raises(ValueError):
        service.update_job(job.id, {
            "name": "must not leak",
            "message": "must not leak",
            "schedule": CronSchedule(kind="at", at_ms=cron_service._now_ms() - 1),
        })
    assert job.name == "original"
    assert job.payload.message == "old prompt"
    assert job.lifecycle == "completed"

    future_every = CronSchedule(kind="every", every_ms=60_000)
    service.update_job(job.id, {"schedule": future_every})
    assert job.lifecycle == "scheduled"
    assert job.repeat_times is None
    assert job.repeat_completed == 0
    assert job.delete_after_run is False


def test_identical_schedule_and_title_edit_do_not_reactivate_completed_job(tmp_path: Path):
    service = CronService(tmp_path / "jobs.json")
    schedule = CronSchedule(kind="at", at_ms=cron_service._now_ms() + 60_000)
    job = service.add_job("done", schedule, "old")
    job.lifecycle = "completed"
    job.enabled = False
    job.state.next_run_at_ms = None

    service.update_job(job.id, {
        "name": "renamed",
        "message": "new",
        "schedule": CronSchedule(kind="at", at_ms=schedule.at_ms),
    })

    assert job.name == "renamed"
    assert job.lifecycle == "completed"
    assert job.enabled is False
    assert job.state.next_run_at_ms is None


def test_source_identity_survives_completion_and_archive(tmp_path: Path):
    store = tmp_path / "jobs.json"
    service = CronService(store)
    job = service.add_job(
        "owned",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
        source="firestore:user-42:server-a",
        source_id="task-123",
    )
    service.remove_job(job.id)

    recovered = CronService(store).list_jobs(
        include_disabled=True,
        include_archived=True,
    )[0]
    assert recovered.lifecycle == "archived"
    assert recovered.source == "firestore:user-42:server-a"
    assert recovered.source_id == "task-123"


def test_output_rpc_rejects_archive_path_traversal(tmp_path: Path, monkeypatch):
    service = CronService(tmp_path / "jobs.json")
    monkeypatch.setattr(feature_rpc, "_cron_provider", lambda: service)

    with pytest.raises(feature_rpc.FeatureRpcError) as error:
        feature_rpc.cron_output({"id": "../outside"})

    assert error.value.code == "IO_ERROR"


@pytest.mark.parametrize(
    "raw",
    [
        "{broken json",
        json.dumps({
            "version": 1,
            "jobs": [
                _legacy_job(job_id="valid", enabled=True, kind="every"),
                {"id": "invalid-entry-without-schedule"},
            ],
        }),
    ],
)
def test_unreadable_existing_store_fails_closed_without_overwrite(
    tmp_path: Path,
    raw: str,
) -> None:
    store = tmp_path / "jobs.json"
    store.write_text(raw, encoding="utf-8")
    original = store.read_bytes()
    # An orphan archive would previously trigger recovery and overwrite the
    # unreadable store with archive-only definitions.
    orphan = tmp_path / "output" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "legacy.md").write_text(
        "# Cron Job: Orphan\n\n**Run Time:** 2026-01-01T00:00:00\n",
        encoding="utf-8",
    )

    service = CronService(store)
    with pytest.raises(CronStoreLoadError):
        service.list_jobs(include_disabled=True, include_archived=True)

    assert store.read_bytes() == original
    assert service._store is None


@pytest.mark.asyncio
async def test_agent_tool_remove_archives_and_purge_is_explicit(tmp_path: Path):
    from flowly.agent.tools.cron import CronTool

    service = CronService(tmp_path / "jobs.json")
    job = service.add_job(
        "tool-history",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    tool = CronTool(service)

    archived = await tool.execute(action="remove", job_id=job.id)
    assert "Archived job" in archived
    listed = await tool.execute(action="list", include_archived=True)
    assert "[archived]" in listed
    assert job.id in listed

    purged = await tool.execute(action="purge", job_id=job.id)
    assert "Permanently purged" in purged
    assert service.list_jobs(include_disabled=True, include_archived=True) == []


def test_cli_remove_archives_by_default_and_purge_is_separate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from typer.testing import CliRunner

    from flowly.cli.cron_cmd import cron_app
    from flowly.config import loader

    monkeypatch.setattr(loader, "get_data_dir", lambda: tmp_path)
    service = CronService(tmp_path / "cron" / "jobs.json")
    job = service.add_job(
        "cli-history",
        CronSchedule(kind="every", every_ms=60_000),
        "hello",
    )
    runner = CliRunner()

    archived = runner.invoke(cron_app, ["remove", job.id])
    assert archived.exit_code == 0
    assert "Archived job" in archived.stdout
    persisted = CronService(service.store_path).list_jobs(
        include_disabled=True,
        include_archived=True,
    )[0]
    assert persisted.lifecycle == "archived"

    purged = runner.invoke(cron_app, ["remove", job.id, "--purge"])
    assert purged.exit_code == 0
    assert "Permanently purged job" in purged.stdout
    assert CronService(service.store_path).list_jobs(
        include_disabled=True,
        include_archived=True,
    ) == []


def test_rpc_filters_history_and_round_trips_source_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = CronService(tmp_path / "jobs.json")
    monkeypatch.setattr(feature_rpc, "_cron_provider", lambda: service)
    created = feature_rpc.cron_add({
        "name": "remote-owned",
        "message": "hello",
        "schedule": {"kind": "every", "everyMs": 60_000},
        "deliver": False,
        "source": "firestore:user-42:server-a",
        "sourceId": "task-9",
    })["job"]
    assert created["source"] == "firestore:user-42:server-a"
    assert created["sourceId"] == "task-9"

    removed = feature_rpc.cron_remove({"id": created["id"]})
    assert removed == {"ok": True, "archived": True, "purged": False}
    assert feature_rpc.cron_list({"includeDisabled": True})["jobs"] == []
    history = feature_rpc.cron_list({
        "includeDisabled": True,
        "includeArchived": True,
    })
    assert history["retentionDays"] >= 0
    assert history["jobs"][0]["lifecycle"] == "archived"
    assert history["jobs"][0]["sourceId"] == "task-9"
