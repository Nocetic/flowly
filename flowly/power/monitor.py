"""Answers the Flowly server's power questions (server/PROTOCOL.md B4).

A cloud machine sleeps when it is idle. The server asks Core whether it may
(``power.status``), asks it to get ready (``power.sleep``) and wakes it again
for the next scheduled work, which Core announces (``power.next_due``):

- busy: a turn (also a named agent's), a routine (cron run), a heartbeat, memory learning (dreamer or
  consolidation, running or owed), a watched screen, maintenance, recent use,
  a fresh wake, or work due within minutes;
- next due: the earliest cron job, heartbeat (only with active tasks and inside
  the active hours) and nightly dreaming pass (only when one is owed);
- dream before sleep: when the only thing in the way is memory learning,
  ``power.sleep`` starts it and says ``dream``; the server asks again later.

After ``power.sleep`` succeeds Core takes no new work until it wakes: the
server suspends the machine; on resume Core sees the wall clock jump, or it
reconnects, and is awake again.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from loguru import logger

BUSY_CODES = (
    "turn", "run", "heartbeat", "dream", "screen", "maintenance",
    "recent-activity", "just-woke", "due-soon",
)
# Codes a caller may add through ``add_busy_probe``.
_PROBE_CODES = frozenset({"turn", "run", "heartbeat", "dream", "screen", "maintenance"})

#: A machine stays up this long after it starts or reconnects, so queued
#: messages and the server's own trigger reach it before it sleeps again.
JUST_WOKE_SECONDS = 60
#: Work due this soon keeps the machine up; the server wakes it a minute early.
DUE_SOON_SECONDS = 180
#: The wall clock ran this much further than the monotonic one: the machine
#: was suspended (a suspend stops CLOCK_MONOTONIC, flowly/utils/wall_clock.py).
RESUME_GAP_SECONDS = 20
#: How often the watch loop looks for a resume and a moved next-due time
#: (HEARTBEAT.md edits and active-hours edges have no event of their own).
WATCH_INTERVAL_SECONDS = 15

NextDueListener = Callable[[int | None], Awaitable[None] | None]


def _ms(at: float | None) -> int | None:
    return None if at is None else int(at * 1000)


class PowerMonitor:
    """Core's side of sleeping: what is busy, what is due, and the sleep handshake.

    Every collaborator is optional and duck-typed:

    - ``agent``: ``power_snapshot()`` → ``{turns, dreamRunning, dreamOwed,
      nextDreamAt, lastUserActivityAt}`` and ``async dream_before_sleep()``
      (``AgentLoop``);
    - ``cron``: ``running_runs()``, ``next_due_at_ms()``,
      ``add_schedule_listener(cb)`` (``CronService``);
    - ``heartbeat``: ``running``, ``next_due_at()``, ``set_schedule_listener(cb)``,
      ``async trigger()`` (``HeartbeatService``);
    - ``flush``: writes memory and disk before the machine sleeps.
    """

    def __init__(
        self,
        *,
        agent: Any = None,
        cron: Any = None,
        heartbeat: Any = None,
        flush: Callable[[], None] | None = None,  # sync; run in a worker thread
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._agent = agent
        self._cron = cron
        self._heartbeat = heartbeat
        self._flush = flush
        self._wall = wall
        self._mono = monotonic
        self._probes: list[tuple[str, Callable[[], bool]]] = []
        self._listeners: list[NextDueListener] = []
        self._woke_at = wall()
        self._clock_mark = (wall(), monotonic())
        self._last_activity: float | None = None
        self._sleeping = False
        self._dream_task: asyncio.Task | None = None
        self._dream_attempted_at: float | None = None
        self._announced: int | None | object = _UNSET
        self._notify_task: asyncio.Task | None = None
        if cron is not None and hasattr(cron, "add_schedule_listener"):
            cron.add_schedule_listener(self.schedule_changed)
        if heartbeat is not None and hasattr(heartbeat, "set_schedule_listener"):
            heartbeat.set_schedule_listener(self.schedule_changed)

    # ── inputs ────────────────────────────────────────────────────────────

    def add_busy_probe(self, code: str, probe: Callable[[], bool]) -> None:
        """Report ``code`` as busy whenever ``probe()`` is true (or raises)."""
        if code not in _PROBE_CODES:
            raise ValueError(f"unknown busy code: {code}")
        self._probes.append((code, probe))

    def touch(self) -> None:
        """Someone used the agent (a chat message, a settings change, …)."""
        self._last_activity = self._wall()

    def mark_connected(self) -> None:
        """Core (re)connected to the server: it is awake and just woke."""
        self._sleeping = False
        self._woke_at = self._wall()

    def wake(self) -> None:
        """The server cancelled a sleep (its suspend failed)."""
        self._sleeping = False
        self._woke_at = self._wall()

    @property
    def sleeping(self) -> bool:
        self._check_resume()
        return self._sleeping

    def _check_resume(self) -> None:
        wall, mono = self._wall(), self._mono()
        last_wall, last_mono = self._clock_mark
        self._clock_mark = (wall, mono)
        if (wall - last_wall) - (mono - last_mono) > RESUME_GAP_SECONDS:
            logger.info("[power] resumed after a suspend")
            self._sleeping = False
            self._woke_at = wall

    # ── status ────────────────────────────────────────────────────────────

    def _last_activity_at(self, snapshot: dict) -> float | None:
        values = [v for v in (self._last_activity, snapshot.get("lastUserActivityAt")) if v]
        return max(values) if values else None

    def _snapshot(self) -> dict:
        if self._agent is None:
            return {}
        try:
            return dict(self._agent.power_snapshot() or {})
        except Exception as exc:
            logger.warning(f"[power] agent snapshot failed: {exc}")
            # Unknown is not idle.
            return {"turns": 1}

    def _dream_settled(self, last_activity: float | None) -> bool:
        """A before-sleep attempt already ran and nothing new happened since."""
        attempted = self._dream_attempted_at
        return attempted is not None and (last_activity is None or last_activity <= attempted)

    def next_due_at_ms(self) -> int | None:
        """The earliest of the next cron job, heartbeat and owed nightly dream."""
        return self._next_due_at_ms(self._snapshot())

    def _next_due_at_ms(self, snapshot: dict) -> int | None:
        candidates: list[int] = []
        if self._cron is not None:
            try:
                value = self._cron.next_due_at_ms()
                if value:
                    candidates.append(int(value))
            except Exception as exc:
                logger.warning(f"[power] cron next due failed: {exc}")
        if self._heartbeat is not None:
            try:
                value = self._heartbeat.next_due_at()
                if value:
                    candidates.append(int(value * 1000))
            except Exception as exc:
                logger.warning(f"[power] heartbeat next due failed: {exc}")
        if snapshot.get("nextDreamAt"):
            candidates.append(int(snapshot["nextDreamAt"] * 1000))
        return min(candidates) if candidates else None

    def _busy(self, idle_minutes: float | None) -> tuple[list[str], int | None, float | None]:
        self._check_resume()
        now = self._wall()
        snapshot = self._snapshot()
        busy: set[str] = set()
        if snapshot.get("turns"):
            busy.add("turn")
        if self._cron is not None:
            try:
                if self._cron.running_runs():
                    busy.add("run")
            except Exception:
                busy.add("run")
        if self._heartbeat is not None and getattr(self._heartbeat, "running", False):
            busy.add("heartbeat")
        last_activity = self._last_activity_at(snapshot)
        dream_task_running = self._dream_task is not None and not self._dream_task.done()
        if snapshot.get("dreamRunning") or dream_task_running:
            busy.add("dream")
        elif snapshot.get("dreamOwed") and not self._dream_settled(last_activity):
            busy.add("dream")
        for code, probe in self._probes:
            try:
                if probe():
                    busy.add(code)
            except Exception as exc:
                logger.warning(f"[power] {code} probe failed: {exc}")
                busy.add(code)
        if idle_minutes and last_activity is not None and now - last_activity < idle_minutes * 60:
            busy.add("recent-activity")
        if now - self._woke_at < JUST_WOKE_SECONDS:
            busy.add("just-woke")
        next_due = self._next_due_at_ms(snapshot)
        if next_due is not None and next_due - now * 1000 < DUE_SOON_SECONDS * 1000:
            busy.add("due-soon")
        return [code for code in BUSY_CODES if code in busy], next_due, last_activity

    def status(self, idle_minutes: float | None = None) -> dict:
        """``power.status``: ``{idle, busy, nextDueAt, lastActivityAt}``."""
        busy, next_due, last_activity = self._busy(idle_minutes)
        return {"idle": not busy, "busy": busy, "nextDueAt": next_due, "lastActivityAt": _ms(last_activity)}

    # ── sleep ─────────────────────────────────────────────────────────────

    async def sleep(self, idle_minutes: float | None = None) -> dict:
        """``power.sleep``: refuse with the busy list, dream first, or get ready.

        Only memory learning is started here; any other busy reason refuses
        without side effects. After ``ok`` Core takes no new work until it wakes.
        """
        busy, next_due, _ = self._busy(idle_minutes)
        if busy:
            if busy == ["dream"]:
                self._start_dream()
            return {"ok": False, "busy": busy}
        self._sleeping = True
        if self._flush is not None:
            try:
                await asyncio.to_thread(self._flush)
            except Exception as exc:
                logger.warning(f"[power] flush before sleep failed: {exc}")
        logger.info("[power] ready to sleep")
        return {"ok": True, "nextDueAt": next_due}

    def _start_dream(self) -> None:
        if self._dream_task is not None and not self._dream_task.done():
            return
        dream = getattr(self._agent, "dream_before_sleep", None)
        if dream is None:
            self._dream_attempted_at = self._wall()
            return

        async def run() -> None:
            try:
                logger.info("[power] dreaming before sleep")
                await dream()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[power] dream before sleep failed: {exc}")
            finally:
                self._dream_attempted_at = self._wall()
                self.schedule_changed()

        self._dream_task = asyncio.create_task(run(), name="power-dream-before-sleep")

    async def wait_for_dream(self) -> None:
        """Wait for a before-sleep pass started by ``sleep`` (tests, shutdown)."""
        task = self._dream_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    # ── heartbeat ─────────────────────────────────────────────────────────

    async def trigger_heartbeat(self) -> dict:
        """``heartbeat.trigger``: run the heartbeat now; ``outcome`` says how it went."""
        if self._heartbeat is None or not hasattr(self._heartbeat, "trigger"):
            return {"ok": False, "outcome": "unavailable", "nextDueAt": self.next_due_at_ms()}
        outcome = await self._heartbeat.trigger()
        return {"ok": outcome != "unavailable", "outcome": outcome, "nextDueAt": self.next_due_at_ms()}

    # ── next due ──────────────────────────────────────────────────────────

    def add_next_due_listener(self, listener: NextDueListener) -> None:
        self._listeners.append(listener)

    def schedule_changed(self) -> None:
        """A schedule moved (sync hook for cron/heartbeat); announce if it changed."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._notify_task = loop.create_task(self.check_next_due())

    async def check_next_due(self, *, force: bool = False) -> None:
        """Tell listeners the next due time when it changed (or ``force``)."""
        value = self.next_due_at_ms()
        if not force and value == self._announced:
            return
        self._announced = value
        for listener in tuple(self._listeners):
            try:
                result = listener(value)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning(f"[power] next-due listener failed: {exc}")

    async def watch(self, interval: float = WATCH_INTERVAL_SECONDS) -> None:
        """Look for a resume and a moved next-due time until cancelled."""
        while True:
            await asyncio.sleep(interval)
            try:
                self._check_resume()
                await self.check_next_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[power] watch failed: {exc}")


_UNSET = object()


# ── probes and flush helpers used by the gateway ──────────────────────────


def add_gateway_probes(monitor: PowerMonitor, gateway: Any) -> None:
    """The busy probes a gateway adds: named agents' turns, a watched screen, maintenance."""
    monitor.add_busy_probe("turn", lambda: named_agent_turns(gateway))
    monitor.add_busy_probe("screen", lambda: screen_watched(gateway))
    monitor.add_busy_probe("maintenance", lambda: environment_maintenance())


def named_agent_turns(gateway: Any) -> bool:
    """True while a named agent (a profile host runtime) is in a turn.

    Those turns run in their own processes and never reach the main agent
    loop's counters (``power_snapshot``); the profile host tracks every run
    its runtimes report until the run's final, aborted or error event.
    """
    host = getattr(gateway, "profile_host", None)
    return bool(host is not None and host.has_active_turns())


def screen_watched(gateway: Any) -> bool:
    """True while someone watches one of this gateway's screens."""
    sessions = getattr(gateway, "_display_sessions", None) or {}
    return any(getattr(session, "streams", None) for session in list(sessions.values()))


def environment_maintenance() -> bool:
    """True while optional dependency maintenance holds the environment (Linux)."""
    import sys

    if sys.platform != "linux":
        return False
    from flowly.desktop_runtime.authority import ScreenError
    from flowly.desktop_runtime.installation_gate import agent_environment_activity

    try:
        with agent_environment_activity():
            return False
    except ScreenError:
        return True


def flush_to_disk(directories: Iterable[Path]) -> None:
    """Checkpoint SQLite write-ahead logs and sync the file system.

    A passive checkpoint never blocks a writer; ``os.sync`` makes the page
    cache durable before the machine is suspended (and maybe never resumed).
    """
    seen: set[Path] = set()
    for directory in directories:
        try:
            files = [p for pattern in ("*.sqlite3", "*.sqlite", "*.db") for p in Path(directory).glob(pattern)]
        except OSError:
            continue
        for path in files:
            wal = path.with_name(path.name + "-wal")
            if path in seen or not wal.exists():
                continue
            seen.add(path)
            try:
                with closing(sqlite3.connect(path, timeout=1)) as connection:
                    connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error as exc:
                logger.debug(f"[power] checkpoint of {path.name} skipped: {exc}")
    if hasattr(os, "sync"):
        os.sync()
