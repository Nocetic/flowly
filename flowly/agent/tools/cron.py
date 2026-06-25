"""Cron tool for scheduling tasks from agent."""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

from loguru import logger

from flowly.agent.tools.base import Tool
from flowly.cron.guard import scan_cron_prompt
from flowly.cron.script_runner import validate_script_path
from flowly.cron.service import CronService
from flowly.cron.types import CronOrigin, CronSchedule


def _parse_duration(duration: str) -> int | None:
    """
    Parse a human-readable duration string to milliseconds.

    Formats:
    - "30s" -> 30 seconds
    - "5m" -> 5 minutes
    - "2h" -> 2 hours
    - "1d" -> 1 day
    - "1w" -> 1 week

    Returns:
        Duration in milliseconds, or None if invalid.
    """
    if not duration:
        return None

    duration = duration.strip().lower()

    try:
        if duration.endswith("s"):
            return int(duration[:-1]) * 1000
        elif duration.endswith("m"):
            return int(duration[:-1]) * 60 * 1000
        elif duration.endswith("h"):
            return int(duration[:-1]) * 60 * 60 * 1000
        elif duration.endswith("d"):
            return int(duration[:-1]) * 24 * 60 * 60 * 1000
        elif duration.endswith("w"):
            return int(duration[:-1]) * 7 * 24 * 60 * 60 * 1000
        else:
            # Try parsing as seconds
            return int(duration) * 1000
    except ValueError:
        return None


def _parse_time(time_str: str) -> int | None:
    """
    Parse a time string to timestamp in milliseconds.

    Formats:
    - "14:30" -> today at 14:30 (or tomorrow if past)
    - "2024-12-25 09:00" -> specific datetime
    - "tomorrow 09:00" -> tomorrow at 09:00
    - "+2h" -> 2 hours from now

    Returns:
        Timestamp in milliseconds, or None if invalid.
    """
    if not time_str:
        return None

    time_str = time_str.strip().lower()
    now = datetime.now()

    try:
        # Relative time: +2h, +30m, etc.
        if time_str.startswith("+"):
            duration_ms = _parse_duration(time_str[1:])
            if duration_ms:
                return int(time.time() * 1000) + duration_ms
            return None

        # "tomorrow HH:MM"
        if time_str.startswith("tomorrow"):
            time_part = time_str.replace("tomorrow", "").strip()
            if time_part:
                hour, minute = map(int, time_part.split(":"))
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                target += timedelta(days=1)
                return int(target.timestamp() * 1000)

        # "HH:MM" - today or tomorrow
        if ":" in time_str and len(time_str) <= 5:
            hour, minute = map(int, time_str.split(":"))
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # If time has passed, schedule for tomorrow
            if target <= now:
                target += timedelta(days=1)
            return int(target.timestamp() * 1000)

        # ISO format: "2024-12-25 09:00" or "2024-12-25T09:00"
        for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"]:
            try:
                dt = datetime.strptime(time_str, fmt)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue

        return None

    except Exception:
        return None


def _format_next_run(timestamp_ms: int | None) -> str:
    """Format a timestamp for display."""
    if not timestamp_ms:
        return "not scheduled"

    dt = datetime.fromtimestamp(timestamp_ms / 1000)
    now = datetime.now()
    diff = dt - now

    if diff.total_seconds() < 0:
        return "overdue"
    elif diff.total_seconds() < 60:
        return f"in {int(diff.total_seconds())}s"
    elif diff.total_seconds() < 3600:
        return f"in {int(diff.total_seconds() / 60)}m"
    elif diff.total_seconds() < 86400:
        return f"in {int(diff.total_seconds() / 3600)}h"
    else:
        return dt.strftime("%Y-%m-%d %H:%M")


class CronTool(Tool):
    """
    Tool for managing scheduled tasks.

    Allows the agent to:
    - List scheduled jobs
    - Add new scheduled jobs (reminders, recurring tasks)
    - Remove or disable jobs
    - Check job status

    Supports multiple schedule types:
    - One-time: "at 14:30", "at 2024-12-25 09:00"
    - Recurring: "every 30m", "every 1d"
    - Cron expressions: "0 9 * * *" (daily at 9am)
    """

    def __init__(self, cron_service: CronService | None = None):
        """
        Initialize the cron tool.

        Args:
            cron_service: CronService instance (will be set by agent loop).
        """
        self._cron_service = cron_service
        self._default_channel: str = ""
        self._default_chat_id: str = ""
        self._default_chat_name: str | None = None
        self._default_thread_id: str | None = None
        # Callable returning the current stable cronSessionId for web channel.
        # Injected from gateway_cmd so web-channel crons route to the
        # "Scheduled Tasks" conversation (same as desktop/web-created ones).
        self._get_web_cron_session_id: Callable[[], str | None] | None = None
        # Firestore sync callbacks — fire-and-forget after local add/remove
        # so bot-created crons appear in iOS/web/desktop UI.
        self._on_cron_register: Callable[[dict], Any] | None = None
        self._on_cron_unregister: Callable[[str], Any] | None = None

    def set_cron_service(self, service: CronService) -> None:
        """Set the cron service instance."""
        self._cron_service = service

    def set_context(
        self,
        channel: str,
        chat_id: str,
        chat_name: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Set the current context for delivery defaults.

        chat_name and thread_id are optional and, when provided, get
        captured into the job's `origin` so delivery can route back to
        a specific topic/thread and logs show a friendly chat label.
        """
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_chat_name = chat_name
        self._default_thread_id = thread_id

    def set_web_cron_session_getter(self, getter: Callable[[], str | None]) -> None:
        """Register a callable that returns the relay-provisioned cronSessionId."""
        self._get_web_cron_session_id = getter

    def set_cron_sync_callbacks(
        self,
        on_register: Callable[[dict], Any] | None = None,
        on_unregister: Callable[[str], Any] | None = None,
    ) -> None:
        """Register async callbacks invoked after local add/remove to sync with Firestore via relay."""
        self._on_cron_register = on_register
        self._on_cron_unregister = on_unregister

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Manage scheduled tasks and reminders. "
            "Use 'list' to see jobs, 'add' to create new ones, 'update' to modify, 'remove' to delete. "
            "Schedules: 'every 30m', 'every 1d', 'at 14:30', 'at tomorrow 09:00', "
            "or cron expressions like '0 9 * * *'. "
            "For deterministic scheduled actions, use tool_name + tool_args. "
            "For data-collection, attach a 'script' (python, relative to ~/.flowly/workspace/). "
            "For skill-guided runs, attach 'skills'. "
            "For per-job LLM override, set 'model' and optionally 'provider'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "update", "remove", "enable", "disable", "status"],
                    "description": "Action to perform"
                },
                "name": {
                    "type": "string",
                    "description": "Job name (for 'add' action)"
                },
                "message": {
                    "type": "string",
                    "description": "Message/prompt to execute when job runs (for 'add')"
                },
                "schedule": {
                    "type": "string",
                    "description": (
                        "Schedule: 'every 30m', 'every 1h', 'every 1d', "
                        "'at 14:30', 'at tomorrow 09:00', 'at +2h', "
                        "or cron expression '0 9 * * *'"
                    )
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID (for 'remove', 'enable', 'disable', 'update')"
                },
                "deliver": {
                    "type": "boolean",
                    "description": "Whether to deliver result to chat (default: false)"
                },
                "channel": {
                    "type": "string",
                    "description": "Channel to deliver to (e.g., 'telegram')"
                },
                "to": {
                    "type": "string",
                    "description": "Chat ID to deliver to"
                },
                "tool_name": {
                    "type": "string",
                    "description": "Optional tool to execute directly when job runs (e.g., 'voice_call')"
                },
                "tool_args": {
                    "type": "object",
                    "description": "Arguments for tool_name when using direct tool execution"
                },
                "script": {
                    "type": "string",
                    "description": (
                        "Optional path (relative to ~/.flowly/workspace/) to a Python "
                        "script that runs BEFORE the agent turn. Its stdout is "
                        "injected into the prompt as context. If the script emits "
                        "'{\"wakeAgent\": false}' as its last JSON line, the agent "
                        "turn is skipped entirely (silent data-collection runs). "
                        "Tip: agents can write a script with write_file and then "
                        "reference it here directly — no second file-layout needed."
                    )
                },
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional ordered list of skill names to load into the "
                        "prompt before the agent runs. Each skill's SKILL.md "
                        "is wrapped with a [SYSTEM:] banner announcing it."
                    )
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional per-job LLM override (e.g. 'anthropic/claude-opus-4'). "
                        "Used only for this job; the gateway default is unchanged."
                    )
                },
                "provider": {
                    "type": "string",
                    "description": (
                        "Optional per-job provider override (e.g. 'openrouter', "
                        "'anthropic'). Pairs with 'model' when set."
                    )
                },
                "repeat_times": {
                    "type": "integer",
                    "description": (
                        "Optional repeat limit. Job runs this many times then "
                        "auto-deletes. Omit (or 0) for forever / default."
                    )
                }
            },
            "required": ["action"]
        }

    async def execute(
        self,
        action: str,
        name: str | None = None,
        message: str | None = None,
        schedule: str | None = None,
        job_id: str | None = None,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        script: str | None = None,
        skills: list[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        repeat_times: int | None = None,
        **kwargs: Any
    ) -> str:
        """Execute cron action."""
        if not self._cron_service:
            return "Error: Cron service not available"

        try:
            if action == "list":
                return self._list_jobs()

            elif action == "add":
                return await self._add_job(
                    name=name,
                    message=message,
                    schedule=schedule,
                    deliver=deliver,
                    channel=channel,
                    to=to,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    script=script,
                    skills=skills,
                    model=model,
                    provider=provider,
                    repeat_times=repeat_times,
                )

            elif action == "update":
                return self._update_job(
                    job_id=job_id,
                    name=name,
                    message=message,
                    schedule=schedule,
                    deliver=deliver,
                    channel=channel,
                    to=to,
                    script=script,
                    skills=skills,
                    model=model,
                    provider=provider,
                    repeat_times=repeat_times,
                )

            elif action == "remove":
                return self._remove_job(job_id)

            elif action == "enable":
                return self._enable_job(job_id, True)

            elif action == "disable":
                return self._enable_job(job_id, False)

            elif action == "status":
                return self._get_status()

            else:
                return f"Error: Unknown action '{action}'"

        except Exception as e:
            logger.error(f"Cron tool error: {e}")
            return f"Error: {str(e)}"

    def _list_jobs(self) -> str:
        """List all scheduled jobs."""
        jobs = self._cron_service.list_jobs(include_disabled=True)

        if not jobs:
            return "No scheduled jobs."

        lines = ["Scheduled Jobs:", ""]

        for job in jobs:
            status = "✓" if job.enabled else "✗"
            next_run = _format_next_run(job.state.next_run_at_ms)

            # Format schedule description
            if job.schedule.kind == "every":
                sched = f"every {job.schedule.every_ms // 1000}s"
                if job.schedule.every_ms >= 60000:
                    sched = f"every {job.schedule.every_ms // 60000}m"
                if job.schedule.every_ms >= 3600000:
                    sched = f"every {job.schedule.every_ms // 3600000}h"
            elif job.schedule.kind == "cron":
                sched = f"cron: {job.schedule.expr}"
            elif job.schedule.kind == "at":
                if job.state.next_run_at_ms:
                    dt = datetime.fromtimestamp(job.state.next_run_at_ms / 1000)
                    sched = f"at {dt.strftime('%Y-%m-%d %H:%M')}"
                else:
                    sched = "one-time (done)"
            else:
                sched = "unknown"

            lines.append(f"[{status}] {job.id}: {job.name}")
            lines.append(f"    Kind: {job.payload.kind}")
            lines.append(f"    Schedule: {sched}")
            lines.append(f"    Next run: {next_run}")
            lines.append(f"    Message: {job.payload.message[:50]}...")
            lines.append("")

        return "\n".join(lines)

    async def _add_job(
        self,
        name: str | None,
        message: str | None,
        schedule: str | None,
        deliver: bool,
        channel: str | None,
        to: str | None,
        tool_name: str | None,
        tool_args: dict[str, Any] | None,
        script: str | None = None,
        skills: list[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        repeat_times: int | None = None,
    ) -> str:
        """Add a new scheduled job."""
        if not name:
            return "Error: 'name' is required for adding a job"
        if not schedule:
            return "Error: 'schedule' is required for adding a job"

        # Validate script path at the tool boundary so bad paths never
        # land in jobs.json. Runtime re-validation covers on-disk edits.
        if script:
            script_err = validate_script_path(script)
            if script_err:
                return f"Error: {script_err}"

        # Scan agent-turn prompts for injection/exfiltration payloads BEFORE
        # persisting. Cron prompts run in fresh sessions with full tool access,
        # so a malicious prompt would bypass the current session's guardrails.
        # tool_call jobs skip this — their `message` is just a synthetic label.
        # Prompt-injection scan for cron job messages — blocks
        # payloads that try to escalate through the fresh session.
        if not tool_name and message:
            threat = scan_cron_prompt(message)
            if threat:
                return f"Error: {threat}"

        payload_kind: Literal["agent_turn", "tool_call"] = "agent_turn"
        if tool_name:
            payload_kind = "tool_call"
            message = message or f"Run tool '{tool_name}'"
            if tool_args is None or not isinstance(tool_args, dict) or not tool_args:
                return "Error: 'tool_args' must be a non-empty object when 'tool_name' is set"

            if tool_name == "voice_call":
                action_value = str(tool_args.get("action", "")).lower()
                to_value = str(tool_args.get("to", "")).strip()
                if action_value != "call":
                    return "Error: voice_call scheduled jobs must set tool_args.action='call'"
                if not to_value:
                    return "Error: voice_call scheduled jobs must set tool_args.to (E.164 phone number)"
                has_greeting = bool(str(tool_args.get("greeting", "")).strip())
                has_script = bool(str(tool_args.get("script", "")).strip())
                if not has_greeting and not has_script:
                    tool_args["script"] = "Hello!"
        elif not message:
            return "Error: 'message' is required for adding a job"

        # Parse schedule
        schedule_obj = self._parse_schedule(schedule)
        if not schedule_obj:
            return (
                f"Error: Invalid schedule '{schedule}'. "
                "Use: 'every 30m', 'at 14:30', 'at tomorrow 09:00', "
                "or cron expression '0 9 * * *'"
            )

        # Use defaults if deliver is true but no target specified.
        #
        # Priority order (option A from the design doc):
        #   1. The current chat's `chat_id` (where the agent was invoked
        #      from). This makes a cron kurulduğu sohbete geri teslim —
        #      "Summarize every morning" kurulduğu chat'te cevap alır,
        #      "Scheduled Tasks" havuzuna değil.
        #   2. Fallback: web-channel `cronSessionId` (server-wide "Scheduled
        #      Tasks" conversation). Used when the agent has no chat
        #      context — e.g. fired from a CLI tool-call without a
        #      session, or from a channel without per-chat routing.
        #
        # Caveat: the current chat's sessionId is ephemeral (relay drops
        # it when the browser disconnects). If the cron fires while the
        # browser is offline, delivery may silently drop. Option B
        # (per-conversation persistent cron session) is the durable fix,
        # but requires a web-app + relay change. For now, option A wins
        # the common case where the user keeps their browser open.
        if deliver:
            if not channel:
                channel = self._default_channel or None
            if not to:
                if self._default_chat_id:
                    to = self._default_chat_id
                elif channel == "web" and self._get_web_cron_session_id:
                    to = self._get_web_cron_session_id()

        # Capture the current session context as the job's origin so
        # delivery can route back to the originating chat (including
        # topic/thread IDs) even after the creating session has ended.
        origin: CronOrigin | None = None
        if self._default_channel or self._default_chat_id:
            origin = CronOrigin(
                platform=self._default_channel or None,
                chat_id=self._default_chat_id or None,
                chat_name=self._default_chat_name,
                thread_id=self._default_thread_id,
            )

        # Add the job
        job = self._cron_service.add_job(
            name=name,
            schedule=schedule_obj,
            message=message,
            deliver=deliver,
            channel=channel,
            to=to,
            delete_after_run=(schedule_obj.kind == "at"),
            payload_kind=payload_kind,
            tool_name=tool_name,
            tool_args=tool_args,
            origin=origin,
            repeat_times=repeat_times,
            script=script,
            skills=skills,
            model=model,
            provider=provider,
        )

        # Fire-and-forget sync to Firestore via relay so bot-created tasks
        # appear in desktop Activity tab, iOS, and web dashboard — exactly
        # like user-created tasks.
        if deliver and channel == "web" and self._on_cron_register:
            try:
                sync_payload = {
                    "name": job.name,
                    "message": message or "",
                    "schedule": {
                        "type": "interval" if schedule_obj.kind == "every"
                        else "at" if schedule_obj.kind == "at"
                        else "cron",
                        "intervalMs": schedule_obj.every_ms,
                        "atMs": schedule_obj.at_ms,
                        "expr": schedule_obj.expr,
                    },
                    "channel": channel,
                }
                result = self._on_cron_register(sync_payload)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                logger.warning(f"Cron Firestore sync failed (non-fatal): {e}")

        next_run = _format_next_run(job.state.next_run_at_ms)

        return (
            f"Created job '{job.name}' (ID: {job.id})\n"
            f"Kind: {job.payload.kind}\n"
            f"Schedule: {schedule}\n"
            f"Next run: {next_run}\n"
            f"Message: {message}"
        )

    def _parse_schedule(self, schedule: str) -> CronSchedule | None:
        """Parse a schedule string into CronSchedule."""
        schedule = schedule.strip()

        # "every X" format
        if schedule.lower().startswith("every "):
            duration_str = schedule[6:].strip()
            duration_ms = _parse_duration(duration_str)
            if duration_ms:
                return CronSchedule(kind="every", every_ms=duration_ms)
            return None

        # "at X" format
        if schedule.lower().startswith("at "):
            time_str = schedule[3:].strip()
            timestamp_ms = _parse_time(time_str)
            if timestamp_ms:
                return CronSchedule(kind="at", at_ms=timestamp_ms)
            return None

        # Cron expression (contains spaces and looks like cron)
        parts = schedule.split()
        if len(parts) == 5 and all(p.replace("*", "").replace(",", "").replace("-", "").replace("/", "").isdigit() or p == "*" for p in parts):
            # Validate cron expression
            try:
                from croniter import croniter
                croniter(schedule)  # Will raise if invalid
                return CronSchedule(kind="cron", expr=schedule)
            except Exception:
                return None

        return None

    def _update_job(
        self,
        job_id: str | None,
        name: str | None = None,
        message: str | None = None,
        schedule: str | None = None,
        deliver: bool | None = None,
        channel: str | None = None,
        to: str | None = None,
        script: str | None = None,
        skills: list[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        repeat_times: int | None = None,
    ) -> str:
        """Update an existing job's fields in place."""
        if not job_id:
            return "Error: 'job_id' is required for updating a job"

        if script:
            script_err = validate_script_path(script)
            if script_err:
                return f"Error: {script_err}"

        if message and not script and message:
            # Scan prompts on update too — prevents swapping in an injection
            # payload after initial safe creation.
            threat = scan_cron_prompt(message)
            if threat:
                return f"Error: {threat}"

        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if message is not None:
            updates["message"] = message
        if deliver is not None:
            updates["deliver"] = deliver
        if channel is not None:
            updates["channel"] = channel
        if to is not None:
            updates["to"] = to
        if script is not None:
            updates["script"] = script
        if skills is not None:
            updates["skills"] = skills
        if model is not None:
            updates["model"] = model
        if provider is not None:
            updates["provider"] = provider
        if repeat_times is not None:
            updates["repeat_times"] = repeat_times
        if schedule is not None:
            schedule_obj = self._parse_schedule(schedule)
            if not schedule_obj:
                return f"Error: Invalid schedule '{schedule}'"
            updates["schedule"] = schedule_obj

        if not updates:
            return "Error: no fields to update"

        job = self._cron_service.update_job(job_id, updates)
        if not job:
            return f"Job {job_id} not found"

        changed = ", ".join(sorted(updates.keys()))
        return f"Updated job '{job.name}' ({job.id}): {changed}"

    def _remove_job(self, job_id: str | None) -> str:
        """Remove a job."""
        if not job_id:
            return "Error: 'job_id' is required for removing a job"

        # Look up name before removing so we can unregister from Firestore
        removed_name: str | None = None
        for j in self._cron_service.list_jobs(include_disabled=True):
            if j.id == job_id:
                removed_name = j.name
                break

        if self._cron_service.remove_job(job_id):
            # Fire-and-forget Firestore unregister
            if removed_name and self._on_cron_unregister:
                try:
                    result = self._on_cron_unregister(removed_name)
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception as e:
                    logger.warning(f"Cron Firestore unregister failed (non-fatal): {e}")
            return f"Removed job {job_id}"
        else:
            return f"Job {job_id} not found"

    def _enable_job(self, job_id: str | None, enable: bool) -> str:
        """Enable or disable a job."""
        if not job_id:
            return f"Error: 'job_id' is required for {'enabling' if enable else 'disabling'} a job"

        job = self._cron_service.enable_job(job_id, enabled=enable)
        if job:
            status = "enabled" if enable else "disabled"
            return f"Job '{job.name}' ({job_id}) {status}"
        else:
            return f"Job {job_id} not found"

    def _get_status(self) -> str:
        """Get cron service status."""
        status = self._cron_service.status()

        lines = [
            "Cron Service Status:",
            f"  Running: {'yes' if status['enabled'] else 'no'}",
            f"  Total jobs: {status['jobs']}",
        ]

        if status.get("next_wake_at_ms"):
            next_wake = _format_next_run(status["next_wake_at_ms"])
            lines.append(f"  Next job: {next_wake}")

        return "\n".join(lines)
