"""Bounded, durable lifecycle operations; disconnect never retries a mutation.

Each profile has one active operation and 128 terminal receipts. A lost worker
is reported as recovery-required, never replayed. Generation preconditions also
fence retries whose old receipts have been pruned. Only closed public fields are
returned; process identity and filesystem details remain private.
"""

from __future__ import annotations

import contextvars
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Callable

from filelock import Timeout

from flowly.desktop_runtime.authority import ScreenError, _identity
from flowly.desktop_runtime.handoff import is_alive, process_identity
from flowly.desktop_runtime.launcher import ScreenRuntime

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="screen-lifecycle")
_SLOTS = threading.BoundedSemaphore(2)
_TERMINAL = {"succeeded", "failed"}
_ERRORS = {"STALE_GENERATION", "HUMAN_HAS_CONTROL", "SCREEN_BUSY", "CONTROL_STATE_INVALID",
           "RUNTIME_STATE_INVALID", "RUNTIME_RECOVERY_REQUIRED", "RUNTIME_FAILED",
           "RUNTIME_START_FAILED", "RUNTIME_START_TIMEOUT", "RUNTIME_STOP_FAILED", "RUNTIME_STOP_TIMEOUT",
           "PROFILE_IDENTITY_CHANGED", "PROFILE_NOT_AVAILABLE", "UNSUPPORTED_HOST", "DEPENDENCIES_MISSING",
           "UNPRIVILEGED_RUNTIME_REQUIRED", "LIFECYCLE_BUSY", "CONTROL_UNAVAILABLE",
           "INSUFFICIENT_RESOURCES", "RESOURCE_CHECK_UNAVAILABLE", "INSTALL_BUSY", "INSTALL_STATE_INVALID"}
_FIELDS = {"operationId", "action", "hostId", "botId", "profileId", "generation", "resultGeneration",
           "state", "error", "owner", "deadline"}


def _validate(value: object) -> dict:
    try:
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError()
        for key in ("operationId", "hostId", "botId", "profileId"):
            _identity(value[key])
        for key in ("generation", "resultGeneration"):
            if value[key] is not None:
                _identity(value[key])
        if value["action"] not in {"start", "stop"} or value["state"] not in {"pending", "running", *_TERMINAL}:
            raise ValueError()
        if value["error"] not in {None, *_ERRORS} or (value["state"] == "failed") != (value["error"] is not None):
            raise ValueError()
        if type(value["deadline"]) is not int or not 0 < value["deadline"] < 2**53:
            raise ValueError()
        owner = value["owner"]
        if not isinstance(owner, dict) or set(owner) != {"pid", "start", "boot"} or type(owner["pid"]) is not int or owner["pid"] < 1:
            raise ValueError()
        _identity(owner["start"])
        _identity(owner["boot"])
        return value
    except (TypeError, ValueError, ScreenError) as exc:
        raise ScreenError("LIFECYCLE_STATE_INVALID") from exc


class ScreenLifecycle:
    def __init__(self, runtime: ScreenRuntime, identity: dict, guard: Callable[[], None]):
        self.runtime = runtime
        self.identity = {key: _identity(identity[key]) for key in ("hostId", "botId", "profileId")}
        self.guard = guard
        self.path = runtime.directory / "lifecycle.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)

    @contextmanager
    def _transaction(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS operations (serial INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, value TEXT NOT NULL)")
            yield db
            db.execute("COMMIT")
        except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
            raise ScreenError("LIFECYCLE_STATE_INVALID") from exc
        finally:
            if db is not None:
                db.close()

    def _rows(self, db) -> list[dict]:
        rows = db.execute("SELECT id, value FROM operations ORDER BY serial").fetchall()
        if len(rows) > 128:
            raise ScreenError("LIFECYCLE_STATE_INVALID")
        result = []
        for operation, raw in rows:
            if not isinstance(raw, str) or len(raw) > 4096:
                raise ScreenError("LIFECYCLE_STATE_INVALID")
            value = _validate(json.loads(raw))
            if value["operationId"] != operation:
                raise ScreenError("LIFECYCLE_STATE_INVALID")
            result.append(value)
        return result

    @staticmethod
    def _save(db, value):
        changed = db.execute("UPDATE operations SET value=? WHERE id=?",
                             (json.dumps(_validate(value), separators=(",", ":")), value["operationId"]))
        if changed.rowcount != 1:
            raise ScreenError("LIFECYCLE_STATE_INVALID")

    @staticmethod
    def _public(value: dict) -> dict:
        result = {key: item for key, item in value.items() if key not in {"owner", "deadline"}}
        # This only reports uncertainty; it does not release the active slot or
        # retry a worker whose final publication was lost or delayed.
        if value["state"] not in _TERMINAL and (not is_alive(value["owner"]) or time.time() > value["deadline"]):
            result.update(state="recovery_required", error="LIFECYCLE_RECOVERY_REQUIRED")
        return result

    def status(self, operation: str) -> dict:
        _identity(operation)
        with self._transaction() as db:
            for row in self._rows(db):
                if row["operationId"] == operation:
                    if any(row[key] != value for key, value in self.identity.items()):
                        raise ScreenError("PROFILE_IDENTITY_CHANGED")
                    return self._public(row)
        raise ScreenError("OPERATION_NOT_FOUND")

    def submit(self, action: str, generation: str | None, operation: str) -> dict:
        _identity(operation)
        if action not in {"start", "stop"} or (generation is None and action == "stop"):
            raise ScreenError("INVALID_PARAMS")
        if generation is not None:
            _identity(generation)
        acquired = False
        try:
            with self._transaction() as db:
                rows = self._rows(db)
                for row in rows:
                    if row["operationId"] == operation:
                        if any(row[key] != value for key, value in {**self.identity, "action": action, "generation": generation}.items()):
                            raise ScreenError("OPERATION_CONFLICT")
                        return self._public(row)
                if any(row["state"] not in _TERMINAL for row in rows):
                    raise ScreenError("LIFECYCLE_BUSY")
                owner = process_identity(os.getpid())
                if owner is None:
                    raise ScreenError("LIFECYCLE_RECOVERY_REQUIRED")
                acquired = _SLOTS.acquire(blocking=False)
                if not acquired:
                    raise ScreenError("LIFECYCLE_BUSY")
                record = {**self.identity, "operationId": operation, "action": action, "generation": generation,
                          "resultGeneration": None, "state": "pending", "error": None, "owner": owner,
                          "deadline": int(time.time()) + 120}
                if len(rows) == 128:
                    db.execute("DELETE FROM operations WHERE id=?", (rows[0]["operationId"],))
                db.execute("INSERT INTO operations (id, value) VALUES (?, ?)",
                           (operation, json.dumps(_validate(record), separators=(",", ":"))))
            try:
                _EXECUTOR.submit(contextvars.copy_context().run, self._run, record)
            except RuntimeError:
                record.update(state="failed", error="CONTROL_UNAVAILABLE")
                with self._transaction() as db:
                    self._save(db, record)
                raise ScreenError("CONTROL_UNAVAILABLE") from None
            acquired = False  # The worker owns the slot, even if the RPC disconnects.
            return self._public(record)
        finally:
            if acquired:
                _SLOTS.release()

    def _run(self, accepted: dict) -> None:
        record = dict(accepted)
        try:
            record["state"] = "running"
            with self._transaction() as db:
                self._save(db, record)
            self.guard()
            if record["action"] == "start":
                state = self.runtime.start(expected_generation=record["generation"], identity_guard=self.guard)
                record["resultGeneration"] = state["generation"]
            else:
                self.runtime.stop(record["generation"], identity_guard=self.guard)
                record["resultGeneration"] = record["generation"]
            record["state"] = "succeeded"
        except BaseException as exc:
            code = exc.code if isinstance(exc, ScreenError) and exc.code in _ERRORS else "CONTROL_UNAVAILABLE"
            if isinstance(exc, Timeout):
                code = "LIFECYCLE_BUSY"
            record.update(state="failed", error=code)
        finally:
            try:
                with self._transaction() as db:
                    self._save(db, record)
            finally:
                _SLOTS.release()
