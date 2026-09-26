"""Durable admission control for one profile's browser screen.

All processes use the same file lock. No timer or disconnected viewer grants
control back to an agent. This fences cooperating tools, not arbitrary code
running as the same OS user. Viewer capabilities must already be authenticated
by the transport before calling this internal API.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout


class ScreenError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ActionAdmission:
    generation: str
    epoch: int
    run_id: str
    action_id: str
    nonce: str


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ScreenError("INVALID_PARAMS")
    if any(ord(character) < 32 for character in value):
        raise ScreenError("INVALID_PARAMS")
    return value


def _fresh() -> dict:
    return dict(schema=1, generation=None, epoch=0, revision=0, running=False,
                control="agent", viewer=None, run=None, action=None)


def _validate(state: object) -> dict:
    try:
        if not isinstance(state, dict) or set(state) != set(_fresh()):
            raise ValueError()
        if type(state["schema"]) is not int or state["schema"] != 1:
            raise ValueError()
        for key in ("epoch", "revision"):
            if type(state[key]) is not int or not 0 <= state[key] < 2**53:
                raise ValueError()
        if type(state["running"]) is not bool:
            raise ValueError()
        if state["control"] not in ("agent", "human-pending", "human"):
            raise ValueError()
        for key in ("generation", "viewer", "run"):
            if state[key] is not None:
                _identity(state[key])
        if (state["control"] == "agent") != (state["viewer"] is None):
            raise ValueError()
        if state["running"] and state["generation"] is None:
            raise ValueError()
        action = state["action"]
        if action is not None:
            if not isinstance(action, dict) or set(action) != {"id", "nonce", "epoch"}:
                raise ValueError()
            _identity(action["id"])
            _identity(action["nonce"])
            if type(action["epoch"]) is not int or not 0 <= action["epoch"] <= state["epoch"]:
                raise ValueError()
            if not state["running"] or state["run"] is None or state["control"] == "human":
                raise ValueError()
        if not state["running"] and (state["control"] != "agent" or state["run"] is not None):
            raise ValueError()
        return state
    except (KeyError, TypeError, ValueError, ScreenError) as exc:
        raise ScreenError("CONTROL_STATE_INVALID") from exc


def _public(state: dict) -> dict:
    viewer = state["viewer"]
    return {
        "generation": state["generation"], "epoch": state["epoch"],
        "revision": state["revision"], "running": state["running"],
        "control": state["control"], "runId": state["run"],
        "actionInFlight": state["action"] is not None,
        "holderHash": hashlib.sha256(viewer.encode()).hexdigest() if viewer else None,
    }


class ScreenAuthority:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self._path = self.directory / "control.json"
        self._marker = self.directory / ".initialized"
        self._lock = FileLock(self.directory / "control.lock", timeout=5, mode=0o600)

    def _read(self) -> dict:
        try:
            fd = os.open(self._path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                raw = source.read(8193)
            if len(raw) > 8192:
                raise ValueError()
            return _validate(json.loads(raw))
        except FileNotFoundError:
            if self._marker.exists():
                raise ScreenError("CONTROL_STATE_INVALID") from None
            return _fresh()
        except (OSError, ValueError) as exc:
            raise ScreenError("CONTROL_STATE_INVALID") from exc

    def _write(self, state: dict) -> None:
        _validate(state)
        temporary = self.directory / (".control-" + secrets.token_hex(12))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as target:
                json.dump(state, target, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self._path)
            marker_fd = os.open(self._marker, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(marker_fd)
            if os.name == "posix":
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _transaction(self) -> Iterator[dict]:
        try:
            with self._lock:
                state = self._read()
                before = json.dumps(state, sort_keys=True)
                yield state
                if before != json.dumps(state, sort_keys=True):
                    state["revision"] += 1
                    self._write(state)
        except Timeout as exc:
            raise ScreenError("CONTROL_UNAVAILABLE") from exc
        except OSError as exc:
            raise ScreenError("CONTROL_UNAVAILABLE") from exc

    @staticmethod
    def _check(state: dict, generation: str, epoch: int | None = None) -> None:
        _identity(generation)
        if generation != state["generation"]:
            raise ScreenError("STALE_GENERATION")
        if epoch is not None:
            if type(epoch) is not int or epoch < 0:
                raise ScreenError("INVALID_PARAMS")
            if epoch != state["epoch"]:
                raise ScreenError("STALE_EPOCH")
        if not state["running"]:
            raise ScreenError("SCREEN_NOT_RUNNING")

    def snapshot(self) -> dict:
        with self._transaction() as state:
            pass
        return _public(state)

    def start(self, generation: str) -> dict:
        _identity(generation)
        with self._transaction() as state:
            if state["generation"] == generation and state["running"]:
                pass
            elif state["control"] != "agent":
                raise ScreenError("HUMAN_HAS_CONTROL")
            elif state["run"] is not None or state["action"] is not None:
                raise ScreenError("SCREEN_BUSY")
            elif state["generation"] == generation:
                raise ScreenError("STALE_GENERATION")
            else:
                state.update(generation=generation, running=True, epoch=state["epoch"] + 1)
        return _public(state)

    def stop(self, generation: str) -> dict:
        with self._transaction() as state:
            self._check(state, generation)
            if state["control"] != "agent":
                raise ScreenError("HUMAN_HAS_CONTROL")
            if state["run"] is not None:
                raise ScreenError("SCREEN_BUSY")
            state.update(running=False, epoch=state["epoch"] + 1)
        return _public(state)

    def begin_action(self, run_id: str, action_id: str, generation: str) -> ActionAdmission:
        _identity(run_id)
        _identity(action_id)
        with self._transaction() as state:
            self._check(state, generation)
            if state["control"] != "agent":
                raise ScreenError("HUMAN_HAS_CONTROL")
            if state["run"] not in (None, run_id) or state["action"] is not None:
                raise ScreenError("SCREEN_BUSY")
            nonce = secrets.token_urlsafe(24)
            state.update(run=run_id, action=dict(id=action_id, nonce=nonce, epoch=state["epoch"]))
            admission = ActionAdmission(generation, state["epoch"], run_id, action_id, nonce)
        return admission

    def end_action(self, admission: ActionAdmission) -> bool:
        """Release an exact admission and say whether its output may be used.

        The caller must only finish after the actual backend operation has
        stopped, including its child tasks. A cancelled await is not proof.
        """
        with self._transaction() as state:
            expected = dict(id=admission.action_id, nonce=admission.nonce, epoch=admission.epoch)
            if (state["generation"] != admission.generation or state["run"] != admission.run_id
                    or state["action"] != expected):
                return False
            valid = state["control"] == "agent" and state["epoch"] == admission.epoch
            state["action"] = None
        return valid

    def finish_run(self, run_id: str, generation: str) -> None:
        _identity(run_id)
        with self._transaction() as state:
            self._check(state, generation)
            if state["run"] not in (None, run_id) or state["action"] is not None:
                raise ScreenError("SCREEN_BUSY")
            state["run"] = None

    def acquire(self, viewer: str, generation: str, epoch: int, *, reclaim: bool = False) -> dict:
        _identity(viewer)
        with self._transaction() as state:
            self._check(state, generation, epoch)
            if state["control"] != "agent" and state["viewer"] != viewer and not reclaim:
                raise ScreenError("SCREEN_BUSY")
            if state["viewer"] != viewer:
                state.update(viewer=viewer, epoch=state["epoch"] + 1,
                             control="human-pending" if state["action"] is not None else "human")
        return _public(state)

    def confirm_acquire(self, viewer: str, generation: str, epoch: int) -> dict:
        _identity(viewer)
        with self._transaction() as state:
            self._check(state, generation, epoch)
            if state["viewer"] != viewer:
                raise ScreenError("VIEWER_MISMATCH")
            if state["action"] is not None:
                raise ScreenError("TAKEOVER_PENDING")
            state["control"] = "human"
        return _public(state)

    def release(self, viewer: str, generation: str, epoch: int) -> dict:
        _identity(viewer)
        with self._transaction() as state:
            self._check(state, generation, epoch)
            if state["viewer"] != viewer:
                raise ScreenError("VIEWER_MISMATCH")
            state.update(viewer=None, control="agent", epoch=state["epoch"] + 1)
        return _public(state)

    def may_input(self, viewer: str, generation: str, epoch: int) -> bool:
        try:
            _identity(viewer)
            with self._transaction() as state:
                self._check(state, generation, epoch)
                return state["control"] == "human" and state["viewer"] == viewer
        except ScreenError:
            return False
