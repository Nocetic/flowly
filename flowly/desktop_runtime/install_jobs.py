"""Bounded installation orchestration, independent of a requesting connection.

Only an authenticated admission layer may call submit. Recipes and interpreter
paths are always host-resolved. Secrets live only in the worker call, never in
receipts. Gateway loss requires recovery; accepted mutations are never replayed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Callable

from flowly.desktop_runtime.authority import ScreenError, _identity
from flowly.desktop_runtime.handoff import is_alive, process_identity
from flowly.desktop_runtime.install_plan import installation_plan
from flowly.desktop_runtime.installer import _operation, install_status, install_system
from flowly.desktop_runtime.python_install_worker import private_directory, state_root
from flowly.desktop_runtime.python_installer import install_python

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screen-install")
_SLOTS = threading.BoundedSemaphore(1)
_TERMINAL = {"succeeded", "failed"}
_ERRORS = {"INSTALL_BUSY", "INSTALL_STATE_INVALID", "INSTALL_PRIVILEGE_REQUIRED", "INSTALL_PLAN_CHANGED",
           "INSTALL_RECOVERY_REQUIRED", "INSTALL_HISTORY_FULL", "INSTALL_INDEX_FAILED", "INSTALL_PACKAGES_FAILED",
           "INSTALL_PYTHON_FAILED", "INSTALL_VERIFICATION_FAILED", "INSTALL_TIMEOUT", "INSTALL_UV_REQUIRED",
           "INSTALL_VIRTUALENV_REQUIRED", "INSTALL_UNSUPPORTED_HOST", "INSTALL_TOOLS_MISSING",
           "INSTALL_MANAGED_BUILD_REQUIRED", "INSTALL_RUNTIME_ACCOUNT_REQUIRED",
           "PROFILE_IDENTITY_CHANGED", "PROFILE_NOT_AVAILABLE", "CONTROL_UNAVAILABLE"}
_FIELDS = {"installJobVersion", "hostId", "botId", "profileId", "screenId", "operationId", "planId",
           "state", "phase", "completedSteps", "error", "restartRequired", "owner", "deadline"}


def _validate(value):
    try:
        if not isinstance(value, dict) or set(value) != _FIELDS or type(value["installJobVersion"]) is not int or value["installJobVersion"] != 1:
            raise ValueError()
        for key in ("hostId", "botId", "profileId"):
            _identity(value[key])
        _operation(value["operationId"])
        if (value["screenId"] != "browser" or not isinstance(value["planId"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["planId"])
                or value["state"] not in ("pending", "running", "succeeded", "failed", "recovery_required")
                or value["phase"] not in ("pending", "system", "python", "complete")
                or value["completedSteps"] not in ([], ["system"], ["system", "python"])
                or value["error"] not in (None, *_ERRORS)
                or ((value["state"] in ("failed", "recovery_required")) != (value["error"] is not None))
                or ((value["state"] == "succeeded") != (value["phase"] == "complete"))
                or (value["state"] == "succeeded" and value["completedSteps"] != ["system", "python"])
                or type(value["restartRequired"]) is not bool
                or type(value["deadline"]) is not int or not 0 < value["deadline"] < 2**53):
            raise ValueError()
        owner = value["owner"]
        if (not isinstance(owner, dict) or set(owner) != {"pid", "start", "boot"}
                or type(owner["pid"]) is not int or owner["pid"] < 1):
            raise ValueError()
        _identity(owner["start"])
        _identity(owner["boot"])
        return value
    except (TypeError, ValueError, ScreenError):
        raise ScreenError("INSTALL_STATE_INVALID") from None


class ScreenInstallJobs:
    def __init__(self, identity: dict, guard: Callable[[], None]):
        if os.name != "posix":
            raise ScreenError("INSTALL_UNSUPPORTED_HOST")
        self.identity = {key: _identity(identity[key]) for key in ("hostId", "botId", "profileId")}
        self.identity["screenId"] = "browser"
        self.guard = guard
        self.root = state_root()
        self.directory = self.root / "jobs"
        self.path = self.directory / "operations.sqlite3"

    @contextmanager
    def _transaction(self, *, create=False):
        try:
            private_directory(self.root, create=create)
            private_directory(self.directory, create=create)
            fd = os.open(self.path, os.O_NOFOLLOW | (os.O_CREAT | os.O_RDWR if create else os.O_RDONLY), 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise ValueError()
            finally:
                os.close(fd)
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rwc" if create else "?mode=ro"), uri=True, timeout=2)
            try:
                if create:
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    db.execute("BEGIN IMMEDIATE")
                yield db
                if create:
                    db.commit()
            finally:
                db.close()
        except FileNotFoundError:
            raise ScreenError("OPERATION_NOT_FOUND") from None
        except sqlite3.OperationalError as exc:
            code = "INSTALL_BUSY" if "locked" in str(exc) else "INSTALL_STATE_INVALID"
            raise ScreenError(code) from None
        except (OSError, ValueError, sqlite3.DatabaseError):
            raise ScreenError("INSTALL_STATE_INVALID") from None

    @staticmethod
    def _rows(db):
        rows = db.execute("SELECT id, value FROM operations LIMIT 129").fetchall()
        if len(rows) > 128:
            raise ScreenError("INSTALL_STATE_INVALID")
        result = []
        for identifier, raw in rows:
            if not isinstance(raw, str) or len(raw) > 4096:
                raise ScreenError("INSTALL_STATE_INVALID")
            try:
                value = _validate(json.loads(raw))
            except ValueError:
                raise ScreenError("INSTALL_STATE_INVALID") from None
            if identifier != value["operationId"]:
                raise ScreenError("INSTALL_STATE_INVALID")
            result.append(value)
        return result

    def _save(self, value):
        with self._transaction(create=True) as db:
            changed = db.execute("UPDATE operations SET value=? WHERE id=?",
                                 (json.dumps(_validate(value)), value["operationId"]))
            if changed.rowcount != 1:
                raise ScreenError("INSTALL_STATE_INVALID")

    @staticmethod
    def _public(value):
        result = {key: item for key, item in value.items() if key not in {"owner", "deadline"}}
        if value["state"] in {"pending", "running"} and (not is_alive(value["owner"]) or time.time() > value["deadline"]):
            result.update(state="recovery_required", error="INSTALL_RECOVERY_REQUIRED")
        return result

    def status(self, operation: str):
        _operation(operation)
        with self._transaction() as db:
            for row in self._rows(db):
                if row["operationId"] == operation:
                    if any(row[key] != value for key, value in self.identity.items()):
                        raise ScreenError("PROFILE_IDENTITY_CHANGED")
                    return self._public(row)
        raise ScreenError("OPERATION_NOT_FOUND")

    def submit(self, plan_id: str, operation: str, *, password: str | None = None):
        _operation(operation)
        if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{64}", plan_id):
            raise ScreenError("INVALID_PARAMS")
        if password is not None:
            try:
                if (not isinstance(password, str) or len(password) > 4096 or len(password.encode()) > 4096
                        or any(char in password for char in "\r\n\x00")):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise ScreenError("INVALID_PARAMS") from None
        try:
            previous = self.status(operation)
        except ScreenError as exc:
            if exc.code != "OPERATION_NOT_FOUND":
                raise
        else:
            if previous["planId"] != plan_id:
                raise ScreenError("OPERATION_CONFLICT")
            return previous
        plan = installation_plan(self.identity)
        if plan["planId"] != plan_id:
            raise ScreenError("INSTALL_PLAN_CHANGED")
        for step in ("system", "python"):
            if not plan[step]["supported"]:
                raise ScreenError(plan[step]["reason"])
        if plan["runtimeAccountRequired"]:
            raise ScreenError("INSTALL_RUNTIME_ACCOUNT_REQUIRED")
        self.guard()
        acquired = False
        try:
            with self._transaction(create=True) as db:
                rows = self._rows(db)
                for row in rows:
                    if row["operationId"] == operation:
                        if row["planId"] != plan_id or any(row[key] != value for key, value in self.identity.items()):
                            raise ScreenError("OPERATION_CONFLICT")
                        return self._public(row)
                if any(row["state"] not in _TERMINAL for row in rows):
                    raise ScreenError("INSTALL_BUSY")
                if len(rows) >= 128:
                    raise ScreenError("INSTALL_HISTORY_FULL")
                owner = process_identity(os.getpid())
                if owner is None:
                    raise ScreenError("INSTALL_RECOVERY_REQUIRED")
                acquired = _SLOTS.acquire(blocking=False)
                if not acquired:
                    raise ScreenError("INSTALL_BUSY")
                record = {"installJobVersion": 1, **self.identity, "operationId": operation, "planId": plan_id,
                          "state": "pending", "phase": "pending", "completedSteps": [], "error": None,
                          "restartRequired": False, "owner": owner, "deadline": int(time.time()) + 1260}
                db.execute("INSERT INTO operations (id, value) VALUES (?, ?)", (operation, json.dumps(_validate(record))))
            try:
                _EXECUTOR.submit(self._run, record, plan, password)
            except RuntimeError:
                record.update(state="failed", error="CONTROL_UNAVAILABLE")
                self._save(record)
                raise ScreenError("CONTROL_UNAVAILABLE") from None
            acquired = False
            return self._public(record)
        finally:
            password = None
            if acquired:
                _SLOTS.release()

    def _run(self, accepted, plan, password):
        record = {**accepted, "completedSteps": []}
        attempted = False
        try:
            if time.time() > record["deadline"]:
                raise ScreenError("INSTALL_TIMEOUT")
            self.guard()
            record.update(state="running", phase="system")
            self._save(record)
            attempted = True
            try:
                result = install_system(plan["system"]["planId"], record["operationId"], password=password)
            finally:
                password = None
            if result["state"] != "succeeded":
                raise ScreenError(result["error"] or "INSTALL_RECOVERY_REQUIRED")
            record["completedSteps"] = ["system"]
            self._save(record)
            self.guard()
            if time.time() > record["deadline"]:
                raise ScreenError("INSTALL_TIMEOUT")
            record["phase"] = "python"
            # A lost worker after this point may have changed imported packages.
            record["restartRequired"] = True
            self._save(record)
            result = install_python(plan["python"]["planId"], record["operationId"])
            record["restartRequired"] = result["restartRequired"]
            if result["state"] != "succeeded":
                raise ScreenError(result["error"] or "INSTALL_RECOVERY_REQUIRED")
            record.update(state="succeeded", phase="complete", completedSteps=["system", "python"])
        except BaseException as exc:
            code = exc.code if isinstance(exc, ScreenError) and exc.code in _ERRORS else "INSTALL_RECOVERY_REQUIRED"
            # Privilege rejection before any package step is safe to retry with
            # a new operation. All uncertain/partial mutations require recovery.
            safe = not attempted or (record["phase"] == "system" and code in {"INSTALL_BUSY", "INSTALL_PLAN_CHANGED"})
            if record["phase"] == "system" and code == "INSTALL_PRIVILEGE_REQUIRED":
                # The wrapper also uses this code for malformed worker output.
                # Only a definitely absent accepted receipt permits a new try.
                try:
                    install_status(record["operationId"])
                except ScreenError as status_error:
                    safe = status_error.code == "OPERATION_NOT_FOUND"
            record.update(state="failed" if safe else "recovery_required", error=code)
        finally:
            password = None
            try:
                self._save(record)
            finally:
                _SLOTS.release()
