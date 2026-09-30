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
from flowly.live_voice.authority import current_request_owner
from flowly.session.control_access import SessionControlScope
from flowly.session.manager import session_file_lock
from flowly.session.ownership import SessionAccessError, is_owned_session, require_session_file


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
        self.sessions_dir = Path(root) / 'sessions'

    def _paths(self, session_key: str) -> tuple[Path, Path]:
        name = _record_name(session_key)
        return self.root / f"{name}.lock", self.root / f"{name}.json"

    @contextmanager
    def _session_guard(self, session_key: str) -> Iterator[None]:
        scope = SessionControlScope.bind(session_key, None, sessions_dir=self.sessions_dir)
        with session_file_lock(scope.path):
            require_session_file(scope.path, session_key)
            yield

    @contextmanager
    def control_guard(self, session_key: str) -> Iterator[None]:
        """Keep synchronous runtime effects within the goal's owner check."""
        with self._session_guard(session_key):
            self.get(session_key)
            yield

    @contextmanager
    def _locked(self, session_key: str) -> Iterator[Path]:
        lock_path, state_path = self._paths(session_key)
        lock = FileLock(str(lock_path), timeout=self.lock_timeout)
        try:
            # Same lock order as session writes: canonical authority first.
            with self._session_guard(session_key), lock:
                yield state_path
        except Timeout as exc:
            raise GoalStoreLockTimeoutError(
                f"timed out acquiring goal state lock for {session_key!r}"
            ) from exc

    def _read(self, path: Path) -> GoalState | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise GoalStoreError(f"could not read goal state: {exc}") from exc
        try:
            value = json.loads(raw)
            state = GoalState.from_dict(value)
            state._session_control_scope = self._record_scope(state.session_key, value)
            return state
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GoalStoreCorruptError(f"invalid goal state at {path}: {exc}") from exc

    @staticmethod
    def _write(path: Path, state: GoalState) -> None:
        GoalStore._write_json(path, {**state.to_dict(), 'sessionOwner': state._session_control_scope.owner})

    def _record_scope(self, key: str, record: dict) -> SessionControlScope:
        # Legacy private namespaces belong to the host. Never assign a missing
        # binding to whichever account now owns the canonical conversation.
        owner = record.get('sessionOwner', {'kind': 'host'} if is_owned_session(key, {}) else None)
        if owner is None and is_owned_session(key, {}):
            raise SessionAccessError()
        return SessionControlScope.bind(key, owner, sessions_dir=self.sessions_dir)

    @staticmethod
    def _require_scope(scope: SessionControlScope, key: str) -> None:
        if scope.key != key:
            raise SessionAccessError()
        with scope.guard(key) as allowed:
            if not allowed:
                raise SessionAccessError()

    def _read_current(self, path: Path, key: str) -> GoalState | None:
        try:
            state = self._read(path)
        except GoalStoreError:
            if current_request_owner() is not None:
                raise SessionAccessError() from None
            raise
        if state is not None:
            if state.session_key != key:
                raise GoalStoreCorruptError('goal record has a different conversation')
            self._require_scope(state._session_control_scope, key)
        return state

    @staticmethod
    def _clone(state: GoalState) -> GoalState:
        result = GoalState.from_dict(state.to_dict())
        result._session_control_scope = state._session_control_scope
        return result

    def _bind_saved(self, state: GoalState, current: GoalState | None) -> None:
        if state._session_control_scope is not None:
            self._require_scope(state._session_control_scope, state.session_key)
        scope = (current._session_control_scope if current else state._session_control_scope)
        if scope is None:
            scope = SessionControlScope.capture(state.session_key, sessions_dir=self.sessions_dir)
        self._require_scope(scope, state.session_key)
        state._session_control_scope = scope

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        payload = (
            json.dumps(
                value,
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
            state = self._read_current(path, session_key)
            return self._clone(state) if state else None

    @staticmethod
    def _generation_proof(state: GoalState) -> dict:
        return {'goalId': state.goal_id, 'revision': state.revision,
                'status': state.status.value, 'lastRunId': state.last_run_id,
                'createdByRunId': state.created_by_run_id}

    def _generation_path(self, session_key: str, goal_id: str) -> Path:
        identity = hashlib.sha256(json.dumps([session_key, goal_id]).encode()).hexdigest()
        return self.root / 'history' / f'{identity}.json'

    def _archive_replaced(self, current: GoalState | None, updated: GoalState) -> None:
        if current is None or current.goal_id == updated.goal_id:
            return
        path = self._generation_path(current.session_key, current.goal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Persist the already committed generation before replacing the current
        # record. A failed archive cannot silently destroy completion evidence.
        self._write_json(path, {'sessionKey': current.session_key,
                               'sessionOwner': current._session_control_scope.owner,
                               **self._generation_proof(current)})

    def get_generation(self, session_key: str, goal_id: str) -> dict | None:
        """Read current or replaced generation proof without retaining goal text."""
        with self._locked(session_key) as path:
            current = self._read_current(path, session_key)
            if current is not None and current.goal_id == goal_id:
                if current.session_key != session_key:
                    raise GoalStoreCorruptError('goal generation has a different conversation')
                return self._generation_proof(current)
            try:
                proof = json.loads(self._generation_path(session_key, goal_id).read_text())
            except FileNotFoundError:
                return None
            except (OSError, ValueError) as exc:
                raise GoalStoreCorruptError('goal generation proof is unavailable') from exc
            if (not isinstance(proof, dict) or proof.get('sessionKey') != session_key
                    or proof.get('goalId') != goal_id
                    or proof.get('status') not in {'active', 'paused', 'done', 'cleared'}
                    or type(proof.get('revision')) is not int or proof['revision'] < 1
                    or any(proof.get(key) is not None and not isinstance(proof[key], str)
                           for key in ('lastRunId', 'createdByRunId'))):
                raise GoalStoreCorruptError('invalid goal generation proof')
            self._require_scope(self._record_scope(session_key, proof), session_key)
            return {key: proof.get(key) for key in ('goalId', 'revision', 'status', 'lastRunId', 'createdByRunId')}

    def iter_states(self) -> "list[GoalState]":
        """Every goal on disk, newest first.

        Used to re-arm the runtime after a restart: goal state is durable but
        the work queue is not, so a process that comes back has to find the
        goals it owes work to. Unreadable files are skipped rather than
        failing the sweep — one corrupt goal must not strand the others.
        """
        states: list[GoalState] = []
        try:
            paths = sorted(self.root.glob("*.json"))
        except OSError:
            return states
        for path in paths:
            # The atomic discovery read finds a key, not an authorized result.
            # Re-read under canonical/session locks before returning a state.
            try:
                state = self._read(path)
                if state is not None and self._paths(state.session_key)[1] == path:
                    state = self.get(state.session_key)
                else:
                    continue
            except Exception:  # noqa: BLE001 — one bad record must not strand the rest
                continue
            if state is not None and state.session_key:
                states.append(state)
        states.sort(key=lambda item: item.updated_at, reverse=True)
        return states

    def save(
        self,
        state: GoalState,
        *,
        expected_goal_id: str | None = None,
        expected_revision: int | None = None,
    ) -> GoalState:
        with self._locked(state.session_key) as path:
            current = self._read_current(path, state.session_key)
            saved = self._clone(state)
            self._bind_saved(saved, current)
            self._check_preconditions(
                current,
                expected_goal_id=expected_goal_id,
                expected_revision=expected_revision,
            )
            saved.revision = (current.revision + 1) if current else 1
            saved.updated_at = time.time()
            self._archive_replaced(current, saved)
            self._write(path, saved)
            return self._clone(saved)

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
            current = self._read_current(path, session_key)
            self._check_preconditions(
                current,
                expected_goal_id=expected_goal_id,
                expected_revision=expected_revision,
            )
            working = self._clone(current) if current else None
            updated = mutation(working)
            if updated.session_key != session_key:
                raise ValueError("goal mutation changed session_key")
            self._bind_saved(updated, current)
            updated.revision = (current.revision + 1) if current else 1
            updated.updated_at = time.time()
            self._archive_replaced(current, updated)
            self._write(path, updated)
            return self._clone(updated)

    def compare_and_update(
        self,
        snapshot: GoalState,
        mutation: Callable[[GoalState], GoalState],
    ) -> GoalState:
        def checked(current: GoalState | None) -> GoalState:
            if current is None:  # preconditions normally catch this
                raise GoalStoreConflictError("goal no longer exists")
            return mutation(current)

        with self._session_guard(snapshot.session_key):
            if snapshot._session_control_scope is not None:
                self._require_scope(snapshot._session_control_scope, snapshot.session_key)
            return self.update(
                snapshot.session_key,
                checked,
                expected_goal_id=snapshot.goal_id,
                expected_revision=snapshot.revision,
            )
