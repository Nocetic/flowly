"""Heartbeat service - periodic agent wake-up to check for tasks."""

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from flowly.utils.wall_clock import sleep_until_wall

# Default interval: 30 minutes
DEFAULT_HEARTBEAT_INTERVAL_S = 30 * 60

# Token that indicates "nothing to do"
HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"

_HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_MARKDOWN_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")
_CHECKED_TASK_RE = re.compile(r"^\s*[-*+]\s+\[[xX]\](?:\s+.*)?$")
_EMPTY_TASK_MARKER_RE = re.compile(
    r"^\s*(?:[-*+](?:\s+\[\s*\])?|(?:\d+[.)]))\s*$"
)


def _build_heartbeat_prompt(deliver: bool, tasks: str) -> str:
    """Build the heartbeat prompt sent to the agent."""
    base = (
        "The following periodic tasks were loaded from HEARTBEAT.md in your "
        "workspace.\n\n"
        f"{tasks.strip()}\n\n"
        "Follow these task instructions."
    )
    if deliver:
        return (
            base + "\n"
            "If you have a task result or message for the user, send it using the message tool.\n"
            f"If nothing needs attention, reply with just: {HEARTBEAT_OK_TOKEN}"
        )
    return base + f"\nIf nothing needs attention, reply with just: {HEARTBEAT_OK_TOKEN}"


def _parse_markdown_heading(line: str) -> tuple[int, str] | None:
    """Return ``(level, normalized title)`` for an ATX Markdown heading."""
    match = _MARKDOWN_HEADING_RE.match(line)
    if not match:
        return None
    return len(match.group(1)), match.group(2).strip().casefold()


def _extract_heartbeat_tasks(content: str | None) -> str:
    """Extract actionable task content while preserving legacy free-form files.

    The bundled template has an ``Active Tasks`` section followed by
    ``Completed``.  When that section exists, only its body is considered;
    explanatory prose elsewhere in the template cannot wake the agent.
    Files created before the sectioned template remain supported by treating
    their whole pre-``Completed`` body as the task area.

    HTML comments, checked tasks, headings, and empty task markers do not make
    a file actionable.  Unchecked checkboxes, bullets, numbered items, and
    free-form instructions all remain valid task formats.
    """
    if not content:
        return ""

    # Remove complete and unterminated HTML comments.  The latter matters for
    # partially edited files: comment text must never become an accidental task.
    lines = _HTML_COMMENT_RE.sub("", content).splitlines()

    active_start: int | None = None
    active_level: int | None = None
    for index, line in enumerate(lines):
        heading = _parse_markdown_heading(line)
        if heading and heading[1] == "active tasks":
            active_start = index + 1
            active_level = heading[0]
            break

    scoped: list[str] = []
    if active_start is not None and active_level is not None:
        for line in lines[active_start:]:
            heading = _parse_markdown_heading(line)
            if heading and heading[0] <= active_level:
                break
            scoped.append(line)
    else:
        # Legacy files had no formal Active Tasks section.  Preserve their
        # free-form behavior, but do not resurrect entries under Completed.
        for line in lines:
            heading = _parse_markdown_heading(line)
            if heading and heading[1] in {"completed", "completed tasks"}:
                break
            scoped.append(line)

    visible: list[str] = []
    has_actionable_content = False
    for line in scoped:
        stripped = line.strip()
        if not stripped:
            visible.append("")
            continue
        if _parse_markdown_heading(line):
            # Keep subheadings as useful grouping context when a real task
            # follows, but a heading by itself must not wake the agent.
            visible.append(line.rstrip())
            continue
        if _CHECKED_TASK_RE.match(line) or _EMPTY_TASK_MARKER_RE.match(line):
            continue
        visible.append(line.rstrip())
        has_actionable_content = True

    if not has_actionable_content:
        return ""
    return "\n".join(visible).strip()


def _is_heartbeat_empty(content: str | None) -> bool:
    """Check if HEARTBEAT.md has no actionable content."""
    return not _extract_heartbeat_tasks(content)


def _is_within_active_hours(start: str, end: str, timezone: str, at: float | None = None) -> bool:
    """
    Return True if the current time (or ``at``, epoch seconds) is within [start, end].

    Args:
        start: "HH:MM" (24h)
        end:   "HH:MM" (24h)
        timezone: IANA tz string (e.g. "Europe/Istanbul"). Empty = system local.
        at: the moment to test instead of now.
    """
    try:
        tz = ZoneInfo(timezone) if timezone else None
        now = datetime.now(tz=tz) if at is None else datetime.fromtimestamp(at, tz=tz)
    except (ZoneInfoNotFoundError, Exception):
        now = datetime.now() if at is None else datetime.fromtimestamp(at)

    def _parse(t: str) -> tuple[int, int]:
        parts = t.strip().split(":")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0

    sh, sm = _parse(start)
    eh, em = _parse(end)
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    now_minutes = now.hour * 60 + now.minute

    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes <= end_minutes
    # Overnight window (e.g. 22:00 – 06:00)
    return now_minutes >= start_minutes or now_minutes <= end_minutes


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    The agent reads HEARTBEAT.md from the workspace and executes any
    tasks listed there. If nothing needs attention, it replies HEARTBEAT_OK
    (silently suppressed). Non-OK responses can optionally be delivered via
    the message tool (deliver="message_tool").

    active_hours: optional dict with "start", "end" (HH:MM 24h), "timezone" (IANA).
    """

    def __init__(
        self,
        workspace: Path,
        on_heartbeat: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S,
        enabled: bool = True,
        active_hours: dict[str, str] | None = None,
        deliver: str = "none",
    ):
        self.workspace = workspace
        self.on_heartbeat = on_heartbeat
        self.interval_s = interval_s
        self.enabled = enabled
        self.active_hours = active_hours  # {"start": "HH:MM", "end": "HH:MM", "timezone": "..."}
        self.deliver = deliver  # "none" | "message_tool"
        self._running = False
        self._task: asyncio.Task | None = None
        # When the periodic loop runs next (epoch seconds); None while stopped.
        # A triggered heartbeat pushes it a full interval out, so a run the
        # Flowly server asked for is not repeated by this timer moments later.
        self._next_tick_at: float | None = None
        # Heartbeat turns in progress (periodic or triggered).
        self._in_progress = 0
        self._schedule_listener: Callable[[], None] | None = None

    @property
    def running(self) -> bool:
        """True while a heartbeat turn is in progress."""
        return self._in_progress > 0

    def set_schedule_listener(self, callback: Callable[[], None] | None) -> None:
        """Call ``callback`` whenever the next periodic tick moves."""
        self._schedule_listener = callback

    def _set_next_tick(self, at: float | None) -> None:
        self._next_tick_at = at
        if self._schedule_listener is not None:
            try:
                self._schedule_listener()
            except Exception as e:
                logger.warning(f"Heartbeat schedule listener failed: {e}")

    def next_due_at(self) -> float | None:
        """When the next heartbeat that would do work is due (epoch seconds).

        None when the loop is not running, HEARTBEAT.md has no active task, or
        no tick falls inside the active hours within a week. A tick outside
        the active hours is skipped, so the first tick inside them is the one
        a sleeping machine must be woken for.
        """
        if not self.enabled or not self._running or self.on_heartbeat is None or self._next_tick_at is None:
            return None
        if not _extract_heartbeat_tasks(self._read_heartbeat_file()):
            return None
        at = self._next_tick_at
        if not self.active_hours:
            return at
        interval = max(1, int(self.interval_s))
        for _ in range(min(10_000, 7 * 24 * 3600 // interval + 1)):
            if _is_within_active_hours(
                self.active_hours.get("start", "09:00"),
                self.active_hours.get("end", "23:00"),
                self.active_hours.get("timezone", ""),
                at=at,
            ):
                return at
            at += interval
        return None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text()
            except Exception:
                return None
        return None

    def _check_active_hours(self) -> bool:
        """Return True if heartbeat should run now (active_hours not set, or within window)."""
        if not self.active_hours:
            return True
        return _is_within_active_hours(
            self.active_hours.get("start", "09:00"),
            self.active_hours.get("end", "23:00"),
            self.active_hours.get("timezone", ""),
        )

    async def start(self, run_on_start: bool = False) -> None:
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        self._running = True
        hours_info = ""
        if self.active_hours:
            hours_info = f", active {self.active_hours.get('start')}–{self.active_hours.get('end')}"
        logger.info(f"Heartbeat started (every {self.interval_s}s{hours_info})")
        if run_on_start:
            # Fire immediately without waiting for first sleep
            asyncio.create_task(self._tick())
        self._task = asyncio.create_task(self._run_loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self._set_next_tick(None)

    async def _run_loop(self) -> None:
        self._set_next_tick(time.time() + self.interval_s)
        while self._running:
            try:
                # The wall clock: a heartbeat due while the machine was
                # suspended runs as soon as it resumes.
                await sleep_until_wall(self._next_tick_at)
                if not self._running:
                    break
                if self._next_tick_at is not None and self._next_tick_at > time.time():
                    continue  # a triggered heartbeat pushed this tick out
                await self._tick()
                self._set_next_tick(time.time() + self.interval_s)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                self._set_next_tick(time.time() + self.interval_s)

    async def _run_heartbeat(self, prompt: str) -> str:
        self._in_progress += 1
        try:
            return await self.on_heartbeat(prompt)
        finally:
            self._in_progress -= 1

    async def _tick(self) -> None:
        if not self._check_active_hours():
            logger.debug("Heartbeat: skipped (outside active hours)")
            return

        tasks = _extract_heartbeat_tasks(self._read_heartbeat_file())
        if not tasks:
            logger.debug("Heartbeat: no tasks (HEARTBEAT.md empty)")
            return

        logger.info("Heartbeat: checking for tasks...")
        if not self.on_heartbeat:
            return
        if self.running:
            logger.debug("Heartbeat: skipped (a heartbeat is already running)")
            return

        try:
            deliver = self.deliver == "message_tool"
            prompt = _build_heartbeat_prompt(deliver=deliver, tasks=tasks)
            response = await self._run_heartbeat(prompt)

            if HEARTBEAT_OK_TOKEN in response.upper():
                logger.info("Heartbeat: OK (no action needed)")
            else:
                logger.info("Heartbeat: completed task")
        except Exception as e:
            logger.error(f"Heartbeat execution failed: {e}")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat tick.

        Returns the agent's reply, ``HEARTBEAT_OK`` when there is no active
        task, or None when no agent is wired or a heartbeat is already running.
        The periodic tick moves a full interval past this run.
        """
        if not self.on_heartbeat or self.running:
            return None
        if self._running:
            self._set_next_tick(time.time() + self.interval_s)
        tasks = _extract_heartbeat_tasks(self._read_heartbeat_file())
        if not tasks:
            return HEARTBEAT_OK_TOKEN
        deliver = self.deliver == "message_tool"
        prompt = _build_heartbeat_prompt(deliver=deliver, tasks=tasks)
        return await self._run_heartbeat(prompt)

    async def trigger(self) -> str:
        """Run a heartbeat now and say how it went.

        ``"ok"`` (nothing needed attention), ``"acted"``, ``"no-tasks"``,
        ``"busy"`` (one is already running) or ``"unavailable"`` (no agent).
        """
        if not self.on_heartbeat:
            return "unavailable"
        if self.running:
            return "busy"
        if not _extract_heartbeat_tasks(self._read_heartbeat_file()):
            if self._running:
                self._set_next_tick(time.time() + self.interval_s)
            return "no-tasks"
        response = await self.trigger_now()
        if response is None:
            return "busy"
        return "ok" if HEARTBEAT_OK_TOKEN in response.upper() else "acted"
