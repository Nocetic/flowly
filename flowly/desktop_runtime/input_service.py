"""Supervisor-owned private input service, independent of gateway connections.

One bounded request per Unix connection. The authenticated gateway supplies the
viewer capability; neither an RFB path nor a runtime directory is accepted on
the wire. This is not a public RPC endpoint. Same-user arbitrary code is outside
the profile isolation boundary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError, _identity
from flowly.desktop_runtime.input_broker import MAX_TEXT, ScreenInputBroker

MAX_PACKET = 96 * 1024


def _read(connection: socket.socket, size: int, deadline: float) -> bytes:
    value = bytearray()
    while len(value) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        connection.settimeout(remaining)
        part = connection.recv(size - len(value))
        if not part:
            raise ScreenError("INPUT_SERVICE_UNAVAILABLE")
        value.extend(part)
    return bytes(value)


def _receive(connection: socket.socket, *, timeout: float = 2) -> dict:
    deadline = time.monotonic() + timeout
    size = int.from_bytes(_read(connection, 4, deadline), "big")
    if not 0 < size <= MAX_PACKET:
        raise ScreenError("INVALID_PARAMS")
    try:
        value = json.loads(_read(connection, size, deadline))
    except (ValueError, UnicodeError, RecursionError):
        raise ScreenError("INVALID_PARAMS") from None
    if not isinstance(value, dict):
        raise ScreenError("INVALID_PARAMS")
    return value


def _send(connection: socket.socket, value: dict) -> None:
    encoded = json.dumps(value, separators=(",", ":")).encode()
    if len(encoded) > MAX_PACKET:
        raise ScreenError("INVALID_PARAMS")
    connection.sendall(struct.pack("!I", len(encoded)) + encoded)


class InputServiceClient:
    """Main/gateway worker API. Retries need the same handle and input sequence."""

    def __init__(self, path: Path):
        self.path = path

    def request(self, method: str, **params) -> dict:
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(8)
                connection.connect(str(self.path))
                _send(connection, {"method": method, **params})
                response = _receive(connection, timeout=8)
        except OSError:
            raise ScreenError("INPUT_SERVICE_UNAVAILABLE") from None
        if response.get("ok") is True and isinstance(response.get("value"), dict):
            return response["value"]
        code = response.get("code")
        if not isinstance(code, str) or not code.isupper() or len(code) > 64:
            code = "INPUT_SERVICE_UNAVAILABLE"
        raise ScreenError(code)


class InputService:
    """One runtime's input broker; no control release on client EOF/shutdown."""

    def __init__(self, authority: ScreenAuthority, generation: str, socket_directory: Path, *, observers=None):
        self.authority, self.generation = authority, generation
        self.observers = observers
        self.path = socket_directory / "input.sock"
        self.rfb = socket_directory / "rfb.sock"
        self._broker: ScreenInputBroker | None = None
        self._viewer: str | None = None
        self._epoch = 0
        self._handle: str | None = None
        self._sequence = 0
        self._last_digest: bytes | None = None
        self._finished: dict[str, tuple[str, int, bool, dict]] = {}
        self._command = threading.Lock()
        self._connections_lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(4)
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="screen-input")
        self._listener = socket.socket(socket.AF_UNIX)
        try:
            # Supervisor owns stale-socket removal under its exclusive lock.
            self._listener.bind(str(self.path))
            self.path.chmod(0o600)
            self._listener.listen(4)
            self._listener.settimeout(0.2)
        except OSError:
            self._listener.close()
            self._workers.shutdown(wait=True)
            raise ScreenError("INPUT_SERVICE_UNAVAILABLE") from None
        self._thread = threading.Thread(target=self._accept, daemon=True, name="screen-input-listener")
        self._thread.start()

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            with self._connections_lock:
                self._connections.add(connection)
            self._workers.submit(self._serve, connection)

    def _serve(self, connection: socket.socket) -> None:
        try:
            with connection:
                connection.settimeout(2)
                if hasattr(socket, "SO_PEERCRED"):
                    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        return
                try:
                    request = _receive(connection)
                    if self._stopping.is_set():
                        raise ScreenError("INPUT_SERVICE_UNAVAILABLE")
                    if not self._command.acquire(timeout=0.1):
                        raise ScreenError("SCREEN_BUSY")
                    try:
                        result = self._dispatch(request)
                    finally:
                        self._command.release()
                    _send(connection, {"ok": True, "value": result})
                except ScreenError as error:
                    _send(connection, {"ok": False, "code": error.code})
                except (ValueError, TypeError):
                    _send(connection, {"ok": False, "code": "INVALID_PARAMS"})
                except Exception:
                    _send(connection, {"ok": False, "code": "CONTROL_UNAVAILABLE"})
        except OSError:
            # The accepted operation may have completed after client EOF.
            # Its result/sequence remains in the supervisor for a safe retry.
            pass
        finally:
            with self._connections_lock:
                self._connections.discard(connection)
            self._slots.release()

    def _dispatch(self, request: dict) -> dict:
        method = request.get("method")
        fields = {"method", "generation", "viewer", "epoch"}
        extras = {"open": set(), "send": {"handle", "sequence", "data"},
                  "finish": {"handle", "release"}, "acquire": set()}
        if not isinstance(method, str) or method not in extras or set(request) != fields | extras[method]:
            raise ScreenError("INVALID_PARAMS")
        viewer = _identity(request["viewer"])
        epoch = request["epoch"]
        if type(epoch) is not int or not 0 <= epoch < 2**53:
            raise ScreenError("INVALID_PARAMS")
        if request["generation"] != self.generation:
            raise ScreenError("STALE_GENERATION")
        if method == "acquire":
            if self.observers is None or not self.observers.is_alive:
                raise ScreenError("CONTROL_UNAVAILABLE")
            state = self.authority.acquire(viewer, self.generation, epoch, pending=True)
            if self.observers.drain_except(viewer):
                try:
                    state = self.authority.confirm_acquire(viewer, self.generation, state["epoch"])
                except ScreenError as error:
                    if error.code != "TAKEOVER_PENDING":
                        raise
            return state
        if method == "open":
            if not self.authority.may_input(viewer, self.generation, epoch):
                raise ScreenError("VIEWER_MISMATCH")
            if self._broker is None:
                self._broker = ScreenInputBroker(self.authority, viewer, self.generation, epoch, self.rfb)
                self._viewer, self._epoch = viewer, epoch
                self._handle, self._sequence = secrets.token_urlsafe(32), 0
                self._last_digest = None
            elif self._viewer != viewer or self._epoch != epoch:
                raise ScreenError("VIEWER_MISMATCH")
            return {"handle": self._handle, "nextSequence": self._sequence, "generation": self.generation}

        handle = _identity(request["handle"])
        if method == "finish":
            if type(request["release"]) is not bool:
                raise ScreenError("INVALID_PARAMS")
            completed = self._finished.get(handle)
            if completed:
                if completed[:3] != (viewer, epoch, request["release"]):
                    raise ScreenError("VIEWER_MISMATCH")
                return completed[3]
        if (self._broker is None or handle != self._handle
                or self._viewer != viewer or epoch != self._epoch):
            raise ScreenError("VIEWER_MISMATCH")
        if method == "send":
            sequence, data = request["sequence"], request["data"]
            if type(sequence) is not int or not 0 <= sequence < 2**53 or not isinstance(data, str):
                raise ScreenError("INVALID_PARAMS")
            try:
                payload = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                raise ScreenError("INVALID_PARAMS") from None
            if len(payload) > MAX_TEXT + 8:
                raise ScreenError("INVALID_PARAMS")
            digest = hashlib.sha256(payload).digest()
            if sequence == self._sequence - 1 and self._last_digest == digest:
                return {"nextSequence": self._sequence}
            if sequence != self._sequence:
                raise ScreenError("STALE_INPUT_SEQUENCE")
            self._broker.send(payload)
            self._last_digest, self._sequence = digest, sequence + 1
            return {"nextSequence": self._sequence}
        result = self._broker.finish(release=request["release"])
        self._finished[handle] = (viewer, epoch, request["release"], result)
        if len(self._finished) > 64:
            self._finished.pop(next(iter(self._finished)))
        self._broker = None
        return result

    def close(self) -> None:
        self._stopping.set()
        self._listener.close()
        self._thread.join(1)
        with self._connections_lock:
            for connection in tuple(self._connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self._workers.shutdown(wait=True)
        if self._broker is not None:
            self._broker.abandon()
        self.path.unlink(missing_ok=True)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive() and not self._stopping.is_set()
