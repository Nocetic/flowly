"""Cross-process coordination for destructive context compaction.

The in-process ``asyncio.Lock`` in :mod:`flowly.compaction.service` prevents
two coroutines from rewriting one session together, but it cannot see a CLI,
gateway, or worker running in another process.  This module adds the two small
pieces of durable state compaction needs without turning the local JSONL store
into a distributed database:

* an advisory OS lock held for the whole summarise/commit cycle; and
* a durable circuit-breaker record updated under its own short-lived lock.

``flock`` is the authority for lease ownership.  The JSON lease record is an
audit/fencing record, never a reason to steal a live lock.  The kernel releases
the lock if its process exits, so a crash cannot strand the conversation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

from flowly.utils.helpers import ensure_dir

try:  # POSIX is the production target; the fallback remains process-safe.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]


class CompactionLeaseTimeoutError(RuntimeError):
    """Another process held a session's compaction lease for too long."""


@dataclass(frozen=True)
class CompactionLease:
    """Ownership proof passed to the destructive commit boundary."""

    session_key: str
    fence: int
    owner: str
    acquired_at: float


@dataclass(frozen=True)
class BreakerState:
    """The restart-safe part of one session's compaction state."""

    consecutive_failures: int = 0
    checks_since_suppression: int = 0


def _safe_state_name(session_key: str) -> str:
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()


class CompactionCoordinator:
    """File-backed leases and breaker records scoped to one agent state dir."""

    LEASE_AUDIT_TTL_SECONDS = 300.0
    LEASE_WAIT_TIMEOUT_SECONDS = 300.0
    LEASE_POLL_SECONDS = 0.05

    def __init__(self, state_dir: Path):
        root = ensure_dir(Path(state_dir) / "compaction-coordination")
        self._lease_dir = ensure_dir(root / "leases")
        self._breaker_dir = ensure_dir(root / "breakers")

    def _paths(self, session_key: str, *, breaker: bool = False) -> tuple[Path, Path]:
        root = self._breaker_dir if breaker else self._lease_dir
        name = _safe_state_name(session_key)
        return root / f"{name}.lock", root / f"{name}.json"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        """Replace a tiny state record atomically and durably enough to recover."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _try_lock(handle: Any) -> bool:
        if fcntl is None:
            return True
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    @staticmethod
    def _unlock(handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @asynccontextmanager
    async def lease(
        self,
        session_key: str,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[CompactionLease]:
        """Acquire a crash-released process lease and mint its fence token."""
        lock_path, state_path = self._paths(session_key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
        deadline = time.monotonic() + (
            self.LEASE_WAIT_TIMEOUT_SECONDS if timeout is None else max(0.0, timeout)
        )
        try:
            while not self._try_lock(handle):
                if time.monotonic() >= deadline:
                    raise CompactionLeaseTimeoutError(
                        f"compaction lease timed out for {session_key}"
                    )
                await asyncio.sleep(self.LEASE_POLL_SECONDS)

            previous = self._read_json(state_path)
            try:
                fence = max(0, int(previous.get("fence", 0))) + 1
            except (TypeError, ValueError):
                fence = 1
            now = time.time()
            owner = f"{os.getpid()}-{secrets.token_hex(8)}"
            self._write_json(
                state_path,
                {
                    "session_key": session_key,
                    "fence": fence,
                    "owner": owner,
                    "acquired_at": now,
                    "audit_expires_at": now + self.LEASE_AUDIT_TTL_SECONDS,
                    "released_at": None,
                },
            )
            lease = CompactionLease(session_key, fence, owner, now)
            try:
                yield lease
            finally:
                current = self._read_json(state_path)
                if current.get("owner") == owner:
                    current["released_at"] = time.time()
                    try:
                        self._write_json(state_path, current)
                    except OSError:
                        # This is an audit timestamp, not lease authority.  A
                        # disk-full error after the canonical commit must not
                        # turn a successful compaction into an apparent failure.
                        pass
        finally:
            try:
                self._unlock(handle)
            finally:
                handle.close()

    @contextmanager
    def _breaker_lock(self, session_key: str) -> Iterator[Path]:
        lock_path, state_path = self._paths(session_key, breaker=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield state_path
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _breaker_from_json(value: dict[str, Any]) -> BreakerState:
        try:
            failures = max(0, int(value.get("consecutive_failures", 0)))
        except (TypeError, ValueError):
            failures = 0
        try:
            checks = max(0, int(value.get("checks_since_suppression", 0)))
        except (TypeError, ValueError):
            checks = 0
        return BreakerState(failures, checks)

    def read_breaker(self, session_key: str) -> BreakerState:
        with self._breaker_lock(session_key) as state_path:
            return self._breaker_from_json(self._read_json(state_path))

    def update_breaker(
        self,
        session_key: str,
        update: Callable[[BreakerState], BreakerState],
    ) -> BreakerState:
        """Read-modify-write the breaker without losing another process's update."""
        with self._breaker_lock(session_key) as state_path:
            state = update(self._breaker_from_json(self._read_json(state_path)))
            clean = BreakerState(
                max(0, int(state.consecutive_failures)),
                max(0, int(state.checks_since_suppression)),
            )
            self._write_json(
                state_path,
                {
                    "session_key": session_key,
                    "consecutive_failures": clean.consecutive_failures,
                    "checks_since_suppression": clean.checks_since_suppression,
                    "updated_at": time.time(),
                },
            )
            return clean

    def clear_breaker(self, session_key: str) -> None:
        with self._breaker_lock(session_key) as state_path:
            try:
                state_path.unlink(missing_ok=True)
            except OSError:
                # Resetting an in-memory conversation must not fail because a
                # diagnostic backoff file could not be removed.
                pass
