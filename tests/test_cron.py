"""Tests for cron types, scheduling, and service."""

import json
import time
from pathlib import Path

import pytest

from flowly.cron.service import CronService, _compute_next_run
from flowly.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore


# ── _compute_next_run ───────────────────────────────────────────────


class TestComputeNextRun:
    def test_at_future(self):
        now = int(time.time() * 1000)
        future = now + 60_000  # 1 minute ahead
        schedule = CronSchedule(kind="at", at_ms=future)
        assert _compute_next_run(schedule, now) == future

    def test_at_past_returns_none(self):
        now = int(time.time() * 1000)
        past = now - 60_000
        schedule = CronSchedule(kind="at", at_ms=past)
        assert _compute_next_run(schedule, now) is None

    def test_every_interval(self):
        now = int(time.time() * 1000)
        schedule = CronSchedule(kind="every", every_ms=30_000)
        result = _compute_next_run(schedule, now)
        assert result == now + 30_000

    def test_every_zero_interval(self):
        now = int(time.time() * 1000)
        schedule = CronSchedule(kind="every", every_ms=0)
        assert _compute_next_run(schedule, now) is None

    def test_every_negative_interval(self):
        now = int(time.time() * 1000)
        schedule = CronSchedule(kind="every", every_ms=-1000)
        assert _compute_next_run(schedule, now) is None

    def test_cron_expression(self):
        now = int(time.time() * 1000)
        schedule = CronSchedule(kind="cron", expr="* * * * *")  # every minute
        result = _compute_next_run(schedule, now)
        assert result is not None
        assert result > now

    def test_cron_invalid_expression(self):
        now = int(time.time() * 1000)
        schedule = CronSchedule(kind="cron", expr="invalid cron")
        assert _compute_next_run(schedule, now) is None

    def test_unknown_kind(self):
        now = int(time.time() * 1000)
        schedule = CronSchedule(kind="every")  # no every_ms set
        assert _compute_next_run(schedule, now) is None


# ── CronService ─────────────────────────────────────────────────────


class TestCronService:
    def test_add_job(self, tmp_path: Path):
        store_file = tmp_path / "cron.json"
        svc = CronService(store_path=store_file)
        job = svc.add_job(
            name="test-job",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            message="do something",
        )
        assert job.name == "test-job"
        assert job.enabled is True
        assert job.payload.message == "do something"
        assert job.state.next_run_at_ms is not None
        assert store_file.exists()

    def test_list_jobs(self, tmp_path: Path):
        svc = CronService(store_path=tmp_path / "cron.json")
        # CronService enforces MIN_INTERVAL_MS=60_000 to prevent cron
        # bomb / runaway LLM cost. Use distinct ≥60s values so sort
        # order is still asserted.
        svc.add_job("a", CronSchedule(kind="every", every_ms=60_000), "msg a")
        svc.add_job("b", CronSchedule(kind="every", every_ms=120_000), "msg b")

        jobs = svc.list_jobs()
        assert len(jobs) == 2
        # Should be sorted by next_run_at_ms
        assert jobs[0].state.next_run_at_ms <= jobs[1].state.next_run_at_ms

    def test_remove_job(self, tmp_path: Path):
        svc = CronService(store_path=tmp_path / "cron.json")
        job = svc.add_job("x", CronSchedule(kind="every", every_ms=60_000), "msg")
        assert svc.remove_job(job.id) is True
        assert svc.list_jobs() == []

    def test_remove_nonexistent(self, tmp_path: Path):
        svc = CronService(store_path=tmp_path / "cron.json")
        assert svc.remove_job("no-such-id") is False

    def test_enable_disable(self, tmp_path: Path):
        svc = CronService(store_path=tmp_path / "cron.json")
        job = svc.add_job("y", CronSchedule(kind="every", every_ms=60_000), "msg")

        disabled = svc.enable_job(job.id, enabled=False)
        assert disabled is not None
        assert disabled.enabled is False
        assert disabled.state.next_run_at_ms is None

        enabled = svc.enable_job(job.id, enabled=True)
        assert enabled is not None
        assert enabled.enabled is True
        assert enabled.state.next_run_at_ms is not None

    def test_enable_nonexistent(self, tmp_path: Path):
        svc = CronService(store_path=tmp_path / "cron.json")
        assert svc.enable_job("nope") is None

    def test_status(self, tmp_path: Path):
        svc = CronService(store_path=tmp_path / "cron.json")
        svc.add_job("z", CronSchedule(kind="every", every_ms=60_000), "msg")

        status = svc.status()
        assert status["jobs"] == 1
        assert status["enabled"] is False  # not started
        assert status["next_wake_at_ms"] is not None

    def test_persistence(self, tmp_path: Path):
        """Jobs survive service restart."""
        store_file = tmp_path / "cron.json"
        svc1 = CronService(store_path=store_file)
        svc1.add_job("persist", CronSchedule(kind="every", every_ms=60_000), "msg")

        # New service instance loading from same file
        svc2 = CronService(store_path=store_file)
        jobs = svc2.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].name == "persist"

    def test_persistence_format(self, tmp_path: Path):
        """Saved JSON uses camelCase keys."""
        store_file = tmp_path / "cron.json"
        svc = CronService(store_path=store_file)
        svc.add_job("fmt", CronSchedule(kind="every", every_ms=60_000), "msg")

        data = json.loads(store_file.read_text())
        job_data = data["jobs"][0]
        assert "everyMs" in job_data["schedule"]
        assert "createdAtMs" in job_data
        assert "deleteAfterRun" in job_data

    def test_add_tool_call_job(self, tmp_path: Path):
        svc = CronService(store_path=tmp_path / "cron.json")
        job = svc.add_job(
            name="call-job",
            schedule=CronSchedule(kind="every", every_ms=60_000),
            message="Run voice call",
            payload_kind="tool_call",
            tool_name="voice_call",
            tool_args={"action": "call", "to": "+1234567890"},
        )
        assert job.payload.kind == "tool_call"
        assert job.payload.tool_name == "voice_call"
        assert job.payload.tool_args["to"] == "+1234567890"

    def test_list_excludes_disabled_by_default(self, tmp_path: Path):
        svc = CronService(store_path=tmp_path / "cron.json")
        job = svc.add_job("d", CronSchedule(kind="every", every_ms=60_000), "msg")
        svc.enable_job(job.id, enabled=False)

        assert len(svc.list_jobs(include_disabled=False)) == 0
        assert len(svc.list_jobs(include_disabled=True)) == 1


# ── CronSchedule parsing (from cron tool) ──────────────────────────


class TestCronToolParsing:
    """Test the schedule parsing helper in the CronTool."""

    def test_parse_duration_seconds(self):
        from flowly.agent.tools.cron import _parse_duration
        assert _parse_duration("30s") == 30_000

    def test_parse_duration_minutes(self):
        from flowly.agent.tools.cron import _parse_duration
        assert _parse_duration("5m") == 300_000

    def test_parse_duration_hours(self):
        from flowly.agent.tools.cron import _parse_duration
        assert _parse_duration("2h") == 7_200_000

    def test_parse_duration_days(self):
        from flowly.agent.tools.cron import _parse_duration
        assert _parse_duration("1d") == 86_400_000

    def test_parse_duration_weeks(self):
        from flowly.agent.tools.cron import _parse_duration
        assert _parse_duration("1w") == 604_800_000

    def test_parse_duration_bare_number(self):
        from flowly.agent.tools.cron import _parse_duration
        assert _parse_duration("60") == 60_000  # treated as seconds

    def test_parse_duration_invalid(self):
        from flowly.agent.tools.cron import _parse_duration
        assert _parse_duration("abc") is None
        assert _parse_duration("") is None

    def test_format_next_run_none(self):
        from flowly.agent.tools.cron import _format_next_run
        assert _format_next_run(None) == "not scheduled"

    def test_format_next_run_past(self):
        from flowly.agent.tools.cron import _format_next_run
        past_ms = int(time.time() * 1000) - 60_000
        assert _format_next_run(past_ms) == "overdue"

    def test_format_next_run_seconds(self):
        from flowly.agent.tools.cron import _format_next_run
        future_ms = int(time.time() * 1000) + 30_000
        result = _format_next_run(future_ms)
        assert result.startswith("in ") and result.endswith("s")

    def test_format_next_run_minutes(self):
        from flowly.agent.tools.cron import _format_next_run
        future_ms = int(time.time() * 1000) + 300_000  # 5 min
        result = _format_next_run(future_ms)
        assert result.startswith("in ") and result.endswith("m")
