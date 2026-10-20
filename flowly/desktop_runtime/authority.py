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


@dataclass(frozen=True)
class InputAdmission:
    generation: str
    epoch: int
    nonce: str


def _identity(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ScreenError("INVALID_PARAMS")
    if any(ord(character) < 32 for character in value):
        raise ScreenError("INVALID_PARAMS")
    return value


# The control record's schema. A screen's own services (input, observers) read
# and write this record too, from the code the screen was started with; the
# screen records the schema it speaks (``controlProtocol`` in its manifest) and
# a screen speaking another one is restarted, never shared (launcher.py).
SCHEMA = 4


def _fresh() -> dict:
    return dict(schema=SCHEMA, generation=None, epoch=0, revision=0, running=False,
                control="agent", viewer=None, run=None, runOwner=None, action=None, input=None)


def _owner() -> dict | None:
    """The process admitting an action, so a dead one's admission can be voided."""
    from flowly.desktop_runtime.launcher import process_identity

    return process_identity(os.getpid(), allow_init=True)


def _alive(owner: dict | None) -> bool:
    """Whether the process that admitted an action still exists.

    No owner means the action predates owners (a version-two record). On a
    host that can identify processes, the gateway that admitted it restarted
    to read this code, so it is gone: such an action is void (a stuck one from
    before the fence kept a screen locked across an update). Only a host with
    no process identities at all (no /proc) takes it as alive.
    """
    from flowly.desktop_runtime.launcher import process_identity

    if owner is None:
        return process_identity(os.getpid(), allow_init=True) is None
    return process_identity(owner["pid"], allow_init=True) == owner


def _void_dead_claims(state: dict) -> None:
    """Claims of a process that no longer exists have ended.

    An action: its backend connection died with the process, so nothing it
    queued can reach the browser. Without this a gateway restart mid-command
    locked the screen (takeover pending, every run busy) until a stop and start.

    A run: the task that reserved the browser is gone with its process, and
    nothing else would ever finish it. Without this a run whose cleanup failed
    (2026-10-20, 01:53 on the test VPS) kept the screen busy for 18 hours: no
    idle stop, no explicit stop, every later task refused.
    """
    action = state["action"]
    if action is not None and not _alive(action["owner"]):
        state["action"] = None
    if state["run"] is not None and state["action"] is None and not _alive(state["runOwner"]):
        state.update(run=None, runOwner=None)


def _owner_shape(owner: object) -> bool:
    return owner is None or (isinstance(owner, dict) and set(owner) == {"pid", "start", "boot"}
                             and type(owner["pid"]) is int and owner["pid"] >= 1
                             and isinstance(owner["start"], str) and isinstance(owner["boot"], str))


def _validate(state: object) -> dict:
    try:
        # Version one never admitted input connections. Upgrade only its exact
        # schema; a damaged version-two document must not lose its input fence.
        if (isinstance(state, dict) and type(state.get("schema")) is int and state["schema"] == 1
                and set(state) == set(_fresh()) - {"input", "runOwner"}):
            state = {**state, "schema": 2, "input": None}
        # Version two's actions had no owner (see _alive for what that means).
        if (isinstance(state, dict) and type(state.get("schema")) is int and state["schema"] == 2
                and set(state) == set(_fresh()) - {"runOwner"}):
            action = state["action"]
            state = {**state, "schema": 3,
                     "action": {**action, "owner": None} if isinstance(action, dict) and "owner" not in action else action}
        # Version three's runs had no owner, the same way.
        if (isinstance(state, dict) and type(state.get("schema")) is int and state["schema"] == 3
                and set(state) == set(_fresh()) - {"runOwner"}):
            state = {**state, "schema": 4, "runOwner": None}
        if not isinstance(state, dict) or set(state) != set(_fresh()):
            raise ValueError()
        if type(state["schema"]) is not int or state["schema"] != SCHEMA:
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
        if not _owner_shape(state["runOwner"]) or (state["run"] is None and state["runOwner"] is not None):
            raise ValueError()
        action = state["action"]
        if action is not None:
            if not isinstance(action, dict) or set(action) != {"id", "nonce", "epoch", "owner"}:
                raise ValueError()
            _identity(action["id"])
            _identity(action["nonce"])
            if not _owner_shape(action["owner"]):
                raise ValueError()
            if type(action["epoch"]) is not int or not 0 <= action["epoch"] <= state["epoch"]:
                raise ValueError()
            if not state["running"] or state["run"] is None or state["control"] == "human":
                raise ValueError()
        if not state["running"] and (state["control"] != "agent" or state["run"] is not None):
            raise ValueError()
        if state["input"] is not None:
            entry = state["input"]
            if not isinstance(entry, dict) or set(entry) != {"nonce", "epoch"}:
                raise ValueError()
            _identity(entry["nonce"])
            if type(entry["epoch"]) is not int or entry["epoch"] != state["epoch"]:
                raise ValueError()
            if not state["running"] or state["control"] != "human":
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
        "inputDrainRequired": state["input"] is not None,
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
            _void_dead_claims(state)
        return _public(state)

    def start(self, generation: str) -> dict:
        _identity(generation)
        with self._transaction() as state:
            _void_dead_claims(state)
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

    def stop(self, generation: str, *, idle_revision: int | None = None) -> dict:
        """``idle_revision``: an idle stop, refused if anything changed since then."""
        with self._transaction() as state:
            self._check(state, generation)
            _void_dead_claims(state)
            if state["control"] != "agent":
                raise ScreenError("HUMAN_HAS_CONTROL")
            if state["run"] is not None:
                raise ScreenError("SCREEN_BUSY")
            if idle_revision is not None and state["revision"] != idle_revision:
                raise ScreenError("SCREEN_BUSY")
            state.update(running=False, epoch=state["epoch"] + 1)
        return _public(state)

    def stop_dead(self, generation: str) -> dict:
        """Finish a generation whose every process the caller verified dead.

        Human control, input, action and run claims protected a live screen
        that no longer exists (crash, host reboot); keeping them would lock
        the profile out forever, since release needs a running screen. Only
        an explicit stop under the lifecycle lock calls this, never a viewer
        disconnect. Anything still tied to this generation is fenced as stale.
        """
        with self._transaction() as state:
            self._check(state, generation)
            state.update(running=False, control="agent", viewer=None, run=None, runOwner=None, action=None,
                         input=None, epoch=state["epoch"] + 1)
        return _public(state)

    def claim_run(self, run_id: str, generation: str) -> dict:
        """Reserve the browser before the first model/tool step of a screen run."""
        _identity(run_id)
        with self._transaction() as state:
            self._check(state, generation)
            _void_dead_claims(state)
            if state["control"] != "agent":
                raise ScreenError("HUMAN_HAS_CONTROL")
            if state["run"] is not None or state["action"] is not None:
                raise ScreenError("SCREEN_BUSY")
            state.update(run=run_id, runOwner=_owner())
        return _public(state)

    def begin_action(self, run_id: str, action_id: str, generation: str, *,
                     expected_epoch: int | None = None) -> ActionAdmission:
        _identity(run_id)
        _identity(action_id)
        with self._transaction() as state:
            self._check(state, generation, expected_epoch)
            _void_dead_claims(state)
            if state["control"] != "agent":
                raise ScreenError("HUMAN_HAS_CONTROL")
            if state["run"] not in (None, run_id) or state["action"] is not None:
                raise ScreenError("SCREEN_BUSY")
            nonce = secrets.token_urlsafe(24)
            owner = _owner()
            if state["run"] is None:
                state["runOwner"] = owner
            state.update(run=run_id, action=dict(id=action_id, nonce=nonce, epoch=state["epoch"], owner=owner))
            admission = ActionAdmission(generation, state["epoch"], run_id, action_id, nonce)
        return admission

    def end_action(self, admission: ActionAdmission) -> bool:
        """Release an exact admission and say whether its output may be used.

        The caller must only finish after the actual backend operation has
        stopped, including its child tasks. A cancelled await is not proof.
        """
        with self._transaction() as state:
            action = state["action"]
            expected = dict(id=admission.action_id, nonce=admission.nonce, epoch=admission.epoch)
            if (state["generation"] != admission.generation or state["run"] != admission.run_id
                    or action is None or {key: action[key] for key in expected} != expected):
                return False
            valid = state["control"] == "agent" and state["epoch"] == admission.epoch
            state["action"] = None
        return valid

    def finish_run(self, run_id: str, generation: str) -> None:
        _identity(run_id)
        with self._transaction() as state:
            self._check(state, generation)
            _void_dead_claims(state)
            if state["run"] not in (None, run_id) or state["action"] is not None:
                raise ScreenError("SCREEN_BUSY")
            state.update(run=None, runOwner=None)

    def acquire(self, viewer: str, generation: str, epoch: int, *, reclaim: bool = False,
                pending: bool = False) -> dict:
        _identity(viewer)
        with self._transaction() as state:
            self._check(state, generation, epoch)
            _void_dead_claims(state)
            if state["control"] != "agent" and state["viewer"] != viewer and not reclaim:
                raise ScreenError("SCREEN_BUSY")
            if state["viewer"] != viewer:
                if state["input"] is not None:
                    raise ScreenError("INPUT_DRAIN_REQUIRED")
                state.update(viewer=viewer, epoch=state["epoch"] + 1,
                             control="human-pending" if pending or state["action"] is not None else "human")
        return _public(state)

    def confirm_acquire(self, viewer: str, generation: str, epoch: int) -> dict:
        _identity(viewer)
        with self._transaction() as state:
            self._check(state, generation, epoch)
            _void_dead_claims(state)
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
            if state["input"] is not None:
                raise ScreenError("INPUT_DRAIN_REQUIRED")
            state.update(viewer=None, control="agent", epoch=state["epoch"] + 1)
        return _public(state)

    def begin_input(self, viewer: str, generation: str, epoch: int) -> InputAdmission:
        """Reserve one input connection until its acknowledged drain and close.

        Nothing about connection loss proves that queued input has completed.
        The durable reservation therefore survives process death and blocks
        handback/reclaim until explicit recovery establishes that boundary.
        """
        _identity(viewer)
        with self._transaction() as state:
            self._check(state, generation, epoch)
            if state["viewer"] != viewer:
                raise ScreenError("VIEWER_MISMATCH")
            if state["control"] != "human":
                raise ScreenError("TAKEOVER_PENDING")
            if state["input"] is not None:
                raise ScreenError("INPUT_DRAIN_REQUIRED")
            nonce = secrets.token_urlsafe(24)
            state["input"] = dict(nonce=nonce, epoch=epoch)
        return InputAdmission(generation, epoch, nonce)

    def end_input(self, admission: InputAdmission, *, release: bool = False) -> dict:
        """Called only after the broker proved input drain and closed its socket.

        Optional handback is atomic with retiring the input reservation, so a
        second input connection cannot slip between drain and agent admission.
        """
        with self._transaction() as state:
            self._check(state, admission.generation, admission.epoch)
            if state["input"] != dict(nonce=admission.nonce, epoch=admission.epoch):
                raise ScreenError("INPUT_ADMISSION_CHANGED")
            state["input"] = None
            if release:
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

    def may_observe(self, viewer: str, generation: str) -> bool:
        try:
            _identity(viewer)
            with self._transaction() as state:
                self._check(state, generation)
                return state["control"] == "agent" or state["viewer"] == viewer
        except ScreenError:
            return False
