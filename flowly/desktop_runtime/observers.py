"""Supervisor registry for all cooperating gateway capture connections.

The gateway holds its private lease until both the RFB source and public viewer
stream have closed. Human control is confirmed only after other leases drain.
An unresponsive worker keeps takeover pending, rather than timing out into a
false acknowledgement. No pixels or user input travel through this registry.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError, _identity
from flowly.desktop_runtime.input_service import _receive, _send


class ObserverRegistry:
    def __init__(self, authority: ScreenAuthority, generation: str, directory: Path):
        self.authority, self.generation = authority, generation
        self.path = directory / "observers.sock"
        self._condition = threading.Condition()
        self._entries: dict[socket.socket, tuple[str, bool, str | None]] = {}
        self._connections: set[socket.socket] = set()
        self._stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(8)
        self._workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="screen-observer")
        self._listener = socket.socket(socket.AF_UNIX)
        try:
            self._listener.bind(str(self.path))
            self.path.chmod(0o600)
            self._listener.listen(8)
            self._listener.settimeout(0.2)
        except OSError:
            self._listener.close()
            self._workers.shutdown(wait=True)
            raise ScreenError("CONTROL_UNAVAILABLE") from None
        self._thread = threading.Thread(target=self._accept, daemon=True, name="screen-observer-listener")
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
            with self._condition:
                self._connections.add(connection)
            self._workers.submit(self._serve, connection)

    def _serve(self, connection: socket.socket) -> None:
        try:
            self._watch(connection)
        except ScreenError as error:
            try:
                _send(connection, {"ok": False, "code": error.code})
            except OSError:
                pass
        except OSError:
            pass
        finally:
            connection.close()
            with self._condition:
                self._entries.pop(connection, None)
                self._connections.discard(connection)
                self._condition.notify_all()
            self._slots.release()

    def _watch(self, connection: socket.socket) -> None:
        if hasattr(socket, "SO_PEERCRED"):
            _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return
        request = _receive(connection)
        if set(request) not in ({"generation", "viewer"}, {"generation", "viewer", "stream"}) or request["generation"] != self.generation:
            raise ScreenError("STALE_GENERATION")
        viewer = _identity(request["viewer"])
        stream = _identity(request["stream"]) if "stream" in request else None
        with self._condition:
            if self._stopping.is_set() or not self.authority.may_observe(viewer, self.generation):
                raise ScreenError("HUMAN_HAS_CONTROL")
            if len(self._entries) >= 4:
                raise ScreenError("SCREEN_BUSY")
            if stream is not None:
                # Reattach retires this viewer's previous input source. It
                # cannot become current again when the newer stream closes.
                for previous, entry in self._entries.items():
                    if entry[0] == viewer:
                        self._revoke(previous)
            self._entries[connection] = (viewer, False, stream)
            # A revoke cannot be sent ahead of the framed admission.
            _send(connection, {"ok": True})
        connection.settimeout(0.2)
        while not self._stopping.is_set():
            with self._condition:
                if not self.authority.may_observe(viewer, self.generation):
                    self._revoke(connection)
            try:
                # EOF is the gateway's acknowledgement after teardown.
                # Unexpected data also closes this private protocol.
                connection.recv(1)
                break
            except socket.timeout:
                continue

    def _revoke(self, connection: socket.socket) -> None:
        viewer, revoked, stream = self._entries[connection]
        if not revoked:
            self._entries[connection] = (viewer, True, stream)
            try:
                connection.sendall(b"R")
            except OSError:
                # Retain until the reader confirms EOF. A send failure alone
                # does not establish that the gateway finished its teardown.
                pass

    def is_current(self, viewer: str, stream: str) -> bool:
        with self._condition:
            return self.is_alive and any(entry == (viewer, False, stream) for entry in self._entries.values())

    def drain_except(self, viewer: str, *, timeout: float = 2) -> bool:
        _identity(viewer)
        if not 0 <= timeout <= 5:
            raise ScreenError("INVALID_PARAMS")
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if not self.is_alive:
                    return False
                others = [connection for connection, entry in self._entries.items() if entry[0] != viewer]
                if not others:
                    return True
                for connection in others:
                    self._revoke(connection)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive() and not self._stopping.is_set()

    def close(self) -> None:
        self._stopping.set()
        self._listener.close()
        self._thread.join(1)
        with self._condition:
            for connection in tuple(self._connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self._workers.shutdown(wait=True)
        self.path.unlink(missing_ok=True)
