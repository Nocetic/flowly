"""Cron types."""

from dataclasses import dataclass, field
from typing import Any
from typing import Literal


@dataclass
class CronOrigin:
    """Where a cron job was created, for auto-delivery back to the source.

    Captured from the session context at job-creation time and persisted
    with the job so the scheduler can route the output back to the
    originating chat even after the creating session has ended.
    """
    platform: str | None = None
    chat_id: str | None = None
    chat_name: str | None = None
    thread_id: str | None = None


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload:
    """What to do when the job runs."""
    kind: Literal["system_event", "agent_turn", "tool_call"] = "agent_turn"
    message: str = ""
    # Deliver response to channel
    deliver: bool = False
    channel: str | None = None  # e.g. "whatsapp"
    to: str | None = None  # e.g. phone number
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None


@dataclass
class CronJobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None
    # Delivery (outbound publish) errors are tracked separately from agent
    # errors. A job can run cleanly but fail to deliver — e.g. Telegram API
    # down — and should NOT be marked as a failed *run*.
    last_delivery_error: str | None = None
    # Number of back-to-back failures the scheduler is using to decide
    # whether to fire a failure alert (reset on success). See Phase 4.2.
    consecutive_failures: int = 0
    # Timestamp of the most recent failure alert delivered for this job,
    # used for the alert-cooldown check so a broken job doesn't spam.
    last_alert_at_ms: int | None = None
    # Current retry attempt for the in-progress fire. Resets to 0 on
    # success or when max retries are exhausted and the job is let through
    # as a "real" failure. See Phase 4.1.
    retry_attempt: int = 0


@dataclass
class CronJob:
    """A scheduled job."""
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False
    # Where this job was created — auto-captured from CronTool.set_context().
    # Carries richer routing metadata than `payload.channel`/`payload.to`
    # (thread/topic IDs, friendly chat name) and preserves it even if the
    # explicit delivery target is later changed.
    origin: CronOrigin | None = None
    # Repeat limit: None = forever, 1 = one-shot, N = run N times then delete.
    # `completed` is incremented after each successful run.
    repeat_times: int | None = None
    repeat_completed: int = 0
    # Optional data-collection script that runs before the agent turn. Path
    # is relative to `~/.flowly/scripts/`; validated at tool boundary and at
    # execution. Its stdout is injected into the prompt as context. If the
    # script prints JSON ending with `{"wakeAgent": false}`, the agent turn
    # is skipped entirely (no-op data collection runs).
    script: str | None = None
    # Optional ordered list of skill names to load into the prompt before
    # the agent runs. Each skill's SKILL.md content is wrapped with a
    # [SYSTEM:] notice announcing the skill to the agent.
    skills: list[str] = field(default_factory=list)
    # Optional per-job model override. None = use the gateway's current
    # default. Allows "use opus for this report, haiku for the rest."
    model: str | None = None
    # Optional per-job provider override (e.g. "openrouter", "anthropic").
    # Resolved fresh each run via the runtime provider resolver.
    provider: str | None = None
    # How many additional attempts to make if a fire fails with a
    # transient error (timeout, network, 5xx). 0 = no retry (default,
    # backward-compat). Retries happen on subsequent scheduler ticks so
    # other due jobs aren't starved.
    retry_max_attempts: int = 0
    # Backoff schedule in milliseconds. If shorter than `retry_max_attempts`,
    # the last value repeats. Empty → defaults to [30s, 60s, 5min].
    # Values are added to `now` to derive the next
    # attempt's `next_run_at_ms`.
    retry_backoff_ms: list[int] = field(default_factory=list)
    # How many consecutive failures to tolerate before sending a "job is
    # broken" notification to the delivery target. 0 disables the alert
    # entirely.
    failure_alert_after: int = 3
    # Minimum time between failure alerts, so a persistently-broken job
    # doesn't spam. Default 24h.
    failure_alert_cooldown_ms: int = 24 * 60 * 60 * 1000


@dataclass
class CronStore:
    """Persistent store for cron jobs."""
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
