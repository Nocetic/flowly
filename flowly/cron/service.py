"""Cron service for scheduling agent tasks."""

import asyncio
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Coroutine, Literal

from loguru import logger

from flowly.cron.timezone import schedule_timezone
from flowly.cron.types import (
    CronJob,
    CronJobState,
    CronOrigin,
    CronPayload,
    CronSchedule,
    CronStore,
)

# Cross-platform file locking. fcntl is Unix-only; on Windows use msvcrt.
# Used to serialize cron ticks across overlapping processes (gateway
# in-process timer + manual `flowly cron run` + systemd daemon) so only
# one tick fires due jobs at a time.
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None
    try:
        import msvcrt as _msvcrt
    except ImportError:
        _msvcrt = None
else:
    _msvcrt = None


# Inactivity timeout for a cron job. A job is killed ONLY if the agent
# shows no sign of progress (no stream chunk, no tool call start/end,
# no API request) for this many seconds — not on wall-clock elapsed.
# This lets legitimate long-running research (15 web_fetch calls,
# multi-step analysis) finish uninterrupted while still catching
# genuinely hung jobs (stuck HTTP, infinite loop). 0 = unlimited.
# Overridable via the ``FLOWLY_CRON_TIMEOUT`` env var.
_JOB_TIMEOUT_S = int(os.getenv("FLOWLY_CRON_TIMEOUT", "600"))
# How often the inactivity poller checks `get_activity_summary()`.
# 5s — small enough to react promptly, large enough that the poll
# overhead is negligible.
_INACTIVITY_POLL_S = 5.0

# How long per-run archive .md files under `output/{job_id}/` are kept.
# Retention bounds the on-disk archive — sustained cron use (288
# fires/day for a 5min job) would otherwise turn into GB on disk
# over a year. 0 disables pruning.
_OUTPUT_RETENTION_DAYS = int(os.getenv("FLOWLY_CRON_RETENTION_DAYS", "30"))

# Sentinel the agent can return from a cron run to signal "nothing new to
# report" — callbacks should check this before publishing the response and
# skip delivery if matched. Output archive still records the [SILENT] run.
# Convention: response body containing this sentinel is not delivered.
SILENT_MARKER = "[SILENT]"

_LIFECYCLES = {"scheduled", "paused", "completed", "archived"}


class CronStoreLoadError(RuntimeError):
    """The durable cron store exists but cannot be loaded safely."""


def is_silent_response(response: str | None) -> bool:
    """Return True if a cron callback response is the [SILENT] sentinel."""
    if not isinstance(response, str):
        return False
    return SILENT_MARKER in response.strip().upper()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _schedule_equal(left: CronSchedule, right: CronSchedule) -> bool:
    return (
        left.kind == right.kind
        and left.at_ms == right.at_ms
        and left.every_ms == right.every_ms
        and left.expr == right.expr
        and left.tz == right.tz
    )


def _legacy_lifecycle(raw: dict[str, Any]) -> str:
    """Infer a safe lifecycle for a pre-v2 jobs.json entry.

    A past one-shot with no recorded run remains scheduled so restart can run
    it. A one-shot that did run, or an exhausted repeat limit, is completed.
    Disabled records remain paused rather than being silently re-enabled.
    """
    explicit = raw.get("lifecycle")
    if explicit in _LIFECYCLES:
        return str(explicit)

    state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
    repeat_times = raw.get("repeatTimes")
    repeat_completed = int(raw.get("repeatCompleted", 0) or 0)
    schedule = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    has_run = state.get("lastRunAtMs") is not None

    if repeat_times is not None and repeat_completed >= int(repeat_times):
        return "completed"
    if schedule.get("kind") == "at" and has_run:
        return "completed"
    if not bool(raw.get("enabled", True)):
        return "paused"
    return "scheduled"


# Grace window bounds for stale-job fast-forward. If the gateway was down
# for longer than the grace window, recurring jobs past-due by more than
# the grace are fast-forwarded instead of firing a cascade of missed
# runs on restart. Scales with schedule period (half of it), clamped to
# [MIN, MAX] so frequent jobs recover quickly and daily jobs tolerate a
# couple of hours of downtime.
_GRACE_MIN_MS = 120 * 1000       # 2 minutes
_GRACE_MAX_MS = 7200 * 1000      # 2 hours


def _format_schedule(schedule: CronSchedule) -> str:
    """Human-readable schedule string for archive headers and logs."""
    if schedule.kind == "every" and schedule.every_ms:
        secs = schedule.every_ms // 1000
        if secs and secs % 3600 == 0:
            return f"every {secs // 3600}h"
        if secs and secs % 60 == 0:
            return f"every {secs // 60}m"
        return f"every {secs}s"
    if schedule.kind == "cron" and schedule.expr:
        return f"cron {schedule.expr}"
    if schedule.kind == "at" and schedule.at_ms:
        import datetime as _dt
        return f"at {_dt.datetime.fromtimestamp(schedule.at_ms / 1000).isoformat()}"
    return schedule.kind


def _compute_grace_ms(schedule: CronSchedule) -> int:
    """How late a recurring job can fire and still catch up instead of fast-forwarding."""
    if schedule.kind == "every" and schedule.every_ms:
        grace = schedule.every_ms // 2
        return max(_GRACE_MIN_MS, min(grace, _GRACE_MAX_MS))

    if schedule.kind == "cron" and schedule.expr:
        try:
            tzinfo = schedule_timezone(schedule.tz)
            now_ms = _now_ms()
            first = _next_cron_datetime(schedule.expr, now_ms, tzinfo)
            second = _next_cron_datetime(
                schedule.expr, int(first.timestamp() * 1000), tzinfo
            )
            period_ms = int((second.timestamp() - first.timestamp()) * 1000)
            grace = period_ms // 2
            return max(_GRACE_MIN_MS, min(grace, _GRACE_MAX_MS))
        except Exception:
            pass

    return _GRACE_MIN_MS


def _next_cron_datetime(
    expression: str, after_ms: int, tzinfo: _dt.tzinfo
) -> _dt.datetime:
    """Resolve the next cron wall time deterministically across DST changes.

    ``croniter``'s timezone-aware arithmetic can shift an ordinary wall-clock
    schedule by an hour on the day after a transition. Iterate naive local
    calendar values instead, then attach the requested zone and verify that
    the local value exists. A spring-forward gap is skipped. During a repeated
    fall-back hour, fold 0 is selected so the job runs once at the earlier
    occurrence rather than twice.
    """
    from croniter import croniter

    after = _dt.datetime.fromtimestamp(after_ms / 1000, tz=tzinfo)
    cron = croniter(expression, after.replace(tzinfo=None))
    # This covers even a per-second expression across the largest historical
    # civil-time gap (24 hours), while still bounding damaged tzinfo behavior.
    for _ in range(100_000):
        wall = cron.get_next(_dt.datetime)
        candidate = wall.replace(tzinfo=tzinfo, fold=0)
        timestamp = candidate.timestamp()
        normalized = _dt.datetime.fromtimestamp(timestamp, tz=tzinfo)
        if normalized.replace(tzinfo=None) != wall:
            # This local calendar value does not exist (spring-forward gap).
            continue
        if int(timestamp * 1000) > after_ms:
            return normalized
    raise ValueError("Unable to resolve next cron occurrence in timezone")


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > now_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        # Next interval from now
        return now_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            tzinfo = schedule_timezone(schedule.tz)
            next_dt = _next_cron_datetime(schedule.expr, now_ms, tzinfo)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None

    return None


class CronService:
    """Service for managing and executing scheduled jobs."""

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        on_alert: Callable[[CronJob, str], Coroutine[Any, Any, None]] | None = None,
        on_complete: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]] | None = None,
        on_run_start: Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]] | None = None,
        activity_probe: Callable[[], dict[str, Any]] | None = None,
        interrupt_fn: Callable[[str], None] | None = None,
    ):
        self.store_path = store_path
        self.on_job = on_job  # Callback to execute job, returns response text
        # Called when consecutive_failures hits the configured threshold.
        # Gateway wires this to an OutboundMessage so the user learns that
        # a scheduled task is broken. Optional — no callback = alerts are
        # logged but not delivered.
        self.on_alert = on_alert
        # Called once a job reaches a TERMINAL outcome (success, or failure
        # with no retries left) — NOT on a transient failure that will retry.
        # Signature: on_complete(event_name: str, data: dict). The gateway
        # wires this to a WS broadcast (``cron.completed``) so desktop clients
        # can raise a native OS notification. Optional — no callback = silent.
        self.on_complete = on_complete
        # Called the moment a job STARTS executing, with the same event shape
        # ``on_complete`` uses (``cron.started``). Together the two bracket a
        # run, so a Schedule UI can show "running now" the instant it begins
        # instead of inferring it from the next poll. Optional — no callback =
        # the run is still tracked in ``_active_runs`` for pollers.
        self.on_run_start = on_run_start
        # Inactivity-based timeout wiring. `activity_probe` returns the
        # agent's get_activity_summary() dict (at minimum with
        # `seconds_since_activity`). `interrupt_fn` signals cooperative
        # shutdown to the agent when the inactivity limit is exceeded.
        # Both optional — without them the timeout reverts to wall-clock.
        self.activity_probe = activity_probe
        self.interrupt_fn = interrupt_fn
        self._store: CronStore | None = None
        # The wake task owns only the cancellable sleep until the next due
        # time.  It must never await an executing job: ordinary mutations
        # re-arm this task, and cancelling it used to cancel that job too.
        self._timer_task: asyncio.Task | None = None
        # One scheduler pass may execute several jobs that became due at the
        # same instant.  Keep it separate from the wake task so re-arming the
        # clock cannot disturb work already in progress.
        self._scheduler_task: asyncio.Task | None = None
        # Includes scheduled and manual executions.  Strong references make
        # shutdown draining deterministic even when the caller launched a
        # detached manual run.
        self._execution_tasks: set[asyncio.Task] = set()
        # A reservation is visible in ``_active_runs`` before a detached
        # child task gets its first event-loop turn. Tracking the owner
        # separately lets shutdown distinguish that unstarted reservation
        # from work it must drain or cancel.
        self._execution_owners: dict[str, asyncio.Task] = {}
        self._running = False
        self._stopping = False
        self._executing = False  # Prevent concurrent _on_timer() calls
        # job_id → in-flight run record. Populated for the whole duration of
        # ``_execute_job`` so live views (cron.list, health_report) can tell
        # which job is executing right now, and the run's ``sessionKey`` /
        # ``runId`` let them follow its output while it happens. Keyed by job
        # rather than a single slot because a manual trigger can overlap a
        # scheduled fire of a DIFFERENT job.
        self._active_runs: dict[str, dict[str, Any]] = {}

    def _load_store(self) -> CronStore:
        """Load jobs from disk."""
        if self._store:
            return self._store

        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("jobs", []), list):
                    raise ValueError("cron store must contain a jobs array")
                jobs = []
                for j in data.get("jobs", []):
                    lifecycle = _legacy_lifecycle(j)
                    origin_raw = j.get("origin")
                    origin_obj = None
                    if isinstance(origin_raw, dict):
                        origin_obj = CronOrigin(
                            platform=origin_raw.get("platform"),
                            chat_id=origin_raw.get("chatId") or origin_raw.get("chat_id"),
                            chat_name=origin_raw.get("chatName") or origin_raw.get("chat_name"),
                            thread_id=origin_raw.get("threadId") or origin_raw.get("thread_id"),
                        )
                    state_raw = j.get("state", {})
                    schedule_raw = j["schedule"]
                    next_run_at_ms = state_raw.get("nextRunAtMs")
                    # Pre-v2 restart recovery: do not strand an overdue
                    # one-shot merely because `_compute_next_run` rejects past
                    # timestamps. Its original timestamp remains a due time.
                    if (
                        lifecycle == "scheduled"
                        and schedule_raw.get("kind") == "at"
                        and state_raw.get("lastRunAtMs") is None
                        and next_run_at_ms is None
                    ):
                        next_run_at_ms = schedule_raw.get("atMs")

                    jobs.append(CronJob(
                        id=j["id"],
                        name=j["name"],
                        enabled=(lifecycle == "scheduled"),
                        schedule=CronSchedule(
                            kind=schedule_raw["kind"],
                            at_ms=schedule_raw.get("atMs"),
                            every_ms=schedule_raw.get("everyMs"),
                            expr=schedule_raw.get("expr"),
                            tz=schedule_raw.get("tz"),
                        ),
                        payload=CronPayload(
                            kind=j["payload"].get("kind", "agent_turn"),
                            message=j["payload"].get("message", ""),
                            deliver=j["payload"].get("deliver", False),
                            channel=j["payload"].get("channel"),
                            to=j["payload"].get("to"),
                            tool_name=j["payload"].get("toolName"),
                            tool_args=j["payload"].get("toolArgs"),
                        ),
                        state=CronJobState(
                            next_run_at_ms=next_run_at_ms,
                            last_run_at_ms=state_raw.get("lastRunAtMs"),
                            last_status=state_raw.get("lastStatus"),
                            last_error=state_raw.get("lastError"),
                            last_delivery_error=state_raw.get("lastDeliveryError"),
                            last_output_error=state_raw.get("lastOutputError"),
                            consecutive_failures=state_raw.get("consecutiveFailures", 0),
                            last_alert_at_ms=state_raw.get("lastAlertAtMs"),
                            retry_attempt=state_raw.get("retryAttempt", 0),
                            last_run_id=state_raw.get("lastRunId"),
                        ),
                        created_at_ms=j.get("createdAtMs", 0),
                        updated_at_ms=j.get("updatedAtMs", 0),
                        lifecycle=lifecycle,
                        completed_at_ms=j.get("completedAtMs"),
                        archived_at_ms=j.get("archivedAtMs"),
                        source=j.get("source"),
                        source_id=j.get("sourceId"),
                        delete_after_run=j.get("deleteAfterRun", False),
                        origin=origin_obj,
                        repeat_times=j.get("repeatTimes"),
                        repeat_completed=j.get("repeatCompleted", 0),
                        script=j.get("script"),
                        skills=list(j.get("skills") or []),
                        model=j.get("model"),
                        provider=j.get("provider"),
                        retry_max_attempts=j.get("retryMaxAttempts", 0),
                        retry_backoff_ms=list(j.get("retryBackoffMs") or []),
                        failure_alert_after=j.get("failureAlertAfter", 3),
                        failure_alert_cooldown_ms=j.get(
                            "failureAlertCooldownMs", 24 * 60 * 60 * 1000
                        ),
                    ))
                self._store = CronStore(version=2, jobs=jobs)
            except Exception as e:
                # Fail closed. Treating an existing unreadable store as empty
                # lets a later add/recovery save silently erase user schedules.
                self._store = None
                logger.error(f"Failed to load cron store safely: {e}")
                raise CronStoreLoadError(
                    f"Cron store is unreadable; refusing to overwrite {self.store_path}: {e}"
                ) from e
        else:
            self._store = CronStore()

        # Older versions deleted the job definition but sometimes left its
        # output directory. Recover those directories as inert archived jobs
        # so History can discover them without already knowing the UUID.
        if self._recover_orphan_archives():
            self._save_store()
        return self._store

    def _save_store(self) -> None:
        """Save jobs to disk atomically."""
        if not self._store:
            return

        self.store_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": self._store.version,
            "jobs": [
                {
                    "id": j.id,
                    "name": j.name,
                    "enabled": j.enabled,
                    "schedule": {
                        "kind": j.schedule.kind,
                        "atMs": j.schedule.at_ms,
                        "everyMs": j.schedule.every_ms,
                        "expr": j.schedule.expr,
                        "tz": j.schedule.tz,
                    },
                    "payload": {
                        "kind": j.payload.kind,
                        "message": j.payload.message,
                        "deliver": j.payload.deliver,
                        "channel": j.payload.channel,
                        "to": j.payload.to,
                        "toolName": j.payload.tool_name,
                        "toolArgs": j.payload.tool_args,
                    },
                    "state": {
                        "nextRunAtMs": j.state.next_run_at_ms,
                        "lastRunAtMs": j.state.last_run_at_ms,
                        "lastStatus": j.state.last_status,
                        "lastError": j.state.last_error,
                        "lastDeliveryError": j.state.last_delivery_error,
                        "lastOutputError": j.state.last_output_error,
                        "consecutiveFailures": j.state.consecutive_failures,
                        "lastAlertAtMs": j.state.last_alert_at_ms,
                        "retryAttempt": j.state.retry_attempt,
                        "lastRunId": j.state.last_run_id,
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
                    "lifecycle": j.lifecycle,
                    "completedAtMs": j.completed_at_ms,
                    "archivedAtMs": j.archived_at_ms,
                    "source": j.source,
                    "sourceId": j.source_id,
                    "deleteAfterRun": j.delete_after_run,
                    "origin": (
                        {
                            "platform": j.origin.platform,
                            "chatId": j.origin.chat_id,
                            "chatName": j.origin.chat_name,
                            "threadId": j.origin.thread_id,
                        }
                        if j.origin
                        else None
                    ),
                    "repeatTimes": j.repeat_times,
                    "repeatCompleted": j.repeat_completed,
                    "script": j.script,
                    "skills": list(j.skills) if j.skills else [],
                    "model": j.model,
                    "provider": j.provider,
                    "retryMaxAttempts": j.retry_max_attempts,
                    "retryBackoffMs": list(j.retry_backoff_ms) if j.retry_backoff_ms else [],
                    "failureAlertAfter": j.failure_alert_after,
                    "failureAlertCooldownMs": j.failure_alert_cooldown_ms,
                }
                for j in self._store.jobs
            ]
        }

        tmp_path = self.store_path.with_suffix(f".tmp.{secrets.token_hex(4)}")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(str(tmp_path), str(self.store_path))
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def start(self) -> None:
        """Start the cron service."""
        self._stopping = False
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        # One-shot housekeeping on gateway boot. Transcript bodies age out,
        # while their small metadata records remain so History can say
        # "expired" instead of confusing expiry with no result or I/O error.
        try:
            self._prune_archive()
        except Exception as e:
            logger.warning(f"Cron: archive pruning skipped: {e}")
        logger.info(f"Cron service started with {len(self._store.jobs if self._store else [])} jobs")

    def _output_root(self) -> Path:
        """Return the `output/` root (siblings the jobs.json file)."""
        return self.store_path.parent / "output"

    def _job_output_dir(self, job_id: str) -> Path:
        """Resolve one archive directory without permitting path traversal."""
        if not job_id or Path(job_id).name != job_id or job_id in {".", ".."}:
            raise ValueError("invalid cron job id")
        root = self._output_root().resolve()
        candidate = root / job_id
        resolved = candidate.resolve(strict=False)
        if resolved.parent != root:
            raise ValueError("cron output path escapes archive root")
        return candidate

    @staticmethod
    def _run_output_path(job_dir: Path, output_name: str) -> Path:
        """Resolve a metadata-referenced body inside its job directory."""
        if (
            not output_name
            or Path(output_name).name != output_name
            or output_name in {".", ".."}
        ):
            raise OSError("invalid cron output filename")
        root = job_dir.resolve()
        candidate = job_dir / output_name
        if candidate.resolve(strict=False).parent != root:
            raise OSError("cron output file escapes job archive")
        return candidate

    def _write_run_metadata(self, job_dir: Path, record: dict[str, Any]) -> Path:
        """Atomically persist the small durable index for one run."""
        run_id = str(record["runId"])
        path = job_dir / f"{run_id}.json"
        tmp = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
        try:
            tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(path))
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return path

    def _legacy_run_record(self, job_id: str, path: Path) -> dict[str, Any]:
        """Build stable metadata for an archive created before run IDs."""
        content = path.read_text(encoding="utf-8")
        first_line = content.splitlines()[0] if content else ""
        match = re.match(r"^# Cron Job: (.*?)(?: \(FAILED\))?$", first_line)
        name = match.group(1) if match else job_id
        time_match = re.search(r"^\*\*Run Time:\*\* (.+)$", content, re.MULTILINE)
        started_ms: int
        if time_match:
            try:
                started_ms = int(_dt.datetime.fromisoformat(time_match.group(1)).timestamp() * 1000)
            except (TypeError, ValueError):
                started_ms = int(path.stat().st_mtime * 1000)
        else:
            started_ms = int(path.stat().st_mtime * 1000)
        digest = hashlib.sha256(f"{job_id}/{path.name}".encode()).hexdigest()[:20]
        return {
            "version": 1,
            "runId": f"legacy-{digest}",
            "jobId": job_id,
            "jobName": name,
            "startedAtMs": started_ms,
            "completedAtMs": started_ms,
            "status": "error" if " (FAILED)" in first_line else "ok",
            "error": None,
            "deliveryError": None,
            "outputFile": path.name,
            "expired": False,
            "legacy": True,
        }

    def _read_run_records(
        self,
        job_id: str,
        *,
        include_content: bool = False,
    ) -> list[dict[str, Any]]:
        """Read new metadata plus any unindexed legacy Markdown outputs."""
        try:
            job_dir = self._job_output_dir(job_id)
        except ValueError as exc:
            raise OSError(str(exc)) from exc
        if not job_dir.is_dir():
            return []

        records: list[dict[str, Any]] = []
        referenced: set[str] = set()
        for meta_path in job_dir.glob("*.json"):
            try:
                raw = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise OSError(f"Unreadable cron run metadata {meta_path.name}: {exc}") from exc
            if not isinstance(raw, dict) or not raw.get("runId"):
                continue
            record = dict(raw)
            output_name = record.get("outputFile")
            if isinstance(output_name, str):
                if Path(output_name).name != output_name:
                    raise OSError("invalid outputFile in cron run metadata")
                record["outputFile"] = output_name
                referenced.add(output_name)
            records.append(record)

        for output_path in job_dir.glob("*.md"):
            if output_path.name not in referenced:
                records.append(self._legacy_run_record(job_id, output_path))

        for record in records:
            output_name = record.get("outputFile")
            output_path = (
                self._run_output_path(job_dir, str(output_name))
                if output_name else None
            )
            available = output_path is not None and output_path.is_file()
            record["expired"] = bool(record.get("expired")) or not available
            if include_content:
                record["content"] = output_path.read_text(encoding="utf-8") if available else None
            record.setdefault("jobId", job_id)
            record.setdefault("jobName", job_id)
            record.setdefault("status", "ok")
            record.setdefault("startedAtMs", 0)
            record.setdefault("completedAtMs", record.get("startedAtMs", 0))
            record.setdefault("deliveryError", None)

        return sorted(
            records,
            key=lambda record: (
                int(record.get("completedAtMs") or record.get("startedAtMs") or 0),
                str(record.get("runId") or ""),
            ),
            reverse=True,
        )

    def _recover_orphan_archives(self) -> bool:
        """Recover output-only jobs as inert archived History records."""
        if self._store is None:
            return False
        output_root = self._output_root()
        if not output_root.is_dir():
            return False
        known = {job.id: job for job in self._store.jobs}
        changed = False
        for job_dir in output_root.iterdir():
            if not job_dir.is_dir():
                continue
            try:
                records = self._read_run_records(job_dir.name)
            except OSError as exc:
                logger.warning(f"Cron: cannot recover archive {job_dir.name}: {exc}")
                continue
            if not records:
                continue
            latest = records[0]
            existing = known.get(job_dir.name)
            latest_started = int(latest.get("startedAtMs") or 0)
            latest_completed = int(
                latest.get("completedAtMs") or latest_started
            )
            if existing is not None:
                # A crash may land run metadata before the final jobs.json
                # save. Replay the newer durable snapshot idempotently.
                if existing.state.last_run_id == str(latest.get("runId")):
                    continue
                if latest_completed < int(existing.updated_at_ms or 0):
                    continue
                existing.state.last_run_at_ms = latest_started or None
                existing.state.last_run_id = str(latest.get("runId"))
                status = latest.get("status")
                if status in {"ok", "error", "skipped"}:
                    existing.state.last_status = status
                existing.state.last_error = latest.get("error")
                existing.state.last_delivery_error = latest.get("deliveryError")
                existing.state.retry_attempt = int(latest.get("retryAttempt") or 0)
                existing.state.next_run_at_ms = latest.get("nextRunAtMs")
                existing.repeat_completed = max(
                    existing.repeat_completed,
                    int(latest.get("repeatCompleted") or 0),
                )
                recovered_lifecycle = latest.get("lifecycleAfterRun")
                if recovered_lifecycle in _LIFECYCLES:
                    existing.lifecycle = recovered_lifecycle
                    existing.enabled = recovered_lifecycle == "scheduled"
                    if recovered_lifecycle == "completed":
                        existing.completed_at_ms = int(
                            latest.get("completedAtMs") or latest_started
                        ) or None
                existing.updated_at_ms = latest_completed
                changed = True
                logger.info(
                    f"Cron: reconciled job {existing.id} from durable run metadata"
                )
                continue
            started_values = [int(r.get("startedAtMs") or 0) for r in records]
            completed = int(latest.get("completedAtMs") or latest.get("startedAtMs") or 0)
            recovered = CronJob(
                id=job_dir.name,
                name=str(latest.get("jobName") or job_dir.name),
                enabled=False,
                schedule=CronSchedule(kind="at"),
                payload=CronPayload(),
                state=CronJobState(
                    next_run_at_ms=None,
                    last_run_at_ms=int(latest.get("startedAtMs") or 0) or None,
                    last_status=latest.get("status") if latest.get("status") in {"ok", "error", "skipped"} else None,
                    last_error=latest.get("error"),
                    last_delivery_error=latest.get("deliveryError"),
                    last_run_id=str(latest.get("runId")),
                ),
                created_at_ms=min((v for v in started_values if v), default=completed),
                updated_at_ms=completed,
                lifecycle="archived",
                completed_at_ms=completed or None,
                archived_at_ms=completed or _now_ms(),
            )
            self._store.jobs.append(recovered)
            known[recovered.id] = recovered
            changed = True
            logger.info(f"Cron: recovered archived history for missing job {recovered.id}")
        return changed

    def _prune_archive(self) -> None:
        """Expire old transcript bodies while preserving run metadata."""
        output_root = self._output_root()
        if not output_root.exists():
            return

        now = time.time()
        retention_s = max(0, _OUTPUT_RETENTION_DAYS) * 86400
        pruned_files = 0
        if retention_s <= 0:
            return
        for job_dir in output_root.iterdir():
            if not job_dir.is_dir():
                continue
            for f in job_dir.iterdir():
                if not f.is_file() or f.suffix != ".md":
                    continue
                try:
                    age = now - f.stat().st_mtime
                except OSError:
                    continue
                if age > retention_s:
                    try:
                        records = self._read_run_records(job_dir.name)
                        record = next(
                            (r for r in records if r.get("outputFile") == f.name),
                            self._legacy_run_record(job_dir.name, f),
                        )
                        record["expired"] = True
                        self._write_run_metadata(job_dir, record)
                        f.unlink(missing_ok=True)
                        pruned_files += 1
                    except OSError as exc:
                        logger.warning(f"Cron: failed to expire {f}: {exc}")

        if pruned_files:
            logger.info(
                f"Cron: archive housekeeping — expired {pruned_files} old run(s)"
            )

    def stop(self) -> None:
        """Stop scheduling new work without cancelling an in-flight run.

        This synchronous method remains backward-compatible for embedders that
        only need to disarm the clock.  Process shutdown should await
        :meth:`shutdown`, which gives active work a grace period and records an
        explicit cancelled result if the process cannot wait any longer.
        """
        self._running = False
        self._stopping = True
        timer = self._timer_task
        self._timer_task = None
        if timer and not timer.done():
            timer.cancel()

    async def shutdown(self, grace_period_s: float = 10.0) -> None:
        """Stop scheduling, then drain or durably cancel active executions."""
        self.stop()
        current = asyncio.current_task()

        # Retire reservations whose detached child has not started yet. This
        # section intentionally contains no await: in one event-loop turn we
        # close admission, snapshot ownership, and make every already-issued
        # run ID durable. The child will later see that its reservation was
        # retired and return without invoking the job callback.
        orphaned_completions: list[tuple[CronJob, dict[str, Any]]] = []
        jobs_by_id = {
            job.id: job for job in (self._store.jobs if self._store else [])
        }
        for job_id, run in list(self._active_runs.items()):
            owner = self._execution_owners.get(job_id)
            if owner is not None:
                continue
            job = jobs_by_id.get(job_id)
            if job is None:
                logger.error(
                    f"Cron: cannot persist unowned reservation {run.get('runId')}; "
                    f"job {job_id} is missing"
                )
                del self._active_runs[job_id]
                continue
            completion = self._persist_cancelled_run(
                job,
                run,
                manual=bool(run.get("manual", True)),
                baseline_repeat_completed=job.repeat_completed,
                baseline_consecutive_failures=job.state.consecutive_failures,
                baseline_retry_attempt=job.state.retry_attempt,
            )
            if self._active_runs.get(job_id) is run:
                del self._active_runs[job_id]
            if completion is not None:
                orphaned_completions.append((job, completion))

        tasks = {
            task
            for task in self._execution_tasks
            if task is not current and not task.done()
        }
        scheduler = self._scheduler_task
        if scheduler is not None and scheduler is not current and not scheduler.done():
            tasks.add(scheduler)

        for job, completion in orphaned_completions:
            await self._publish_completion(job, completion)

        if not tasks:
            return

        timeout = max(0.0, float(grace_period_s))
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        if not pending:
            return

        logger.warning(
            f"Cron: cancelling {len(pending)} task(s) after "
            f"{timeout:g}s shutdown grace period"
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def reload(self) -> int:
        """Reload jobs from disk (picks up externally added jobs). Returns job count."""
        old_store = self._load_store()
        active_jobs = {
            job.id: job for job in old_store.jobs if job.id in self._active_runs
        }
        self._store = None  # Clear cache so _load_store reads from disk
        try:
            fresh_store = self._load_store()
        except BaseException:
            # An active run still owns objects from the old store.  Restoring
            # the cache prevents its eventual completion from becoming a
            # no-op save after a failed reload.
            self._store = old_store
            raise

        if active_jobs:
            fresh_by_id = {job.id: index for index, job in enumerate(fresh_store.jobs)}
            for job_id, active_job in active_jobs.items():
                index = fresh_by_id.get(job_id)
                if index is None:
                    # An external rewrite cannot erase a run that is already
                    # executing; retain it until that exact run is durable.
                    fresh_store.jobs.append(active_job)
                else:
                    # Preserve the object `_execute_job` will mutate.  Offline
                    # writers are already forbidden while the gateway is live,
                    # so ignoring an external edit to this one active record is
                    # the only atomic choice.
                    fresh_store.jobs[index] = active_job
            self._store = fresh_store
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        count = len(self._store.jobs) if self._store else 0
        logger.info(f"Cron service reloaded: {count} jobs")
        return count

    def _recompute_next_runs(self) -> None:
        """Repair next runs without losing retry or overdue one-shot state."""
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            if job.lifecycle != "scheduled" or not job.enabled:
                job.enabled = False
                job.state.next_run_at_ms = None
                continue
            if job.state.retry_attempt > 0 and job.state.next_run_at_ms is not None:
                # Persisted backoff is part of the in-progress fire. Recomputing
                # from the base schedule here used to erase it on every restart.
                continue
            if job.schedule.kind == "at":
                if job.state.last_run_at_ms is None:
                    # Keep a persisted due time (including an overdue one), or
                    # restore the original at timestamp for legacy records.
                    job.state.next_run_at_ms = (
                        job.state.next_run_at_ms or job.schedule.at_ms
                    )
                else:
                    job.lifecycle = "completed"
                    job.enabled = False
                    job.completed_at_ms = job.completed_at_ms or job.state.last_run_at_ms
                    job.state.next_run_at_ms = None
                continue
            next_run = _compute_next_run(job.schedule, now)
            if job.schedule.kind == "cron" and next_run is None:
                # Older builds silently treated unknown explicit zones as UTC.
                # Do not keep executing that guess, and do not leave an enabled
                # job stranded with no due time. Pausing preserves the record so
                # a client can repair its timezone or expression.
                logger.error(
                    "Cron: pausing job '{}' because its schedule cannot be resolved",
                    job.name,
                )
                job.lifecycle = "paused"
                job.enabled = False
                job.state.next_run_at_ms = None
                continue
            job.state.next_run_at_ms = next_run

    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [
            j.state.next_run_at_ms
            for j in self._store.jobs
            if (
                j.lifecycle == "scheduled"
                and j.enabled
                and j.state.next_run_at_ms
                and j.id not in self._active_runs
            )
        ]
        return min(times) if times else None

    def _arm_timer(self) -> None:
        """Schedule the next wake without owning the scheduler execution."""
        previous = self._timer_task
        self._timer_task = None
        if previous and not previous.done():
            previous.cancel()

        next_wake = self._get_next_wake_ms()
        if not next_wake or not self._running:
            return

        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000

        async def tick() -> None:
            try:
                await asyncio.sleep(delay_s)
            except asyncio.CancelledError:
                return

            task = asyncio.current_task()
            # A newer re-arm may have replaced this sleeper at the same event
            # loop boundary.  Only the currently-owned wake may dispatch.
            if self._timer_task is not task:
                return
            self._timer_task = None
            if not self._running:
                return
            self._start_scheduler_pass()

        self._timer_task = asyncio.create_task(tick())

    def _start_scheduler_pass(self) -> None:
        """Launch one due-job pass, or let the active pass re-arm when done."""
        current = self._scheduler_task
        if current is not None and not current.done():
            return

        task = asyncio.create_task(self._on_timer())
        self._scheduler_task = task

        def retire(completed: asyncio.Task) -> None:
            if self._scheduler_task is completed:
                self._scheduler_task = None
            # Retrieve unexpected BaseException outcomes so a scheduler bug is
            # visible rather than becoming an unobserved task warning.
            if completed.cancelled():
                return
            try:
                completed.exception()
            except asyncio.CancelledError:
                pass

        task.add_done_callback(retire)

    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs."""
        if self._executing:
            return
        if not self._store:
            return

        # File-based tick lock: non-blocking exclusive lock on
        # ~/.flowly/cron/.tick.lock. If another process (or a stale
        # restart of the gateway) holds the lock, skip this tick so we
        # don't double-fire. The in-process `_executing` flag still
        # covers the common case — this adds crash-safety across
        # process boundaries.
        lock_path = self.store_path.parent / ".tick.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.debug(f"Cron tick: could not create lock dir: {e}")

        lock_fd = None
        try:
            lock_fd = open(lock_path, "w")
            if _fcntl is not None:
                _fcntl.flock(lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            elif _msvcrt is not None:
                _msvcrt.locking(lock_fd.fileno(), _msvcrt.LK_NBLCK, 1)
        except (OSError, IOError):
            logger.debug("Cron tick skipped: another instance holds the lock")
            if lock_fd is not None:
                lock_fd.close()
            return

        self._executing = True
        try:
            await self._run_due_jobs()
        except Exception:
            # A tick failure (e.g. _save_store hitting a full / read-only disk)
            # must NEVER escape: this runs inside the fire-and-forget `tick`
            # task, so an uncaught error kills that task and the timer is never
            # re-armed — silently stopping ALL cron jobs until restart. Log it
            # and fall through to the re-arm in `finally`. (In-memory next_run
            # is advanced before the failing save, so the next wake is in the
            # future — no busy-loop.)
            logger.exception("Cron: tick failed; re-arming and retrying next cycle")
        finally:
            self._executing = False
            try:
                if _fcntl is not None:
                    _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                elif _msvcrt is not None:
                    try:
                        _msvcrt.locking(lock_fd.fileno(), _msvcrt.LK_UNLCK, 1)
                    except (OSError, IOError):
                        pass
            finally:
                lock_fd.close()
            # Always re-arm, even after a failed tick — this is the single point
            # that keeps the self-perpetuating timer chain alive.
            self._arm_timer()

    async def _run_due_jobs(self) -> None:
        now = _now_ms()
        due_jobs: list[CronJob] = []
        grace_saved = False

        for j in self._store.jobs:
            if not (
                j.lifecycle == "scheduled"
                and j.enabled
                and j.state.next_run_at_ms
                and now >= j.state.next_run_at_ms
            ):
                continue

            # Grace window: if a recurring job is past-due by more than
            # grace, the gateway was probably offline during its window —
            # fast-forward to the next future occurrence instead of firing
            # a stale run (and potentially cascading many missed runs on
            # restart). One-shot "at" jobs don't fast-forward; they still
            # want to retry after downtime.
            if j.schedule.kind in ("every", "cron"):
                grace_ms = _compute_grace_ms(j.schedule)
                lateness = now - j.state.next_run_at_ms
                if lateness > grace_ms:
                    new_next = _compute_next_run(j.schedule, now)
                    if new_next:
                        logger.info(
                            f"Cron: job '{j.name}' missed schedule by {lateness}ms "
                            f"(grace={grace_ms}ms), fast-forwarding to next run"
                        )
                        j.state.next_run_at_ms = new_next
                        grace_saved = True
                        continue

            due_jobs.append(j)

        if grace_saved:
            self._save_store()

        for stale_job in due_jobs:
            if self._stopping:
                break
            # The pass may have awaited an earlier due job.  Re-resolve every
            # later entry because archive/purge/pause/update/reload can change
            # it while that first job is running.  Executing this stale
            # snapshot used to run a job the user had already stopped.
            job = next(
                (candidate for candidate in self._store.jobs if candidate.id == stale_job.id),
                None,
            )
            now = _now_ms()
            if not (
                job is not None
                and job.lifecycle == "scheduled"
                and job.enabled
                and job.state.next_run_at_ms
                and now >= job.state.next_run_at_ms
            ):
                continue
            if self.current_run(job.id) is not None:
                logger.info(f"Cron: job '{job.name}' is already running; leaving it due")
                continue
            # Advance next_run_at BEFORE executing recurring jobs so a crash
            # mid-run does not re-fire the job on next startup (at-most-once
            # semantics). One-shot "at" jobs are left alone so they can
            # retry on restart.
            self._advance_next_run(job)

            await self._execute_job(job)

        self._save_store()
        # NOTE: the timer is re-armed by _on_timer's `finally` (which runs even
        # if the _save_store above raises), so a tick failure can't kill the
        # chain. Re-arming here too would just double-schedule the same wake.

    def _save_job_output(
        self,
        job: CronJob,
        *,
        run_id: str,
        run_start_ms: int,
        completed_at_ms: int,
        response: str | None = None,
        error: str | None = None,
        actions: list[str] | None = None,
        files: list[str] | None = None,
        terminal: bool = True,
    ) -> Path:
        """Write a single-run transcript to the per-job archive directory.

        The UUID in the filename prevents two fast/manual runs from replacing
        each other. A small sibling JSON record preserves identity and status
        after the Markdown body expires.
        """
        output_dir = self._job_output_dir(job.id)
        output_dir.mkdir(parents=True, exist_ok=True)

        ts = _dt.datetime.fromtimestamp(run_start_ms / 1000)
        output_file = output_dir / f"{run_start_ms}-{run_id}.md"

        header = f"# Cron Job: {job.name}" + (" (FAILED)" if error else "")
        schedule_display = _format_schedule(job.schedule)

        parts: list[str] = [
            header,
            "",
            f"**Job ID:** {job.id}",
            f"**Run ID:** {run_id}",
            f"**Run Time:** {ts.isoformat()}",
            f"**Schedule:** {schedule_display}",
            "",
        ]
        if job.payload.message:
            parts += ["## Prompt", "", job.payload.message, ""]
        if actions:
            # What the run DID, in call order — the half of a scheduled run
            # that is otherwise lost the moment it finishes.
            parts += ["## Actions", ""]
            parts += [f"- {name}" for name in actions]
            parts += [""]
        if files:
            # Where the run left its output, when that wasn't the reply.
            parts += ["## Files", ""]
            parts += [f"- {path}" for path in files]
            parts += [""]
        if response is not None:
            parts += ["## Response", "", response, ""]
        if error:
            parts += ["## Error", "", "```", error, "```", ""]

        content = "\n".join(parts)

        tmp_path = output_file.with_suffix(f".tmp.{secrets.token_hex(4)}")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(str(tmp_path), str(output_file))
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        from flowly.push.cron_push import cron_notification_snapshot

        metadata = {
            "version": 1,
            "runId": run_id,
            "jobId": job.id,
            "jobName": job.name,
            "startedAtMs": run_start_ms,
            "completedAtMs": completed_at_ms,
            "status": job.state.last_status,
            "error": job.state.last_error,
            "deliveryError": job.state.last_delivery_error,
            "outputFile": output_file.name,
            "expired": False,
            "scheduleKind": job.schedule.kind,
            "lifecycleAfterRun": job.lifecycle,
            "nextRunAtMs": job.state.next_run_at_ms,
            "retryAttempt": job.state.retry_attempt,
            "repeatCompleted": job.repeat_completed,
            "mobileNotification": cron_notification_snapshot(
                job, response, silent=is_silent_response(response), terminal=terminal
            ),
        }
        self._write_run_metadata(output_dir, metadata)
        return output_file

    async def _maybe_send_failure_alert(self, job: CronJob, error_text: str) -> None:
        """Fire the `on_alert` callback when a job has failed enough times.

        Guards:
          * `failure_alert_after == 0` disables alerts entirely.
          * `consecutive_failures` must have reached the threshold.
          * `failure_alert_cooldown_ms` since the previous alert must have
            passed — so a persistently-broken job doesn't spam the user.
        """
        if job.failure_alert_after <= 0:
            return
        if job.state.consecutive_failures < job.failure_alert_after:
            return

        now = _now_ms()
        last_alert = job.state.last_alert_at_ms or 0
        if now - last_alert < job.failure_alert_cooldown_ms:
            logger.debug(
                f"Cron: alert for '{job.name}' suppressed by cooldown "
                f"({(now - last_alert)}ms < {job.failure_alert_cooldown_ms}ms)"
            )
            return

        job.state.last_alert_at_ms = now
        alert_msg = (
            f"⚠️ Scheduled task '{job.name}' has failed "
            f"{job.state.consecutive_failures} times in a row. "
            f"Last error: {error_text[:300]}"
        )
        logger.warning(
            f"Cron: firing failure alert for '{job.name}' "
            f"({job.state.consecutive_failures} consecutive fails)"
        )
        if self.on_alert:
            try:
                await self.on_alert(job, alert_msg)
            except Exception as e:
                logger.warning(f"Cron: alert dispatch failed: {e}")

    async def _run_with_inactivity_timeout(self, job: CronJob) -> Any:
        """Run `on_job(job)` under an inactivity-based watchdog.

        Behaviour:

          * With `activity_probe` wired (default for the gateway) → the
            callback runs as a task; every `_INACTIVITY_POLL_S` seconds
            we read the agent's activity summary. If
            `seconds_since_activity >= _JOB_TIMEOUT_S`, call
            `interrupt_fn(reason)` for a cooperative shutdown and raise
            `asyncio.TimeoutError`. A busy agent (streaming tokens,
            executing tools) never times out because every signal of
            life resets the clock.

          * Without a probe (e.g. unit tests) → fall back to a fixed
            wall-clock `asyncio.wait_for` so behaviour stays bounded.

          * With `_JOB_TIMEOUT_S <= 0` → no timeout at all (the agent
            can run indefinitely).

        """
        if self.on_job is None:
            return None

        if _JOB_TIMEOUT_S <= 0:
            return await self.on_job(job)

        if self.activity_probe is None:
            # No way to observe activity; wall-clock is the only option.
            return await asyncio.wait_for(self.on_job(job), timeout=_JOB_TIMEOUT_S)

        task = asyncio.create_task(self.on_job(job))
        try:
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(task), timeout=_INACTIVITY_POLL_S
                    )
                except asyncio.TimeoutError:
                    # Poll window expired — task still running. Check
                    # agent activity and decide whether to let it keep
                    # going or declare it hung.
                    pass

                try:
                    summary = self.activity_probe() or {}
                    idle_secs = float(summary.get("seconds_since_activity", 0.0))
                except Exception as probe_err:
                    logger.debug(f"Cron: activity probe failed: {probe_err}")
                    idle_secs = 0.0

                if idle_secs >= _JOB_TIMEOUT_S:
                    last_desc = summary.get("last_activity_desc", "unknown")
                    cur_tool = summary.get("current_tool") or "none"
                    iter_count = summary.get("api_call_count", 0)
                    logger.error(
                        f"Cron: job '{job.name}' idle {idle_secs:.0f}s "
                        f"(limit {_JOB_TIMEOUT_S}s) | last={last_desc} | "
                        f"tool={cur_tool} | api_calls={iter_count}"
                    )
                    if self.interrupt_fn is not None:
                        try:
                            self.interrupt_fn(
                                f"Cron inactivity: idle {int(idle_secs)}s "
                                f"(last: {last_desc})"
                            )
                        except Exception as e:
                            logger.debug(f"Cron: interrupt_fn raised: {e}")
                    # Give the agent a short grace window to exit via
                    # the cooperative interrupt check before raising.
                    try:
                        return await asyncio.wait_for(task, timeout=10.0)
                    except asyncio.TimeoutError:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                        raise asyncio.TimeoutError(
                            f"Inactivity timeout after {int(idle_secs)}s"
                        )
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    def _advance_next_run(self, job: CronJob) -> None:
        """Preemptively compute and persist next_run_at for recurring jobs.

        Called before _execute_job() so the job cannot re-fire on crash.
        No-op for one-shot (`at`) jobs — they keep their original run time
        so they can retry after a restart.
        """
        if job.schedule.kind not in ("every", "cron"):
            return
        new_next = _compute_next_run(job.schedule, _now_ms())
        if new_next and new_next != job.state.next_run_at_ms:
            job.state.next_run_at_ms = new_next
            self._save_store()

    def _run_record(self, job: CronJob, started_at_ms: int) -> dict[str, Any]:
        """Describe one in-flight run for live views and the start event.

        ``sessionKey`` is the session the agent turn runs under, so a client
        holding this record can follow the run's partial output through the
        ordinary ``chat.inflight`` RPC — no cron-specific streaming channel.
        """
        return {
            "jobId": job.id,
            "jobName": job.name,
            "runId": str(uuid.uuid4()),
            "startedAtMs": started_at_ms,
            "sessionKey": f"cron:{job.id}",
            "scheduleKind": job.schedule.kind,
        }

    def reserve_run(
        self,
        job: CronJob,
        *,
        manual: bool = False,
    ) -> dict[str, Any] | None:
        """Atomically reserve the per-job run slot for timer/manual callers."""
        if self._stopping:
            return None
        if job.id in self._active_runs:
            return None
        run = self._run_record(job, _now_ms())
        run["manual"] = manual
        self._active_runs[job.id] = run
        return run

    def _persist_cancelled_run(
        self,
        job: CronJob,
        run: dict[str, Any],
        *,
        manual: bool,
        baseline_repeat_completed: int,
        baseline_consecutive_failures: int,
        baseline_retry_attempt: int,
    ) -> dict[str, Any] | None:
        """Turn forced task cancellation into one durable terminal result."""
        start_ms = int(run["startedAtMs"])
        completed_at_ms = _now_ms()
        error_text = (
            "Cancelled during scheduler shutdown"
            if self._stopping else "Cancelled before completion"
        )
        logger.warning(f"Cron: job '{job.name}' {error_text.lower()}")

        job.state.last_status = "error"
        job.state.last_error = error_text
        job.state.last_delivery_error = None
        job.state.last_run_at_ms = start_ms
        job.state.last_run_id = str(run["runId"])
        job.state.consecutive_failures = baseline_consecutive_failures + 1
        job.state.retry_attempt = baseline_retry_attempt if manual else 0
        job.updated_at_ms = completed_at_ms

        if not manual:
            # Shutdown cancellation is a terminal outcome for this fire.  It
            # must not leave a one-shot due so restart executes it a second
            # time under a different run ID.
            job.repeat_completed = baseline_repeat_completed + 1
            reached_limit = (
                job.repeat_times is not None
                and job.repeat_completed >= job.repeat_times
            )
            if reached_limit or job.schedule.kind == "at":
                job.lifecycle = "completed"
                job.enabled = False
                job.state.next_run_at_ms = None
                job.completed_at_ms = completed_at_ms

        archive_ok = False
        try:
            self._save_job_output(
                job,
                run_id=str(run["runId"]),
                run_start_ms=start_ms,
                completed_at_ms=completed_at_ms,
                error=error_text,
                actions=run.get("actions"),
                files=run.get("files"),
            )
            archive_ok = True
            job.state.last_output_error = None
        except Exception as archive_err:
            job.state.last_output_error = (
                f"{type(archive_err).__name__}: {archive_err}"
            )[:500]
            logger.warning(
                f"Cron: failed to archive cancelled output for '{job.name}': "
                f"{archive_err}"
            )

        self._save_store()
        if not archive_ok:
            return None
        return {
            "jobId": job.id,
            "jobName": job.name,
            "runId": run["runId"],
            "status": "error",
            "errorMessage": error_text,
            "deliveryError": None,
            "preview": None,
            "durationMs": completed_at_ms - start_ms,
            "scheduleKind": getattr(job.schedule, "kind", None),
            "sessionKey": f"cron:{job.id}",
            "lifecycle": job.lifecycle,
            "completedAtMs": job.completed_at_ms,
            "outputPersisted": True,
            "silent": False,
            "cancelled": True,
        }

    async def _execute_job(
        self,
        job: CronJob,
        *,
        run: dict[str, Any] | None = None,
        manual: bool = False,
    ) -> bool:
        """Execute one reserved run and publish completion after persistence."""
        run = run or self.reserve_run(job, manual=manual)
        if run is None or self._active_runs.get(job.id) is not run:
            return False
        run["manual"] = manual
        run["lifecycle"] = job.lifecycle
        baseline_repeat_completed = job.repeat_completed
        baseline_consecutive_failures = job.state.consecutive_failures
        baseline_retry_attempt = job.state.retry_attempt

        # ``stop()`` may run after an external caller reserved this ID but
        # before its detached child entered here. Do not start agent work
        # after admission closed; preserve the exact advertised run ID as a
        # durable cancellation instead.
        if self._stopping:
            completion = self._persist_cancelled_run(
                job,
                run,
                manual=manual,
                baseline_repeat_completed=baseline_repeat_completed,
                baseline_consecutive_failures=baseline_consecutive_failures,
                baseline_retry_attempt=baseline_retry_attempt,
            )
            if self._active_runs.get(job.id) is run:
                del self._active_runs[job.id]
            if completion is not None:
                await self._publish_completion(job, completion)
            return False

        owner = asyncio.current_task()
        if owner is not None:
            self._execution_tasks.add(owner)
            self._execution_owners[job.id] = owner
        completion: dict[str, Any] | None = None
        cancelled_error: asyncio.CancelledError | None = None
        try:
            if self.on_run_start is not None:
                try:
                    await self.on_run_start("cron.started", dict(run))
                except Exception as e:
                    logger.warning(
                        f"Cron: on_run_start callback failed for '{job.name}': {e}"
                    )

            completion = await self._run_job_body(job, run=run, manual=manual)
        except asyncio.CancelledError as exc:
            cancelled_error = exc
            completion = self._persist_cancelled_run(
                job,
                run,
                manual=manual,
                baseline_repeat_completed=baseline_repeat_completed,
                baseline_consecutive_failures=baseline_consecutive_failures,
                baseline_retry_attempt=baseline_retry_attempt,
            )
        finally:
            if self._active_runs.get(job.id) is run:
                del self._active_runs[job.id]
            if owner is not None:
                self._execution_tasks.discard(owner)
                if self._execution_owners.get(job.id) is owner:
                    del self._execution_owners[job.id]

        # Listeners must only observe a run after its transcript and terminal
        # job state are durable, and after it is no longer reported active.
        if completion is not None:
            await self._publish_completion(job, completion)
        if cancelled_error is not None:
            # Cancellation remains observable to the task owner after the
            # exact run outcome has been made durable and published.
            raise cancelled_error
        return True

    async def _publish_completion(
        self,
        job: CronJob,
        completion: dict[str, Any],
    ) -> None:
        """Best-effort lifecycle delivery after state and output are durable."""
        if self.on_complete is None:
            return
        try:
            await self.on_complete("cron.completed", completion)
        except Exception as complete_err:
            logger.warning(
                f"Cron: on_complete callback failed for '{job.name}': {complete_err}"
            )

    async def _run_job_body(
        self,
        job: CronJob,
        *,
        run: dict[str, Any],
        manual: bool,
    ) -> dict[str, Any] | None:
        """Execute a single job."""
        start_ms = int(run["startedAtMs"])
        logger.info(f"Cron: executing job '{job.name}' ({job.id})")

        response: str | None = None
        error_text: str | None = None
        # Delivery is evaluated afresh for each run. The gateway callback may
        # set a new delivery error while producing the response.
        job.state.last_delivery_error = None

        try:
            if self.on_job:
                callback_result = await self._run_with_inactivity_timeout(job)
                if isinstance(callback_result, str):
                    response = callback_result

            # Treat explicit error marker as failed execution.
            # Uses a sentinel prefix unlikely to appear in normal text.
            if response is not None and response.startswith("__error__:"):
                raise RuntimeError(response[len("__error__:"):])

            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info(f"Cron: job '{job.name}' completed")

        except asyncio.TimeoutError:
            job.state.last_status = "error"
            error_text = f"Inactivity timeout after {_JOB_TIMEOUT_S}s"
            job.state.last_error = error_text
            logger.error(f"Cron: job '{job.name}' {error_text}")
        except Exception as e:
            job.state.last_status = "error"
            error_text = str(e)[:500]
            job.state.last_error = error_text
            logger.error(f"Cron: job '{job.name}' failed: {e}")

        job.state.last_run_at_ms = start_ms
        job.state.last_run_id = str(run["runId"])
        completed_at_ms = _now_ms()
        job.updated_at_ms = completed_at_ms

        # ─── Retry on failure ───────────────────────────────────────────
        # If the fire failed and the job has retries remaining, override
        # the scheduled next_run_at_ms with a backoff delay and DO NOT
        # advance the repeat counter. This gives transient errors
        # (network hiccup, provider 5xx) a chance to recover without
        # burning a repeat slot or firing a premature failure alert.
        # Retries are scheduled on the main tick loop so other due jobs
        # aren't blocked by a retrying job's backoff sleep.
        retrying = bool(
            not manual
            and error_text
            and job.retry_max_attempts > 0
            and job.state.retry_attempt < job.retry_max_attempts
        )
        if retrying:
            backoffs = job.retry_backoff_ms or [30_000, 60_000, 300_000]
            idx = min(job.state.retry_attempt, len(backoffs) - 1)
            backoff_ms = backoffs[idx]
            job.state.retry_attempt += 1
            job.state.next_run_at_ms = _now_ms() + backoff_ms
            logger.info(
                f"Cron: job '{job.name}' retry "
                f"{job.state.retry_attempt}/{job.retry_max_attempts} "
                f"scheduled in {backoff_ms}ms"
            )
        elif not error_text:
            job.state.retry_attempt = 0
            job.state.consecutive_failures = 0
        else:
            # Real failure (retries exhausted or no retries configured).
            # Bump the consecutive-failure counter and maybe alert.
            job.state.retry_attempt = 0
            job.state.consecutive_failures += 1
            await self._maybe_send_failure_alert(job, error_text)

        # Manual reruns are standalone history entries. They never consume a
        # schedule slot, enable a paused/terminal schedule, or queue retries.
        if not manual and not retrying:
            job.repeat_completed += 1
            reached_limit = (
                job.repeat_times is not None
                and job.repeat_completed >= job.repeat_times
            )
            if reached_limit or job.schedule.kind == "at":
                job.lifecycle = "completed"
                job.enabled = False
                job.state.next_run_at_ms = None
                job.completed_at_ms = completed_at_ms
                logger.info(
                    f"Cron: job '{job.name}' completed its schedule"
                )

        # Save transcript+metadata first, then the job record. If jobs.json
        # fails after metadata lands, startup reconciliation uses the metadata
        # lifecycle/retry snapshot and prevents a terminal one-shot rerun.
        archive_ok = False
        try:
            self._save_job_output(
                job,
                run_id=str(run["runId"]),
                run_start_ms=start_ms,
                completed_at_ms=completed_at_ms,
                response=response if error_text is None else None,
                error=error_text,
                actions=run.get("actions"),
                files=run.get("files"),
                terminal=not retrying,
            )
            archive_ok = True
            job.state.last_output_error = None
        except Exception as archive_err:
            job.state.last_output_error = f"{type(archive_err).__name__}: {archive_err}"[:500]
            logger.warning(
                f"Cron: failed to archive output for '{job.name}': {archive_err}"
            )

        self._save_store()

        if retrying:
            return None
        if not archive_ok:
            # Do not publish a completion notification that suggests the
            # result can be opened when the archive/metadata did not persist.
            return None

        preview = (response or "").strip()
        return {
            "jobId": job.id,
            "jobName": job.name,
            "runId": run["runId"],
            "status": job.state.last_status,
            "errorMessage": job.state.last_error,
            "deliveryError": job.state.last_delivery_error,
            "preview": preview[:500] if preview else None,
            "durationMs": completed_at_ms - start_ms,
            "scheduleKind": getattr(job.schedule, "kind", None),
            "sessionKey": f"cron:{job.id}",
            "lifecycle": job.lifecycle,
            "completedAtMs": job.completed_at_ms,
            "outputPersisted": True,
            "silent": is_silent_response(response),
        }

    # ========== Public API ==========

    def running_runs(self) -> list[dict[str, Any]]:
        """Every run executing right now, oldest first.

        Copies are returned so a caller can't mutate the live bookkeeping.
        """
        return sorted(
            (dict(run) for run in self._active_runs.values()),
            key=lambda r: r.get("startedAtMs") or 0,
        )

    def current_run(self, job_id: str) -> dict[str, Any] | None:
        """The in-flight run for one job, or None when it isn't executing."""
        run = self._active_runs.get(job_id)
        return dict(run) if run else None

    def record_run_actions(self, job_id: str, actions: list[str]) -> None:
        """Attach the tools a run has called, for its archived transcript.

        The scheduler doesn't watch the agent — the ``on_job`` callback does,
        and it reports back through here. Without this, a finished run's
        transcript says what the agent CONCLUDED but not what it actually did,
        which is exactly what you want to see when a scheduled job's answer
        looks wrong.

        A no-op once the run has been retired, so a late report can't attach
        itself to the next one.
        """
        run = self._active_runs.get(job_id)
        if run is not None:
            run["actions"] = list(actions)

    def record_run_files(self, job_id: str, paths: list[str]) -> None:
        """Attach the files a run wrote, for its archived transcript.

        A scheduled job's deliverable is frequently a file on disk rather than
        the reply — "write the report to ~/Desktop" leaves nothing in the
        answer worth reading. Recorded the same way as actions, and a no-op
        once the run is retired.
        """
        run = self._active_runs.get(job_id)
        if run is not None:
            run["files"] = list(paths)

    def list_jobs(
        self,
        include_disabled: bool = False,
        *,
        include_archived: bool = False,
        lifecycles: set[str] | None = None,
    ) -> list[CronJob]:
        """List jobs without conflating paused, completed, and archived."""
        store = self._load_store()
        if lifecycles is not None:
            jobs = [j for j in store.jobs if j.lifecycle in lifecycles]
        elif include_disabled:
            jobs = [j for j in store.jobs if include_archived or j.lifecycle != "archived"]
        else:
            jobs = [j for j in store.jobs if j.lifecycle == "scheduled" and j.enabled]
        return sorted(
            jobs,
            key=lambda j: (
                0 if j.lifecycle == "scheduled" else 1,
                (
                    j.state.next_run_at_ms or float("inf")
                    if j.lifecycle == "scheduled"
                    else -(j.updated_at_ms or j.created_at_ms or 0)
                ),
            ),
        )

    def mark_delivery_error(self, job_id: str, error: str | None) -> None:
        """Record a delivery-time failure separately from an agent-run failure.

        A job can succeed (agent produced output) but fail to deliver (e.g.
        Telegram API returned 503). That isn't a "failed run" — retry and
        failure-alert logic should ignore it. Callers (gateway callback)
        invoke this after catching a `publish_outbound` exception.
        """
        store = self._load_store()
        for j in store.jobs:
            if j.id == job_id:
                j.state.last_delivery_error = error
                self._save_store()
                return

    def update_job(self, job_id: str, updates: dict[str, Any]) -> CronJob | None:
        """Apply a partial update to a job.

        Accepts any of: name, message, schedule (CronSchedule), deliver,
        channel, to, script, skills (list), model, provider, repeat_times.
        Unknown keys are ignored. Returns the updated job, or None if the
        id wasn't found.
        """
        store = self._load_store()
        for job in store.jobs:
            if job.id != job_id and job.name != job_id:
                continue

            normalized_repeat_times: int | None = job.repeat_times
            if "repeat_times" in updates:
                raw_repeat = updates["repeat_times"]
                normalized_repeat_times = (
                    int(raw_repeat) if raw_repeat and int(raw_repeat) > 0 else None
                )

            new_sched = updates.get("schedule")
            reschedule = False
            next_run: int | None = None
            if new_sched is not None:
                if not isinstance(new_sched, CronSchedule):
                    raise ValueError("schedule must be a CronSchedule")
                if new_sched.kind == "cron":
                    schedule_timezone(new_sched.tz)
                reschedule = bool(updates.get("reschedule")) or not _schedule_equal(
                    job.schedule, new_sched
                )
                if reschedule:
                    if new_sched.kind == "every" and (
                        not new_sched.every_ms or new_sched.every_ms < 60_000
                    ):
                        raise ValueError("Minimum interval is 60 seconds")
                    if new_sched.kind == "cron" and new_sched.expr:
                        try:
                            from croniter import croniter
                            croniter(new_sched.expr)
                        except Exception as exc:
                            raise ValueError(f"Invalid cron expression: {exc}") from exc
                    next_run = _compute_next_run(new_sched, _now_ms())
                    if next_run is None:
                        raise ValueError("rescheduled job must have a future valid schedule")

            if "name" in updates and updates["name"]:
                job.name = str(updates["name"])
            if "message" in updates and updates["message"] is not None:
                job.payload.message = str(updates["message"])
            if "deliver" in updates and updates["deliver"] is not None:
                job.payload.deliver = bool(updates["deliver"])
            if "channel" in updates and updates["channel"] is not None:
                job.payload.channel = str(updates["channel"]) or None
            if "to" in updates and updates["to"] is not None:
                job.payload.to = str(updates["to"]) or None
            if "script" in updates:
                raw = updates["script"]
                job.script = str(raw).strip() if raw and str(raw).strip() else None
            if "skills" in updates:
                raw_list = updates["skills"]
                normalized: list[str] = []
                if isinstance(raw_list, list):
                    for s in raw_list:
                        text = str(s or "").strip()
                        if text and text not in normalized:
                            normalized.append(text)
                job.skills = normalized
            if "model" in updates:
                raw = updates["model"]
                job.model = str(raw).strip() if raw and str(raw).strip() else None
            if "provider" in updates:
                raw = updates["provider"]
                job.provider = str(raw).strip() if raw and str(raw).strip() else None
            if "source" in updates:
                raw = updates["source"]
                job.source = str(raw).strip() if raw and str(raw).strip() else None
            if "source_id" in updates or "sourceId" in updates:
                raw = updates.get("source_id", updates.get("sourceId"))
                job.source_id = str(raw).strip() if raw and str(raw).strip() else None
            if "repeat_times" in updates:
                job.repeat_times = normalized_repeat_times

            if reschedule and isinstance(new_sched, CronSchedule):
                old_kind = job.schedule.kind
                job.schedule = new_sched
                job.lifecycle = "scheduled"
                job.enabled = True
                job.completed_at_ms = None
                job.archived_at_ms = None
                job.state.next_run_at_ms = next_run
                job.state.retry_attempt = 0
                job.repeat_completed = 0
                job.delete_after_run = False
                if "repeat_times" not in updates and old_kind != new_sched.kind:
                    job.repeat_times = 1 if new_sched.kind == "at" else None

            job.updated_at_ms = _now_ms()
            self._save_store()
            self._arm_timer()
            return job
        return None

    def update_delivery_target(self, job_id: str, channel: str, to: str) -> bool:
        """Update a job's delivery channel/to without creating a new job.

        Used for reconciliation when the relay-provisioned cronSessionId changes.
        Returns True if updated, False if job not found.
        """
        store = self._load_store()
        for j in store.jobs:
            if j.id == job_id:
                j.payload.channel = channel
                j.payload.to = to
                j.updated_at_ms = _now_ms()
                self._store = store
                self._save_store()
                return True
        return False

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        delete_after_run: bool = False,
        payload_kind: Literal["system_event", "agent_turn", "tool_call"] = "agent_turn",
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        origin: CronOrigin | None = None,
        repeat_times: int | None = None,
        script: str | None = None,
        skills: list[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        retry_max_attempts: int = 0,
        retry_backoff_ms: list[int] | None = None,
        failure_alert_after: int = 3,
        failure_alert_cooldown_ms: int = 24 * 60 * 60 * 1000,
        source: str | None = None,
        source_id: str | None = None,
    ) -> CronJob:
        """Add a new job."""
        if not name or len(name) > 256:
            raise ValueError("Job name must be 1-256 characters")
        if len(message) > 50_000:
            raise ValueError("Job message too long (max 50,000 chars)")

        # Enforce minimum interval to prevent cron bomb / runaway LLM cost.
        # Matches the relay-side validation (defense in depth).
        min_interval_ms = 60_000
        if schedule.kind == "every":
            if not schedule.every_ms or schedule.every_ms < min_interval_ms:
                raise ValueError(
                    f"Minimum interval is {min_interval_ms // 1000} seconds "
                    f"(got {schedule.every_ms}ms)"
                )

        if schedule.kind == "cron":
            schedule_timezone(schedule.tz)

        # Validate cron expression upfront
        if schedule.kind == "cron" and schedule.expr:
            try:
                from croniter import croniter
                croniter(schedule.expr)
            except Exception as e:
                raise ValueError(f"Invalid cron expression '{schedule.expr}': {e}")

        store = self._load_store()
        now = _now_ms()

        # Auto-set repeat_times=1 for one-shot "at" jobs if not specified
        # — one-shot jobs fire once and are deleted.
        if schedule.kind == "at" and repeat_times is None:
            repeat_times = 1
        if repeat_times is not None and repeat_times <= 0:
            repeat_times = None

        # Normalize skill list — dedup while preserving order, strip whitespace,
        # drop empty/falsy entries.
        normalized_skills: list[str] = []
        if skills:
            for s in skills:
                text = str(s or "").strip()
                if text and text not in normalized_skills:
                    normalized_skills.append(text)

        new_job_id = str(uuid.uuid4())[:12]
        normalized_source = str(source).strip() if source and str(source).strip() else None
        normalized_source_id = (
            str(source_id).strip() if source_id and str(source_id).strip()
            else new_job_id if normalized_source else None
        )
        next_run_at_ms = _compute_next_run(schedule, now)
        if schedule.kind == "cron" and next_run_at_ms is None:
            raise ValueError("Cron schedule has no resolvable future occurrence")

        job = CronJob(
            id=new_job_id,
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind=payload_kind,
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
                tool_name=tool_name,
                tool_args=tool_args,
            ),
            state=CronJobState(next_run_at_ms=next_run_at_ms),
            created_at_ms=now,
            updated_at_ms=now,
            lifecycle="scheduled",
            source=normalized_source,
            source_id=normalized_source_id,
            delete_after_run=delete_after_run,
            origin=origin,
            repeat_times=repeat_times,
            repeat_completed=0,
            script=str(script).strip() if script and str(script).strip() else None,
            skills=normalized_skills,
            model=str(model).strip() if model and str(model).strip() else None,
            provider=str(provider).strip() if provider and str(provider).strip() else None,
            retry_max_attempts=max(0, int(retry_max_attempts or 0)),
            retry_backoff_ms=[int(x) for x in (retry_backoff_ms or []) if int(x) > 0],
            failure_alert_after=max(0, int(failure_alert_after or 0)),
            failure_alert_cooldown_ms=max(0, int(failure_alert_cooldown_ms or 0)),
        )

        store.jobs.append(job)
        self._save_store()
        self._arm_timer()

        logger.info(f"Cron: added job '{name}' ({job.id})")
        return job

    def remove_job(self, job_id: str, *, purge: bool = False) -> bool:
        """Archive a job by default; explicit purge removes record and history."""
        store = self._load_store()
        targets = [j for j in store.jobs if j.id == job_id or j.name == job_id]
        if not targets:
            return False
        if any(j.id in self._active_runs for j in targets):
            return False

        if purge:
            target_ids = {j.id for j in targets}
            for target_id in target_ids:
                job_dir = self._job_output_dir(target_id)
                if job_dir.exists():
                    shutil.rmtree(job_dir)
            store.jobs = [j for j in store.jobs if j.id not in target_ids]
            logger.info(f"Cron: permanently purged job/history {job_id}")
        else:
            archived_at = _now_ms()
            for job in targets:
                job.lifecycle = "archived"
                job.enabled = False
                job.state.next_run_at_ms = None
                job.archived_at_ms = archived_at
                job.updated_at_ms = archived_at
            logger.info(f"Cron: archived job {job_id}")

        self._save_store()
        self._arm_timer()
        return True

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Pause/resume an active schedule; terminal jobs need rescheduling."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.updated_at_ms = _now_ms()
                if enabled:
                    if job.lifecycle in {"completed", "archived"}:
                        raise ValueError("completed or archived jobs must be rescheduled")
                    next_run = _compute_next_run(job.schedule, _now_ms())
                    if next_run is None:
                        raise ValueError("job schedule has no future run; reschedule it")
                    job.lifecycle = "scheduled"
                    job.enabled = True
                    job.state.next_run_at_ms = next_run
                else:
                    if job.lifecycle != "scheduled":
                        return job
                    job.lifecycle = "paused"
                    job.enabled = False
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None

    async def run_job(
        self,
        job_id: str,
        force: bool = False,
        *,
        reserved_run: dict[str, Any] | None = None,
    ) -> bool:
        """Run once without changing the job's scheduling lifecycle."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id or job.name == job_id:
                if job.lifecycle == "archived":
                    return False
                if not force and not job.enabled:
                    return False
                run = reserved_run or self.reserve_run(job, manual=True)
                if run is None or self._active_runs.get(job.id) is not run:
                    return False
                ok = await self._execute_job(job, run=run, manual=True)
                self._arm_timer()
                return ok
        return False

    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "scheduledJobs": sum(1 for j in store.jobs if j.lifecycle == "scheduled"),
            "pausedJobs": sum(1 for j in store.jobs if j.lifecycle == "paused"),
            "completedJobs": sum(1 for j in store.jobs if j.lifecycle == "completed"),
            "archivedJobs": sum(1 for j in store.jobs if j.lifecycle == "archived"),
            "next_wake_at_ms": self._get_next_wake_ms(),
            "running": self.running_runs(),
        }

    def health_report(self) -> dict:
        """Return a structured cron-health snapshot for native app UIs.

        Designed for polling from the desktop Activity tab so tasks can
        show per-job badges — "retrying", "broken", "delivery error",
        "stuck". Only lightweight state is included; no archive reads.

        Issue types emitted in `warnings`:
          * `consecutive_failures` — N back-to-back failed runs (user attention)
          * `retrying` — a retry attempt is actively scheduled (informational)
          * `delivery_error` — agent ran ok but outbound transport failed
          * `stuck` — next_run_at is more than 2x grace in the past
                     (service down or scheduler drift — rare)
        """
        store = self._load_store()
        now = _now_ms()
        total = len(store.jobs)
        enabled_count = sum(
            1 for j in store.jobs if j.lifecycle == "scheduled" and j.enabled
        )

        warnings: list[dict] = []
        affected_ids: set[str] = set()

        for j in store.jobs:
            if j.lifecycle != "scheduled" or not j.enabled:
                continue

            if j.state.consecutive_failures > 0:
                warnings.append({
                    "jobId": j.id,
                    "name": j.name,
                    "issue": "consecutive_failures",
                    "severity": "error" if j.state.consecutive_failures >= max(1, j.failure_alert_after) else "warning",
                    "count": j.state.consecutive_failures,
                    "lastError": j.state.last_error,
                })
                affected_ids.add(j.id)

            if j.state.retry_attempt > 0:
                warnings.append({
                    "jobId": j.id,
                    "name": j.name,
                    "issue": "retrying",
                    "severity": "info",
                    "attempt": j.state.retry_attempt,
                    "maxAttempts": j.retry_max_attempts,
                    "nextRunAtMs": j.state.next_run_at_ms,
                })
                affected_ids.add(j.id)

            if j.state.last_delivery_error:
                warnings.append({
                    "jobId": j.id,
                    "name": j.name,
                    "issue": "delivery_error",
                    "severity": "warning",
                    "detail": j.state.last_delivery_error,
                })
                affected_ids.add(j.id)

            if (
                j.schedule.kind in ("every", "cron")
                and j.state.next_run_at_ms
                and now > j.state.next_run_at_ms
            ):
                lateness_ms = now - j.state.next_run_at_ms
                grace_ms = _compute_grace_ms(j.schedule)
                if lateness_ms > grace_ms * 2:
                    warnings.append({
                        "jobId": j.id,
                        "name": j.name,
                        "issue": "stuck",
                        "severity": "warning",
                        "latenessMs": lateness_ms,
                        "graceMs": grace_ms,
                    })
                    affected_ids.add(j.id)

        return {
            "totalJobs": total,
            "enabledJobs": enabled_count,
            "healthyJobs": enabled_count - len(affected_ids),
            "warnings": warnings,
            # Runs executing right now. The desktop already polls this endpoint
            # for the local bot, so carrying live-run state here means the
            # Schedule screen shows "running" without a second round-trip.
            "running": self.running_runs(),
            "timestampMs": now,
        }
