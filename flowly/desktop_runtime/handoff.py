"""Durable origin-run checkpoints, separate from human input ownership.

This store authorizes only the original live execution to refresh its browser
observation. It never releases control, restarts a task, or replays tool calls.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError, _identity
from flowly.desktop_runtime.launcher import private_directory
from flowly.desktop_runtime.launcher import process_identity as _process_identity

_STATES = {"ready", "waiting_human", "waiting_resume", "resume_requested", "refreshing", "finished", "cancelled"}
_TERMINAL = {"finished", "cancelled"}
_KEYS = {"schema", "hostId", "botId", "profileId", "originSessionKey", "originRunId", "runtimeGeneration",
         "executionId", "owner", "acceptedEpoch", "controlEpoch", "handoffId", "requestedEpoch", "state", "reason"}


def process_identity(pid: int) -> dict | None:
    # An agent can be container PID 1. This is read-only run ownership, not
    # permission to signal init; runtime process signalling keeps its guard.
    return _process_identity(pid, allow_init=True)


def is_alive(owner: dict) -> bool:
    return isinstance(owner, dict) and process_identity(owner.get("pid")) == owner


def _epoch(value) -> int:
    if type(value) is not int or not 0 <= value < 2**53:
        raise ScreenError("INVALID_PARAMS")
    return value


def _validate(value: object) -> dict:
    try:
        if not isinstance(value, dict) or set(value) != _KEYS or value["state"] not in _STATES:
            raise ValueError()
        if type(value["schema"]) is not int or value["schema"] != 1:
            raise ValueError()
        for key in ("hostId", "botId", "profileId", "originSessionKey", "originRunId", "runtimeGeneration", "executionId"):
            _identity(value[key])
        for key in ("acceptedEpoch", "controlEpoch"):
            _epoch(value[key])
        if value["handoffId"] is not None:
            _identity(value["handoffId"])
        if value["reason"] not in {None, "human_control"}:
            raise ValueError()
        if value["requestedEpoch"] is not None:
            _epoch(value["requestedEpoch"])
        owner = value["owner"]
        if not isinstance(owner, dict) or set(owner) != {"pid", "start", "boot"}:
            raise ValueError()
        if type(owner["pid"]) is not int or owner["pid"] < 1:
            raise ValueError()
        _identity(owner["start"])
        _identity(owner["boot"])
        if value["state"] in {"resume_requested", "refreshing"} and value["requestedEpoch"] is None:
            raise ValueError()
        if value["state"] not in {"ready", *_TERMINAL} and value["handoffId"] is None:
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError, ScreenError) as exc:
        raise ScreenError("HANDOFF_STATE_INVALID") from exc


class RunHandoffs:
    """One active screen run, with a bounded cross-reconnect resume receipt log."""

    def __init__(self, directory: Path, authority: ScreenAuthority):
        if os.name == "posix":
            private_directory(directory)
        else:
            directory.mkdir(parents=True, exist_ok=True)
        self.authority = authority
        self.path = directory / "handoff.sqlite3"
        self.marker = directory / ".handoff-initialized"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS current_run (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            connection.execute("""CREATE TABLE IF NOT EXISTS receipts (
                serial INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT UNIQUE NOT NULL,
                handoff TEXT NOT NULL, epoch INTEGER NOT NULL)""")
            row = connection.execute("SELECT value FROM current_run WHERE id=1").fetchone()
            if row is None and self.marker.exists():
                raise ScreenError("HANDOFF_STATE_INVALID")
            value = _validate(json.loads(row[0])) if row is not None and isinstance(row[0], str) and len(row[0]) <= 8192 else None
            if row is not None and value is None:
                raise ScreenError("HANDOFF_STATE_INVALID")
            slot = [value]
            yield connection, slot
            if slot[0] is not None:
                encoded = json.dumps(_validate(slot[0]), separators=(",", ":"))
                if len(encoded) > 8192:
                    raise ScreenError("HANDOFF_STATE_INVALID")
                if row is None or row[0] != encoded:
                    connection.execute("INSERT OR REPLACE INTO current_run VALUES (1, ?)", (encoded,))
            connection.execute("COMMIT")
            if slot[0] is not None and not self.marker.exists():
                fd = os.open(self.marker, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ScreenError("HANDOFF_STATE_INVALID") from exc
        except (sqlite3.Error, OSError) as exc:
            raise ScreenError("CONTROL_UNAVAILABLE") from exc
        finally:
            if connection is not None:
                connection.close()

    def start(self, identity: dict, session_key: str, run_id: str, generation: str) -> str:
        if not isinstance(identity, dict) or set(identity) != {"hostId", "botId", "profileId"}:
            raise ScreenError("INVALID_PARAMS")
        for value in (*identity.values(), session_key, run_id, generation):
            _identity(value)
        owner = process_identity(os.getpid())
        if owner is None:
            raise ScreenError("HANDOFF_RECOVERY_REQUIRED")
        with self._transaction() as (_, slot):
            if slot[0] is not None and slot[0]["state"] not in _TERMINAL:
                raise ScreenError("SCREEN_BUSY")
            control = self.authority.snapshot()
            if control["generation"] != generation or not control["running"]:
                raise ScreenError("STALE_GENERATION")
            if control["runId"] != run_id:
                raise ScreenError("RUN_CHANGED")
            if control["control"] != "agent" or control["actionInFlight"] or control["inputDrainRequired"]:
                raise ScreenError("SCREEN_BUSY")
            execution = secrets.token_urlsafe(24)
            slot[0] = {"schema": 1, **identity, "originSessionKey": session_key, "originRunId": run_id,
                       "runtimeGeneration": generation, "owner": owner, "executionId": execution,
                       "acceptedEpoch": control["epoch"], "controlEpoch": control["epoch"],
                       "handoffId": None, "requestedEpoch": None, "state": "ready", "reason": None}
        return execution

    @staticmethod
    def _execution(value: dict | None, execution: str, *, original_process: bool = False):
        _identity(execution)
        if value is None or not secrets.compare_digest(value["executionId"], execution):
            raise ScreenError("RUN_CHANGED")
        if value["state"] in _TERMINAL:
            raise ScreenError("RUN_ENDED")
        if not is_alive(value["owner"]) or (original_process and process_identity(os.getpid()) != value["owner"]):
            raise ScreenError("HANDOFF_RECOVERY_REQUIRED")

    def _reconcile(self, value: dict) -> dict:
        control = self.authority.snapshot()
        if not control["running"] or control["generation"] != value["runtimeGeneration"]:
            raise ScreenError("HANDOFF_RECOVERY_REQUIRED")
        if control["runId"] != value["originRunId"]:
            raise ScreenError("RUN_CHANGED")
        value["controlEpoch"] = control["epoch"]
        same_resume = (value["state"] in {"resume_requested", "refreshing"}
                       and value["requestedEpoch"] == control["epoch"] and control["control"] == "agent")
        if not same_resume and (control["control"] != "agent" or control["epoch"] != value["acceptedEpoch"]):
            if value["state"] in {"ready", "refreshing"}:
                value["handoffId"] = secrets.token_urlsafe(24)
            value["state"] = "waiting_human" if control["control"] != "agent" else "waiting_resume"
            value["requestedEpoch"] = None
            value["reason"] = "human_control"
        return control

    @staticmethod
    def _public(value: dict) -> dict:
        return {key: value[key] for key in ("hostId", "botId", "profileId", "originSessionKey", "originRunId",
                                            "runtimeGeneration", "controlEpoch", "handoffId", "state", "reason")}

    def checkpoint(self, execution: str) -> dict:
        with self._transaction() as (_, slot):
            value = slot[0]
            self._execution(value, execution, original_process=True)
            self._reconcile(value)
            result = self._public(value)
        return result

    def status(self) -> dict | None:
        with self._transaction() as (_, slot):
            value = slot[0]
            if value is None:
                return None
            if value["state"] not in _TERMINAL:
                try:
                    self._execution(value, value["executionId"])
                    self._reconcile(value)
                except ScreenError as exc:
                    if exc.code not in {"HANDOFF_RECOVERY_REQUIRED", "RUN_CHANGED"}:
                        raise
                    return {**self._public(value), "state": "recovery_required"}
            return self._public(value)

    def request_resume(self, handoff_id: str, epoch: int, operation_id: str) -> dict:
        _identity(handoff_id)
        _identity(operation_id)
        _epoch(epoch)
        with self._transaction() as (db, slot):
            prior = db.execute("SELECT handoff, epoch FROM receipts WHERE operation=?", (operation_id,)).fetchone()
            if prior is not None:
                if prior != (handoff_id, epoch):
                    raise ScreenError("OPERATION_CONFLICT")
                # A historical ACK has no authority to resume a new checkpoint.
                return {"handoffId": handoff_id, "controlEpoch": epoch, "requested": True}
            value = slot[0]
            if value is None:
                raise ScreenError("RUN_CHANGED")
            self._execution(value, value["executionId"])
            control = self._reconcile(value)
            if value["handoffId"] != handoff_id:
                raise ScreenError("HANDOFF_CHANGED")
            if control["epoch"] != epoch:
                raise ScreenError("STALE_EPOCH")
            if control["control"] != "agent":
                raise ScreenError("HUMAN_HAS_CONTROL")
            if control["actionInFlight"] or control["inputDrainRequired"]:
                raise ScreenError("SCREEN_BUSY")
            if value["state"] != "waiting_resume":
                raise ScreenError("RESUME_NOT_AVAILABLE")
            value.update(state="resume_requested", requestedEpoch=epoch)
            db.execute("INSERT INTO receipts(operation, handoff, epoch) VALUES (?, ?, ?)", (operation_id, handoff_id, epoch))
            db.execute("DELETE FROM receipts WHERE serial NOT IN (SELECT serial FROM receipts ORDER BY serial DESC LIMIT 128)")
        return {"handoffId": handoff_id, "controlEpoch": epoch, "requested": True}

    def begin_refresh(self, execution: str) -> int | None:
        with self._transaction() as (_, slot):
            value = slot[0]
            self._execution(value, execution, original_process=True)
            control = self._reconcile(value)
            if value["state"] != "resume_requested":
                return None
            if control["actionInFlight"] or control["inputDrainRequired"]:
                raise ScreenError("SCREEN_BUSY")
            value["state"] = "refreshing"
            return control["epoch"]

    def complete_refresh(self, execution: str, epoch: int) -> None:
        _epoch(epoch)
        with self._transaction() as (_, slot):
            value = slot[0]
            self._execution(value, execution, original_process=True)
            control = self._reconcile(value)
            if value["state"] != "refreshing" or value["requestedEpoch"] != epoch or control["epoch"] != epoch:
                raise ScreenError("STALE_OBSERVATION")
            if control["actionInFlight"] or control["inputDrainRequired"]:
                raise ScreenError("SCREEN_BUSY")
            value.update(state="ready", acceptedEpoch=epoch, requestedEpoch=None)

    def finish(self, execution: str, *, cancelled: bool) -> None:
        with self._transaction() as (_, slot):
            value = slot[0]
            _identity(execution)
            if value is None or not secrets.compare_digest(value["executionId"], execution):
                raise ScreenError("RUN_CHANGED")
            if value["state"] not in _TERMINAL:
                value.update(state="cancelled" if cancelled else "finished", requestedEpoch=None)
