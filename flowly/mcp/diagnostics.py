"""Bounded, credential-safe MCP diagnostics, independent of terminal logging.

One writer thread per retained server owns a fixed-size queue. Disk contention
or unsafe storage drops records instead of blocking MCP or falling back to raw
stderr. Health counters make those losses observable. No global profile handle.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import queue
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from flowly.mcp.security import diagnostic_secrets, exception_diagnostic, safe_diagnostic

LOG_RELATIVE_PATH = "logs/mcp/diagnostics.jsonl"
MAX_LOG_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 20 * 1024
MAX_QUEUED_RECORDS = 64
MAX_RECORDS_PER_WINDOW = 100
LOG_WINDOW_SECONDS = 10.0
LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")
_LOG_NAME = "diagnostics.jsonl"
_active_diagnostics: contextvars.ContextVar[Any] = contextvars.ContextVar("mcp_diagnostics", default=None)
_factory_lock = threading.Lock()


def _install_sdk_log_router():
    """Route only owned SDK/HTTP records; preserve all unrelated logging.

    Logger-parent filters do not apply to descendants, and SDK modules can be
    imported after connection setup. The standard record-factory hook covers
    both without replacing handlers or changing global logger levels. Already
    routed records have no raw message/traceback and a level below NOTSET so
    standard handlers (including the last-resort stderr handler) skip them.
    """
    with _factory_lock:
        previous = logging.getLogRecordFactory()
        if getattr(previous, "_flowly_mcp_router", False):
            return

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            sink = _active_diagnostics.get()
            if sink is not None and record.name.split(".", 1)[0] in {"mcp", "httpx", "httpcore"}:
                try:
                    # SDK debug/info can include complete request/response
                    # bodies or bearer session IDs. Those are never diagnostics.
                    if record.levelno >= logging.WARNING:
                        level = "critical" if record.levelno >= logging.CRITICAL else "error" if record.levelno >= logging.ERROR else "warning"
                        sink.emit(level, record.name, {"message": record.msg, "arguments": record.args},
                                  source="sdk", exception=record.exc_info[1] if record.exc_info else None)
                except Exception:
                    pass  # A logger failure must never fail the MCP operation.
                finally:
                    record.msg, record.args = "", ()
                    record.exc_info = record.exc_text = record.stack_info = None
                    record.levelno, record.levelname = -1, "MCP_ROUTED"
            return record

        factory._flowly_mcp_router = True
        logging.setLogRecordFactory(factory)


@contextmanager
def sdk_log_scope(diagnostics: Any):
    mark = _active_diagnostics.set(diagnostics)
    try:
        yield
    finally:
        _active_diagnostics.reset(mark)


class PrivateLogWriter:
    """Three private files, at most 3 MiB total, shared across processes.

    Directory-relative no-follow descriptors prevent symlink traversal. Secure
    storage primitives are mandatory; unsupported platforms retain counters
    and bounded stderr excerpts but do not persist logs via an unsafe fallback.
    """

    def __init__(self, home: Path, *, max_bytes: int = MAX_LOG_BYTES):
        self.home = Path(home).expanduser().resolve()
        if not 256 <= max_bytes <= MAX_LOG_BYTES:
            raise ValueError("Invalid diagnostic rotation size")
        self.max_bytes = max_bytes

    @contextmanager
    def _directory(self):
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise OSError("Secure diagnostic storage is unavailable")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.home.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in self.home.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            for component in ("logs", "mcp"):
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                info = os.fstat(descriptor)
                forbidden = 0o077 if component == "mcp" else 0o022
                if info.st_uid != os.getuid() or info.st_mode & forbidden:
                    raise OSError("Unsafe diagnostic directory permissions")
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _check(info: os.stat_result) -> None:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise OSError("Unsafe diagnostic file")

    @staticmethod
    def _open(directory: int, name: str) -> int:
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        # Exclusive creation also avoids an APFS O_CREAT/NOFOLLOW race when
        # independent processes open an existing shared file concurrently.
        try:
            return os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        except FileExistsError:
            return os.open(name, flags, dir_fd=directory)

    def append(self, record: bytes) -> bool:
        """Append one already-sanitized JSONL record, or report a dropped write."""
        if not record or len(record) > min(MAX_RECORD_BYTES, self.max_bytes) or not record.endswith(b"\n") or record.count(b"\n") != 1:
            return False
        try:
            import fcntl

            with self._directory() as directory:
                lock = self._open(directory, ".lock")
                try:
                    self._check(os.fstat(lock))
                    if os.fstat(lock).st_size:
                        raise OSError("Invalid diagnostic lock")
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    descriptor = self._open(directory, _LOG_NAME)
                    try:
                        self._check(os.fstat(descriptor))
                        size = os.fstat(descriptor).st_size
                        if size > self.max_bytes:
                            raise OSError("Existing diagnostic file exceeds the size limit")
                        if size and os.pread(descriptor, 1, size - 1) != b"\n":
                            # Recover only a bounded incomplete tail from a
                            # process crash or short disk write. Never join it
                            # to the next otherwise-valid JSON record.
                            start = max(0, size - MAX_RECORD_BYTES)
                            tail = os.pread(descriptor, size - start, start)
                            newline = tail.rfind(b"\n")
                            if newline < 0 and start:
                                raise OSError("Cannot safely repair diagnostic tail")
                            os.ftruncate(descriptor, start + newline + 1)
                        if os.fstat(descriptor).st_size + len(record) > self.max_bytes:
                            # Validate every existing rotation target before
                            # replacing anything. Never follow links or FIFOs.
                            existing = set()
                            for name in (_LOG_NAME, _LOG_NAME + ".1", _LOG_NAME + ".2"):
                                try:
                                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                                    self._check(info)
                                    if info.st_size > self.max_bytes:
                                        raise OSError("Existing diagnostic rotation exceeds the size limit")
                                    existing.add(name)
                                except FileNotFoundError:
                                    pass
                            if _LOG_NAME + ".1" in existing:
                                os.replace(_LOG_NAME + ".1", _LOG_NAME + ".2", src_dir_fd=directory, dst_dir_fd=directory)
                            os.replace(_LOG_NAME, _LOG_NAME + ".1", src_dir_fd=directory, dst_dir_fd=directory)
                            os.close(descriptor)
                            descriptor = -1
                            descriptor = self._open(directory, _LOG_NAME)
                            self._check(os.fstat(descriptor))
                        # Local regular files normally accept one write. Keep
                        # partial-write recovery bounded to this one record.
                        remaining = memoryview(record)
                        while remaining:
                            count = os.write(descriptor, remaining)
                            if count <= 0:
                                raise OSError("Diagnostic write made no progress")
                            remaining = remaining[count:]
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                finally:
                    os.close(lock)  # releases flock on every path
            return True
        except (ImportError, OSError, ValueError, NotImplementedError):
            return False


class MCPDiagnostics:
    """Nonblocking callback sink. Only sanitized bounded strings enter its queue."""

    def __init__(self, name: str, config: dict, home: Path):
        _install_sdk_log_router()
        self._secrets = diagnostic_secrets(config)
        self._name = safe_diagnostic(name, secrets=self._secrets, limit=200)
        logging_config = config.get("logging") or {}
        self.enabled = logging_config.get("enabled", True) is True
        level = logging_config.get("level", "info")
        self.level = level if level in LOG_LEVELS else "info"
        self._writer = PrivateLogWriter(home)
        self._queue: queue.Queue[bytes] = queue.Queue(MAX_QUEUED_RECORDS)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._window = time.monotonic()
        self._window_count = 0
        self._counts = {"received": 0, "written": 0, "dropped": 0, "filtered": 0, "storageErrors": 0}
        self._thread = threading.Thread(target=self._run, name="mcp-diagnostic-writer", daemon=True)
        self._thread.start()

    def _record(self, level: str, name: str, message: str, source: str) -> bytes:
        return (json.dumps({
            "time": time.time(), "server": self._name, "source": source,
            "level": level, "logger": name, "message": message,
        }, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", errors="backslashreplace")

    def emit(
        self, level: str, name: Any, data: Any, *, source: str = "notification", exception: BaseException | None = None,
    ) -> str | None:
        """Return retained sanitized text, or None when filtered/dropped/closed."""
        with self._lock:
            if self._stop.is_set():
                return None
            self._counts["received"] += 1
            level = level if level in LOG_LEVELS else "warning"
            if (source == "notification" and not self.enabled) or (source != "stderr" and LOG_LEVELS.index(level) < LOG_LEVELS.index(self.level)):
                self._counts["filtered"] += 1
                return None
            now = time.monotonic()
            if now - self._window >= LOG_WINDOW_SECONDS:
                self._window, self._window_count = now, 0
            if self._window_count >= MAX_RECORDS_PER_WINDOW or self._queue.full():
                self._counts["dropped"] += 1
                return None
            self._window_count += 1
            if exception is not None:
                data = {"context": data, "exception": exception_diagnostic(exception, secrets=self._secrets)}
            message = safe_diagnostic(data, secrets=self._secrets)
            label = safe_diagnostic(name or "", secrets=self._secrets, limit=200)
            record = self._record(level, label, message, source)
            if len(record) > MAX_RECORD_BYTES:
                self._counts["dropped"] += 1
                return None
            self._queue.put_nowait(record)
            return message

    def _write(self, record: bytes):
        try:
            ok = self._writer.append(record)
        except Exception:
            ok = False  # No raw traceback or stderr fallback from the logger.
        with self._lock:
            self._counts["written" if ok else "storageErrors"] += 1

    def _run(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                record = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._write(record)
            finally:
                self._queue.task_done()
        with self._lock:
            dropped, failed = self._counts["dropped"], self._counts["storageErrors"]
        if dropped or failed:
            self._write(self._record("warning", "flowly", f"Diagnostic records dropped: {dropped}; failed disk writes: {failed}", "diagnostic"))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._counts, "pending": self._queue.qsize(), "level": self.level,
                    "enabled": self.enabled, "path": LOG_RELATIVE_PATH, "closed": self._stop.is_set(),
                    "draining": self._stop.is_set() and self._thread.is_alive()}

    def close(self):
        with self._lock:
            self._stop.set()
        # Disk IO is isolated from the event loop. A stalled filesystem cannot
        # make server teardown wait indefinitely; the daemon still owns its IO.
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)
