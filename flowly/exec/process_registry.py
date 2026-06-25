"""Background process registry for long-running shell tasks.

Spawns subprocesses, tracks them by handle ID, captures stdout/stderr in a
rolling buffer, watches for patterns, and surfaces completion to the agent
via the message bus as a synthetic system message.

Native asyncio: every spawned process gets one reader Task that reads chunks,
buffers them, scans for watch patterns, and on EOF awaits process.wait() to
record the exit code. No threads, no queue.Queue.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from flowly.bus.events import InboundMessage

if TYPE_CHECKING:
    from flowly.bus.queue import MessageBus
    from flowly.exec.process_checkpoint import ProcessCheckpoint


MAX_OUTPUT_CHARS = 200_000
FINISHED_TTL_SECONDS = 1800
MAX_PROCESSES = 64
READ_CHUNK_SIZE = 4096
COMPLETION_OUTPUT_TAIL_CHARS = 2000
POLL_OUTPUT_PREVIEW_CHARS = 1000

WATCH_MIN_INTERVAL_SECONDS = 15
WATCH_STRIKE_LIMIT = 3
WATCH_GLOBAL_MAX_PER_WINDOW = 15
WATCH_GLOBAL_WINDOW_SECONDS = 10
WATCH_GLOBAL_COOLDOWN_SECONDS = 30

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@dataclass
class ProcessSession:
    id: str
    command: str
    session_key: str | None
    started_at: float
    cwd: str | None = None
    pid: int | None = None
    process: "asyncio.subprocess.Process | None" = None
    exited: bool = False
    exit_code: int | None = None
    output_buffer: str = ""
    notify_on_complete: bool = True
    watch_patterns: list[str] = field(default_factory=list)
    _watch_last_emit_at: float = 0.0
    _watch_consecutive_strikes: int = 0
    _watch_disabled: bool = False
    _watch_suppressed: int = 0
    _reader_task: "asyncio.Task | None" = None
    _exit_event: asyncio.Event = field(default_factory=asyncio.Event)
    _moved_to_finished: bool = False
    _finished_at: float = 0.0
    _completion_consumed: bool = False
    detached: bool = False

    def status(self) -> str:
        return "exited" if self.exited else "running"

    def uptime_seconds(self) -> float:
        end = self._finished_at if self.exited else time.time()
        return max(0.0, end - self.started_at)

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "status": self.status(),
            "pid": self.pid,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "uptime_seconds": round(self.uptime_seconds(), 2),
            "session_key": self.session_key,
        }


class ProcessRegistry:
    """In-memory registry of background subprocesses.

    Per-loop singleton — get instance via ``get_registry()`` after binding the
    bus once at gateway/CLI startup with ``bind_bus()``.
    """

    def __init__(self, bus: "MessageBus | None" = None):
        self._bus: "MessageBus | None" = bus
        self._running: dict[str, ProcessSession] = {}
        self._finished: dict[str, ProcessSession] = {}
        self._lock = asyncio.Lock()
        self._global_watch_emits: list[float] = []
        self._global_watch_cooldown_until: float = 0.0
        self._checkpoint: "ProcessCheckpoint | None" = None

    def bind_bus(self, bus: "MessageBus") -> None:
        self._bus = bus

    def bind_checkpoint(self, checkpoint: "ProcessCheckpoint") -> None:
        """Enable disk-backed restart-survive. Recovery is the caller's job
        (use ``recover_detached()`` after binding)."""
        self._checkpoint = checkpoint

    async def recover_detached(self) -> int:
        """Load checkpoint, probe PIDs, register survivors as detached sessions.

        Returns the count recovered. Detached sessions have no process handle
        and no reader — poll returns running-without-output, kill uses raw
        os.kill, wait hangs forever. Once Flowly restarts again the process
        will be re-checkpointed.
        """
        if self._checkpoint is None:
            return 0
        recovered = self._checkpoint.recover()
        async with self._lock:
            for s in recovered:
                s.detached = True
                s._exit_event = asyncio.Event()
                self._running[s.id] = s
        return len(recovered)

    def _persist(self) -> None:
        if self._checkpoint is None:
            return
        try:
            sessions = list(self._running.values()) + list(self._finished.values())
            self._checkpoint.save(sessions)
        except Exception as e:
            logger.warning(f"[ProcessRegistry] checkpoint save failed: {e}")

    async def spawn(
        self,
        command: str,
        *,
        session_key: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        watch_patterns: list[str] | None = None,
        notify_on_complete: bool = True,
    ) -> ProcessSession:
        async with self._lock:
            await self._prune_locked()
            if len(self._running) + len(self._finished) >= MAX_PROCESSES:
                raise RuntimeError(
                    f"Process registry full ({MAX_PROCESSES}) — kill or wait for old ones"
                )

        session_id = "proc_" + secrets.token_hex(6)
        session = ProcessSession(
            id=session_id,
            command=command,
            session_key=session_key,
            started_at=time.time(),
            cwd=cwd,
            notify_on_complete=notify_on_complete,
            watch_patterns=list(watch_patterns or []),
        )

        from flowly.utils.subprocess_compat import hide_window_kwargs
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
            start_new_session=True,   # POSIX: setsid (detach). Ignored on Windows.
            # Windows: CREATE_NO_WINDOW only — NO detach/process-group: this is an
            # asyncio + captured-stdio spawn, where DETACHED_PROCESS would sever
            # capture and CREATE_NEW_PROCESS_GROUP cancels the ProactorEventLoop
            # task on Win/Py3.11. {} on POSIX (byte-identical to before).
            **hide_window_kwargs(),
        )
        session.process = proc
        session.pid = proc.pid

        async with self._lock:
            self._running[session_id] = session

        session._reader_task = asyncio.create_task(
            self._reader_loop(session),
            name=f"process-reader:{session_id}",
        )
        self._persist()

        logger.info(f"[ProcessRegistry] spawned {session_id} pid={proc.pid} cmd={command[:60]!r}")
        return session

    async def get(self, session_id: str) -> ProcessSession | None:
        async with self._lock:
            return self._running.get(session_id) or self._finished.get(session_id)

    async def list_sessions(self, session_key: str | None = None) -> list[dict]:
        async with self._lock:
            await self._prune_locked()
            all_sessions = list(self._running.values()) + list(self._finished.values())
        if session_key is not None:
            all_sessions = [s for s in all_sessions if s.session_key == session_key]
        all_sessions.sort(key=lambda s: s.started_at, reverse=True)
        return [s.to_summary() for s in all_sessions]

    async def poll(self, session_id: str) -> dict:
        session = await self.get(session_id)
        if not session:
            return {"status": "not_found", "session_id": session_id}
        # Detached sessions (recovered post-restart) have no reader, so we
        # probe liveness on demand.
        if session.detached and not session.exited and session.pid:
            try:
                os.kill(session.pid, 0)
            except ProcessLookupError:
                session.exited = True
                session.exit_code = None
                session._finished_at = time.time()
                session._exit_event.set()
                await self._move_to_finished(session)
        preview = session.output_buffer[-POLL_OUTPUT_PREVIEW_CHARS:]
        if session.exited:
            session._completion_consumed = True
        result = {
            "status": session.status(),
            "session_id": session.id,
            "command": session.command,
            "pid": session.pid,
            "exit_code": session.exit_code,
            "uptime_seconds": round(session.uptime_seconds(), 2),
            "output_preview": preview,
            "buffer_chars": len(session.output_buffer),
        }
        if session.detached:
            result["detached"] = True
            result["note"] = (
                "This session was recovered after a Flowly restart. Output history "
                "before the restart is not available; exit code may be unknown."
            )
        return result

    async def read_log(
        self,
        session_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict:
        session = await self.get(session_id)
        if not session:
            return {"status": "not_found", "session_id": session_id}
        lines = session.output_buffer.splitlines()
        total_lines = len(lines)
        if offset < 0:
            offset = max(0, total_lines + offset)
        end = total_lines if limit is None else min(total_lines, offset + limit)
        chunk = "\n".join(lines[offset:end])
        if session.exited:
            session._completion_consumed = True
        return {
            "status": session.status(),
            "session_id": session.id,
            "exit_code": session.exit_code,
            "total_lines": total_lines,
            "showing": [offset, end],
            "output": chunk,
        }

    async def wait(self, session_id: str, timeout: float | None = None) -> dict:
        session = await self.get(session_id)
        if not session:
            return {"status": "not_found", "session_id": session_id}
        if session.exited:
            session._completion_consumed = True
            return self._exit_payload(session)
        try:
            await asyncio.wait_for(session._exit_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "session_id": session.id,
                "uptime_seconds": round(session.uptime_seconds(), 2),
                "output_preview": session.output_buffer[-POLL_OUTPUT_PREVIEW_CHARS:],
            }
        session._completion_consumed = True
        return self._exit_payload(session)

    async def kill(self, session_id: str) -> dict:
        session = await self.get(session_id)
        if not session:
            return {"status": "not_found", "session_id": session_id}
        if session.exited:
            return {"status": "already_exited", "session_id": session.id, "exit_code": session.exit_code}
        proc = session.process
        if proc is None:
            # Detached (recovered after restart) — no Process handle, kill by PID.
            if session.detached and session.pid:
                try:
                    os.kill(session.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    return {"status": "error", "session_id": session.id, "error": str(e)}
                session.exited = True
                session.exit_code = -signal.SIGTERM
                session._finished_at = time.time()
                session._exit_event.set()
                await self._move_to_finished(session)
                return {"status": "killed", "session_id": session.id, "exit_code": session.exit_code}
            return {"status": "error", "session_id": session.id, "error": "no process handle"}
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"[ProcessRegistry] terminate failed for {session_id}: {e}")
            try:
                proc.kill()
            except Exception:
                return {"status": "error", "session_id": session.id, "error": str(e)}
        try:
            await asyncio.wait_for(session._exit_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(session._exit_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        return {"status": "killed", "session_id": session.id, "exit_code": session.exit_code}

    async def kill_all(self, session_key: str | None = None) -> int:
        async with self._lock:
            targets = [
                s for s in self._running.values()
                if session_key is None or s.session_key == session_key
            ]
        count = 0
        for session in targets:
            res = await self.kill(session.id)
            if res.get("status") in ("killed", "already_exited"):
                count += 1
        return count

    def _exit_payload(self, session: ProcessSession) -> dict:
        return {
            "status": "exited",
            "session_id": session.id,
            "exit_code": session.exit_code,
            "uptime_seconds": round(session.uptime_seconds(), 2),
            "output_tail": session.output_buffer[-COMPLETION_OUTPUT_TAIL_CHARS:],
        }

    async def _reader_loop(self, session: ProcessSession) -> None:
        proc = session.process
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                chunk = await proc.stdout.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                session.output_buffer += text
                if len(session.output_buffer) > MAX_OUTPUT_CHARS:
                    session.output_buffer = session.output_buffer[-MAX_OUTPUT_CHARS:]
                if session.watch_patterns and not session._watch_disabled:
                    await self._check_watch_patterns(session, text)
        except Exception as e:
            logger.warning(f"[ProcessRegistry] reader error for {session.id}: {e}")
        finally:
            try:
                rc = await proc.wait()
            except Exception:
                rc = None
            session.exit_code = rc
            session.exited = True
            session._finished_at = time.time()
            session._exit_event.set()
            await self._move_to_finished(session)
            if session.notify_on_complete:
                await self._emit_completion(session)

    async def _move_to_finished(self, session: ProcessSession) -> None:
        async with self._lock:
            if session._moved_to_finished:
                return
            session._moved_to_finished = True
            self._running.pop(session.id, None)
            self._finished[session.id] = session
        self._persist()

    async def _prune_locked(self) -> None:
        now = time.time()
        expired = [
            sid for sid, s in self._finished.items()
            if now - s._finished_at > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            self._finished.pop(sid, None)
        if len(self._running) + len(self._finished) >= MAX_PROCESSES and self._finished:
            oldest = sorted(self._finished.values(), key=lambda s: s._finished_at)
            for s in oldest[: max(1, len(self._finished) // 4)]:
                self._finished.pop(s.id, None)

    async def _check_watch_patterns(self, session: ProcessSession, new_text: str) -> None:
        matched = None
        for pat in session.watch_patterns:
            if pat and pat in new_text:
                matched = pat
                break
        if matched is None:
            return

        now = time.time()
        if now < self._global_watch_cooldown_until:
            session._watch_suppressed += 1
            return
        self._global_watch_emits = [
            t for t in self._global_watch_emits
            if now - t <= WATCH_GLOBAL_WINDOW_SECONDS
        ]
        if len(self._global_watch_emits) >= WATCH_GLOBAL_MAX_PER_WINDOW:
            self._global_watch_cooldown_until = now + WATCH_GLOBAL_COOLDOWN_SECONDS
            session._watch_suppressed += 1
            logger.warning(
                f"[ProcessRegistry] global watch breaker tripped — "
                f"cooling down {WATCH_GLOBAL_COOLDOWN_SECONDS}s"
            )
            return

        if now - session._watch_last_emit_at < WATCH_MIN_INTERVAL_SECONDS:
            session._watch_consecutive_strikes += 1
            session._watch_suppressed += 1
            if session._watch_consecutive_strikes >= WATCH_STRIKE_LIMIT:
                session._watch_disabled = True
                logger.info(
                    f"[ProcessRegistry] watch disabled for {session.id} after "
                    f"{WATCH_STRIKE_LIMIT} strikes — falling back to completion-only"
                )
            return

        session._watch_last_emit_at = now
        session._watch_consecutive_strikes = 0
        self._global_watch_emits.append(now)
        await self._emit_watch_match(session, matched, new_text)

    async def _emit_watch_match(self, session: ProcessSession, pattern: str, snippet: str) -> None:
        if self._bus is None or session.session_key is None:
            return
        tail = _strip_ansi(snippet)[-COMPLETION_OUTPUT_TAIL_CHARS:]
        content = (
            f"[Process {session.id} matched pattern {pattern!r}]\n"
            f"Last output:\n{tail}"
        )
        try:
            channel, chat_id = _split_session_key(session.session_key)
            msg = InboundMessage(
                channel=channel,
                sender_id="process",
                chat_id=chat_id,
                content=content,
                timestamp=datetime.now(),
                metadata={
                    "kind": "process_watch_match",
                    "session_id": session.id,
                    "pattern": pattern,
                    "command": session.command,
                },
            )
            await self._bus.publish_inbound(msg)
        except Exception as e:
            logger.warning(f"[ProcessRegistry] watch emit failed for {session.id}: {e}")

    async def _emit_completion(self, session: ProcessSession) -> None:
        if self._bus is None or session.session_key is None:
            return
        tail = _strip_ansi(session.output_buffer)[-COMPLETION_OUTPUT_TAIL_CHARS:]
        verdict = "succeeded" if session.exit_code == 0 else "failed"
        content = (
            f"[Process {session.id} {verdict} (exit_code={session.exit_code}, "
            f"command={session.command!r})]\n"
            f"Last output:\n{tail}"
        )
        try:
            channel, chat_id = _split_session_key(session.session_key)
            msg = InboundMessage(
                channel=channel,
                sender_id="process",
                chat_id=chat_id,
                content=content,
                timestamp=datetime.now(),
                metadata={
                    "kind": "process_completion",
                    "session_id": session.id,
                    "exit_code": session.exit_code,
                    "command": session.command,
                },
            )
            await self._bus.publish_inbound(msg)
        except Exception as e:
            logger.warning(f"[ProcessRegistry] completion emit failed for {session.id}: {e}")


def _split_session_key(session_key: str) -> tuple[str, str]:
    if ":" in session_key:
        channel, chat_id = session_key.split(":", 1)
        return channel, chat_id
    return "system", session_key


_registry_singleton: ProcessRegistry | None = None


def get_registry() -> ProcessRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = ProcessRegistry()
    return _registry_singleton


def reset_registry() -> None:
    """Test helper — drop the singleton."""
    global _registry_singleton
    _registry_singleton = None
