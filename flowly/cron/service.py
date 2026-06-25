"""Cron service for scheduling agent tasks."""

import asyncio
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Coroutine, Literal

from loguru import logger

from flowly.cron.types import CronJob, CronJobState, CronOrigin, CronPayload, CronSchedule, CronStore

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


def is_silent_response(response: str | None) -> bool:
    """Return True if a cron callback response is the [SILENT] sentinel."""
    if not isinstance(response, str):
        return False
    return SILENT_MARKER in response.strip().upper()


def _now_ms() -> int:
    return int(time.time() * 1000)


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
            from croniter import croniter
            import datetime as _dt
            tz = schedule.tz or "UTC"
            try:
                import zoneinfo
                tzinfo = zoneinfo.ZoneInfo(tz)
            except Exception:
                tzinfo = _dt.timezone.utc
            now_aware = _dt.datetime.now(tz=tzinfo)
            cron = croniter(schedule.expr, now_aware)
            first = cron.get_next(_dt.datetime)
            second = cron.get_next(_dt.datetime)
            period_ms = int((second - first).total_seconds() * 1000)
            grace = period_ms // 2
            return max(_GRACE_MIN_MS, min(grace, _GRACE_MAX_MS))
        except Exception:
            pass

    return _GRACE_MIN_MS


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
            from croniter import croniter
            import datetime as _dt
            tz = schedule.tz or "UTC"
            try:
                import zoneinfo
                tzinfo = zoneinfo.ZoneInfo(tz)
            except Exception:
                tzinfo = _dt.timezone.utc
            now_aware = _dt.datetime.now(tz=tzinfo)
            cron = croniter(schedule.expr, now_aware)
            next_dt = cron.get_next(_dt.datetime)
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
        # Inactivity-based timeout wiring. `activity_probe` returns the
        # agent's get_activity_summary() dict (at minimum with
        # `seconds_since_activity`). `interrupt_fn` signals cooperative
        # shutdown to the agent when the inactivity limit is exceeded.
        # Both optional — without them the timeout reverts to wall-clock.
        self.activity_probe = activity_probe
        self.interrupt_fn = interrupt_fn
        self._store: CronStore | None = None
        self._timer_task: asyncio.Task | None = None
        self._running = False
        self._executing = False  # Prevent concurrent _on_timer() calls
    
    def _load_store(self) -> CronStore:
        """Load jobs from disk."""
        if self._store:
            return self._store
        
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                jobs = []
                for j in data.get("jobs", []):
                    origin_raw = j.get("origin")
                    origin_obj = None
                    if isinstance(origin_raw, dict):
                        origin_obj = CronOrigin(
                            platform=origin_raw.get("platform"),
                            chat_id=origin_raw.get("chatId") or origin_raw.get("chat_id"),
                            chat_name=origin_raw.get("chatName") or origin_raw.get("chat_name"),
                            thread_id=origin_raw.get("threadId") or origin_raw.get("thread_id"),
                        )
                    jobs.append(CronJob(
                        id=j["id"],
                        name=j["name"],
                        enabled=j.get("enabled", True),
                        schedule=CronSchedule(
                            kind=j["schedule"]["kind"],
                            at_ms=j["schedule"].get("atMs"),
                            every_ms=j["schedule"].get("everyMs"),
                            expr=j["schedule"].get("expr"),
                            tz=j["schedule"].get("tz"),
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
                            next_run_at_ms=j.get("state", {}).get("nextRunAtMs"),
                            last_run_at_ms=j.get("state", {}).get("lastRunAtMs"),
                            last_status=j.get("state", {}).get("lastStatus"),
                            last_error=j.get("state", {}).get("lastError"),
                            last_delivery_error=j.get("state", {}).get("lastDeliveryError"),
                            consecutive_failures=j.get("state", {}).get("consecutiveFailures", 0),
                            last_alert_at_ms=j.get("state", {}).get("lastAlertAtMs"),
                            retry_attempt=j.get("state", {}).get("retryAttempt", 0),
                        ),
                        created_at_ms=j.get("createdAtMs", 0),
                        updated_at_ms=j.get("updatedAtMs", 0),
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
                self._store = CronStore(jobs=jobs)
            except Exception as e:
                logger.warning(f"Failed to load cron store: {e}")
                self._store = CronStore()
        else:
            self._store = CronStore()
        
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
                        "consecutiveFailures": j.state.consecutive_failures,
                        "lastAlertAtMs": j.state.last_alert_at_ms,
                        "retryAttempt": j.state.retry_attempt,
                    },
                    "createdAtMs": j.created_at_ms,
                    "updatedAtMs": j.updated_at_ms,
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
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        # One-shot housekeeping on gateway boot — prune old run archives
        # and orphaned `output/{job_id}/` folders whose job has been
        # deleted. Cheap, logged, fully skippable via env var.
        try:
            self._prune_archive()
        except Exception as e:
            logger.warning(f"Cron: archive pruning skipped: {e}")
        logger.info(f"Cron service started with {len(self._store.jobs if self._store else [])} jobs")

    def _output_root(self) -> Path:
        """Return the `output/` root (siblings the jobs.json file)."""
        return self.store_path.parent / "output"

    def _prune_archive(self) -> None:
        """Delete stale run transcripts and orphaned per-job archive dirs.

        Two cleanups:

          * **Old transcripts:** any `.md` file under
            `output/{job_id}/` older than `_OUTPUT_RETENTION_DAYS` days
            is removed. 0 disables this pass.

          * **Orphaned directories:** `output/{job_id}/` folders whose
            `job_id` no longer exists in the store (job was removed or
            renamed) are deleted entirely. Runs on every start so users
            who `cron remove` from the CLI don't leak disk.

        Safe to be interrupted — each file is `unlink(missing_ok=True)`;
        each dir is removed only after its files are gone.
        """
        output_root = self._output_root()
        if not output_root.exists():
            return

        now = time.time()
        retention_s = max(0, _OUTPUT_RETENTION_DAYS) * 86400
        live_ids = {j.id for j in (self._store.jobs if self._store else [])}

        pruned_files = 0
        orphan_dirs = 0
        for job_dir in output_root.iterdir():
            if not job_dir.is_dir():
                continue

            is_orphan = job_dir.name not in live_ids
            if is_orphan:
                # Job gone — everything in this dir is stale by definition.
                try:
                    for f in job_dir.iterdir():
                        try:
                            f.unlink(missing_ok=True)
                        except OSError:
                            pass
                    job_dir.rmdir()
                    orphan_dirs += 1
                except OSError:
                    pass
                continue

            if retention_s <= 0:
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
                        f.unlink(missing_ok=True)
                        pruned_files += 1
                    except OSError:
                        pass

        if pruned_files or orphan_dirs:
            logger.info(
                f"Cron: archive housekeeping — pruned {pruned_files} old "
                f"run(s), removed {orphan_dirs} orphan dir(s)"
            )
    
    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    async def reload(self) -> int:
        """Reload jobs from disk (picks up externally added jobs). Returns job count."""
        self._store = None  # Clear cache so _load_store reads from disk
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        count = len(self._store.jobs) if self._store else 0
        logger.info(f"Cron service reloaded: {count} jobs")
        return count
    
    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        if not self._store:
            return
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)
    
    def _get_next_wake_ms(self) -> int | None:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [j.state.next_run_at_ms for j in self._store.jobs 
                 if j.enabled and j.state.next_run_at_ms]
        return min(times) if times else None
    
    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()
        
        next_wake = self._get_next_wake_ms()
        if not next_wake or not self._running:
            return
        
        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000
        
        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()
        
        self._timer_task = asyncio.create_task(tick())
    
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

    async def _run_due_jobs(self) -> None:
        now = _now_ms()
        due_jobs: list[CronJob] = []
        grace_saved = False

        for j in self._store.jobs:
            if not (j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms):
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

        for job in due_jobs:
            # Advance next_run_at BEFORE executing recurring jobs so a crash
            # mid-run does not re-fire the job on next startup (at-most-once
            # semantics). One-shot "at" jobs are left alone so they can
            # retry on restart.
            self._advance_next_run(job)

            await self._execute_job(job)

        self._save_store()
        self._arm_timer()

    def _save_job_output(
        self,
        job: CronJob,
        *,
        run_start_ms: int,
        response: str | None = None,
        error: str | None = None,
    ) -> Path | None:
        """Write a single-run transcript to the per-job archive directory.

        Location: `<store_path.parent>/output/<job_id>/<YYYY-MM-DD_HH-MM-SS>.md`.
        Keeps a human-readable audit trail independent of the live
        job state.
        """
        import datetime as _dt

        output_dir = self.store_path.parent / "output" / job.id
        output_dir.mkdir(parents=True, exist_ok=True)

        ts = _dt.datetime.fromtimestamp(run_start_ms / 1000)
        output_file = output_dir / (ts.strftime("%Y-%m-%d_%H-%M-%S") + ".md")

        header = f"# Cron Job: {job.name}" + (" (FAILED)" if error else "")
        schedule_display = _format_schedule(job.schedule)

        parts: list[str] = [
            header,
            "",
            f"**Job ID:** {job.id}",
            f"**Run Time:** {ts.isoformat()}",
            f"**Schedule:** {schedule_display}",
            "",
        ]
        if job.payload.message:
            parts += ["## Prompt", "", job.payload.message, ""]
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

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single job."""
        start_ms = _now_ms()
        logger.info(f"Cron: executing job '{job.name}' ({job.id})")

        response: str | None = None
        error_text: str | None = None

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

        # Archive this run (success or failure) so past runs can be audited
        # / replayed even after the job is later removed. Non-fatal on error:
        # a failed archive must not fail the run.
        try:
            self._save_job_output(
                job,
                run_start_ms=start_ms,
                response=response if error_text is None else None,
                error=error_text,
            )
        except Exception as archive_err:
            logger.warning(
                f"Cron: failed to archive output for '{job.name}': {archive_err}"
            )

        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = _now_ms()

        # ─── Retry on failure ───────────────────────────────────────────
        # If the fire failed and the job has retries remaining, override
        # the scheduled next_run_at_ms with a backoff delay and DO NOT
        # advance the repeat counter. This gives transient errors
        # (network hiccup, provider 5xx) a chance to recover without
        # burning a repeat slot or firing a premature failure alert.
        # Retries are scheduled on the main tick loop so other due jobs
        # aren't blocked by a retrying job's backoff sleep.
        if error_text and job.retry_max_attempts > 0 and job.state.retry_attempt < job.retry_max_attempts:
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
            return

        # Terminal outcome reached (success, or failure with no retries left).
        # Notify any listener BEFORE the repeat-limit deletion below so the
        # event fires even for a "run once then delete" job. Fire-and-forget:
        # a broken callback must never abort the run's bookkeeping.
        if self.on_complete is not None:
            try:
                preview = (response or "").strip()
                await self.on_complete(
                    "cron.completed",
                    {
                        "jobId": job.id,
                        "jobName": job.name,
                        "status": job.state.last_status,  # "ok" | "error"
                        "errorMessage": job.state.last_error,
                        "preview": preview[:500] if preview else None,
                        "durationMs": _now_ms() - start_ms,
                        "scheduleKind": getattr(job.schedule, "kind", None),
                        "sessionKey": f"cron:{job.id}",
                    },
                )
            except Exception as complete_err:
                logger.warning(
                    f"Cron: on_complete callback failed for '{job.name}': {complete_err}"
                )

        # Success (or retries exhausted) — reset the retry counter so the
        # next fire starts clean.
        if not error_text:
            job.state.retry_attempt = 0
            job.state.consecutive_failures = 0
        else:
            # Real failure (retries exhausted or no retries configured).
            # Bump the consecutive-failure counter and maybe alert.
            job.state.retry_attempt = 0
            job.state.consecutive_failures += 1
            await self._maybe_send_failure_alert(job, error_text)

        # Increment repeat counter for every attempt — success or failure.
        # Counting failures too prevents a persistently-broken job from
        # running forever.
        job.repeat_completed += 1

        # Enforce repeat limit: when `repeat_times` is set and we have hit
        # it, remove the job outright. Applies to all schedule kinds, so a
        # recurring job can be declared as "run N times then delete".
        if job.repeat_times is not None and job.repeat_completed >= job.repeat_times:
            logger.info(
                f"Cron: job '{job.name}' reached repeat limit "
                f"({job.repeat_times}), removing"
            )
            self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            return

        # Handle one-shot jobs without a repeat limit (legacy path for
        # jobs created before repeat_times existed). Recurring jobs have
        # already had their next_run_at advanced in _advance_next_run(),
        # so we don't recompute here.
        if job.schedule.kind == "at":
            if job.delete_after_run:
                self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            else:
                job.enabled = False
                job.state.next_run_at_ms = None
    
    # ========== Public API ==========
    
    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float('inf'))

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
            if "repeat_times" in updates:
                rt = updates["repeat_times"]
                job.repeat_times = int(rt) if rt and int(rt) > 0 else None

            if "schedule" in updates and updates["schedule"] is not None:
                new_sched = updates["schedule"]
                if isinstance(new_sched, CronSchedule):
                    job.schedule = new_sched
                    job.state.next_run_at_ms = _compute_next_run(new_sched, _now_ms())

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
    ) -> CronJob:
        """Add a new job."""
        if not name or len(name) > 256:
            raise ValueError("Job name must be 1-256 characters")
        if len(message) > 50_000:
            raise ValueError("Job message too long (max 50,000 chars)")

        # Enforce minimum interval to prevent cron bomb / runaway LLM cost.
        # Matches the relay-side validation (defense in depth).
        MIN_INTERVAL_MS = 60_000
        if schedule.kind == "every":
            if not schedule.every_ms or schedule.every_ms < MIN_INTERVAL_MS:
                raise ValueError(
                    f"Minimum interval is {MIN_INTERVAL_MS // 1000} seconds "
                    f"(got {schedule.every_ms}ms)"
                )

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

        job = CronJob(
            id=str(uuid.uuid4())[:12],
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
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
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
    
    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID or name, and cascade-delete its archive."""
        store = self._load_store()
        # Collect the UUIDs of every matching job BEFORE we mutate the
        # list — `job_id` can match either id or name, so a single call
        # can reap multiple entries.
        targeted_ids = {j.id for j in store.jobs if j.id == job_id or j.name == job_id}

        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != job_id and j.name != job_id]
        removed = len(store.jobs) < before

        if removed:
            self._save_store()
            self._arm_timer()
            # Cascade-delete the per-job archive so `~/.flowly/cron/output/`
            # doesn't accumulate orphans after `remove_job`. Non-fatal.
            output_root = self._output_root()
            for rid in targeted_ids:
                job_dir = output_root / rid
                if not job_dir.exists():
                    continue
                try:
                    for f in job_dir.iterdir():
                        try:
                            f.unlink(missing_ok=True)
                        except OSError:
                            pass
                    job_dir.rmdir()
                    logger.debug(f"Cron: cleaned archive dir for removed job {rid}")
                except OSError as e:
                    logger.debug(f"Cron: could not clean archive for {rid}: {e}")

            logger.info(f"Cron: removed job {job_id}")

        return removed
    
    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        """Enable or disable a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None
    
    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job. job_id can be the job UUID or name."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id or job.name == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._save_store()
                self._arm_timer()
                return True
        return False
    
    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
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
        enabled_count = sum(1 for j in store.jobs if j.enabled)

        warnings: list[dict] = []
        affected_ids: set[str] = set()

        for j in store.jobs:
            if not j.enabled:
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
            "timestampMs": now,
        }
