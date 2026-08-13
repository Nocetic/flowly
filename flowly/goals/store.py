"""Crash-safe, cross-process persistence for session goals."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from filelock import FileLock, Timeout

from flowly.goals.models import GoalState


class GoalStoreError(RuntimeError):
    pass


class GoalStoreConflictError(GoalStoreError):
    """The caller evaluated an obsolete goal generation or revision."""


class GoalStoreCorruptError(GoalStoreError):
    pass


class GoalStoreLockTimeoutError(GoalStoreError):
    pass


T = TypeVar("T")


def _record_name(session_key: str) -> str:
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()


class GoalStore:
    """One durable record per logical Flowly session.

    Every read-modify-write transaction holds an advisory file lock shared by
    CLI and gateway processes. Writes use fsync + atomic replace. Revisions are
    monotonic and optional generation/revision preconditions prevent an old
    judge result or queued continuation from reviving superseded state.
    """

    def __init__(self, root: Path, *, lock_timeout: float = 30.0):
        self.root = Path(root) / "goals"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_timeout = max(0.1, float(lock_timeout))

    def _paths(self, session_key: str) -> tuple[Path, Path]:
        name = _record_name(session_key)
        return self.root / f"{name}.lock", self.root / f"{name}.json"

    @contextmanager
    def _locked(self, session_key: str) -> Iterator[Path]:
        lock_path, state_path = self._paths(session_key)
        lock = FileLock(str(lock_path), timeout=self.lock_timeout)
        try:
            with lock:
                yield state_path
        except Timeout as exc:
            raise GoalStoreLockTimeoutError(
                f"timed out acquiring goal state lock for {session_key!r}"
            ) from exc

    @staticmethod
    def _read(path: Path) -> GoalState | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GoalStoreError(f"could not read goal state: {exc}") from exc
        try:
            value = json.loads(raw)
            return GoalState.from_dict(value)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GoalStoreCorruptError(f"invalid goal state at {path}: {exc}") from exc

    @staticmethod
    def _write(path: Path, state: GoalState) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        payload = (
            json.dumps(
                state.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
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
        except OSError as exc:
            raise GoalStoreError(f"could not persist goal state: {exc}") from exc
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _check_preconditions(
        state: GoalState | None,
        *,
        expected_goal_id: str | None,
        expected_revision: int | None,
    ) -> None:
        if expected_goal_id is not None and (state is None or state.goal_id != expected_goal_id):
            raise GoalStoreConflictError("goal generation changed")
        if expected_revision is not None and (state is None or state.revision != expected_revision):
            raise GoalStoreConflictError("goal revision changed")

    def get(self, session_key: str) -> GoalState | None:
        with self._locked(session_key) as path:
            state = self._read(path)
            return GoalState.from_dict(state.to_dict()) if state else None

    def save(
        self,
        state: GoalState,
        *,
        expected_goal_id: str | None = None,
        expected_revision: int | None = None,
    ) -> GoalState:
        with self._locked(state.session_key) as path:
            current = self._read(path)
            self._check_preconditions(
                current,
                expected_goal_id=expected_goal_id,
                expected_revision=expected_revision,
            )
            saved = GoalState.from_dict(state.to_dict())
            saved.revision = (current.revision + 1) if current else 1
            saved.updated_at = time.time()
            self._write(path, saved)
            return GoalState.from_dict(saved.to_dict())

    def update(
        self,
        session_key: str,
        mutation: Callable[[GoalState | None], GoalState],
        *,
        expected_goal_id: str | None = None,
        expected_revision: int | None = None,
    ) -> GoalState:
        """Atomically mutate one record and return the committed snapshot."""
        with self._locked(session_key) as path:
            current = self._read(path)
            self._check_preconditions(
                current,
                expected_goal_id=expected_goal_id,
                expected_revision=expected_revision,
            )
            working = GoalState.from_dict(current.to_dict()) if current else None
            updated = mutation(working)
            if updated.session_key != session_key:
                raise ValueError("goal mutation changed session_key")
            updated.revision = (current.revision + 1) if current else 1
            updated.updated_at = time.time()
            self._write(path, updated)
            return GoalState.from_dict(updated.to_dict())

    def compare_and_update(
        self,
        snapshot: GoalState,
        mutation: Callable[[GoalState], GoalState],
    ) -> GoalState:
        def checked(current: GoalState | None) -> GoalState:
            if current is None:  # preconditions normally catch this
                raise GoalStoreConflictError("goal no longer exists")
            return mutation(current)

        return self.update(
            snapshot.session_key,
            checked,
            expected_goal_id=snapshot.goal_id,
            expected_revision=snapshot.revision,
        )
