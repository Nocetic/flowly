"""Heartbeat service - periodic agent wake-up to check for tasks."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

# Default interval: 30 minutes
DEFAULT_HEARTBEAT_INTERVAL_S = 30 * 60

# Token that indicates "nothing to do"
HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"


def _build_heartbeat_prompt(deliver: bool) -> str:
    """Build the heartbeat prompt sent to the agent."""
    base = (
        "Read HEARTBEAT.md in your workspace (if it exists).\n"
        "Follow any instructions or tasks listed there."
    )
    if deliver:
        return (
            base + "\n"
            "If you have a task result or message for the user, send it using the message tool.\n"
            f"If nothing needs attention, reply with just: {HEARTBEAT_OK_TOKEN}"
        )
    return base + f"\nIf nothing needs attention, reply with just: {HEARTBEAT_OK_TOKEN}"


def _is_heartbeat_empty(content: str | None) -> bool:
    """Check if HEARTBEAT.md has no actionable content."""
    if not content:
        return True

    skip_patterns = {"- [ ]", "* [ ]", "- [x]", "* [x]"}
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("<!--") or line in skip_patterns:
            continue
        return False
    return True


def _is_within_active_hours(start: str, end: str, timezone: str) -> bool:
    """
    Return True if current time is within [start, end] window.

    Args:
        start: "HH:MM" (24h)
        end:   "HH:MM" (24h)
        timezone: IANA tz string (e.g. "Europe/Istanbul"). Empty = system local.
    """
    try:
        tz = ZoneInfo(timezone) if timezone else None
        now = datetime.now(tz=tz)
    except (ZoneInfoNotFoundError, Exception):
        now = datetime.now()

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

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

    async def _tick(self) -> None:
        if not self._check_active_hours():
            logger.debug("Heartbeat: skipped (outside active hours)")
            return

        content = self._read_heartbeat_file()
        if _is_heartbeat_empty(content):
            logger.debug("Heartbeat: no tasks (HEARTBEAT.md empty)")
            return

        logger.info("Heartbeat: checking for tasks...")
        if not self.on_heartbeat:
            return

        try:
            deliver = self.deliver == "message_tool"
            prompt = _build_heartbeat_prompt(deliver=deliver)
            response = await self.on_heartbeat(prompt)

            if HEARTBEAT_OK_TOKEN in response.upper():
                logger.info("Heartbeat: OK (no action needed)")
            else:
                logger.info("Heartbeat: completed task")
        except Exception as e:
            logger.error(f"Heartbeat execution failed: {e}")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat tick."""
        if not self.on_heartbeat:
            return None
        deliver = self.deliver == "message_tool"
        prompt = _build_heartbeat_prompt(deliver=deliver)
        return await self.on_heartbeat(prompt)
