"""Private supervisor capture admission, scoped to a live observer stream."""

from __future__ import annotations

import os
import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenError, _identity
from flowly.desktop_runtime.input_service import _receive, _send
from flowly.desktop_runtime.shared_rfb import SharedRfbPeer


class CaptureService:
    def __init__(self, generation: str, directory: Path, rfb: Path, *, observers, viewers):
        self.generation, self.rfb = generation, rfb
        self.observers, self.viewers = observers, viewers
        self.path = directory / "captures.sock"
        self._lock = threading.Lock()
        self._peers: dict[tuple[str, str], SharedRfbPeer] = {}
        self._connections: set[socket.socket] = set()
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(8)
        self._workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="screen-capture")
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
        self._thread = threading.Thread(
            target=self._accept, daemon=True, name="screen-capture-listener"
        )
        self._thread.start()

    @property
    def is_alive(self):
        return self._thread.is_alive() and not self._stop.is_set()

    def claim(self, viewer: str, stream: str):
        with self._lock:
            peer = self._peers.get((viewer, stream))
        if (
            peer is None
            or not self.observers.is_current(viewer, stream)
            or not self.viewers.available(viewer, self.generation)
        ):
            raise ScreenError("VIEWER_REVOKED")
        return peer.claim()

    def _accept(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            with self._lock:
                self._connections.add(connection)
            self._workers.submit(self._serve, connection)

    def _serve(self, connection):
        peer = None
        backend = None
        key = None
        admitted = False
        try:
            if hasattr(socket, "SO_PEERCRED"):
                _, uid, _ = struct.unpack(
                    "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid != os.getuid():
                    return
            request = _receive(connection)
            if (
                set(request) != {"viewer", "generation", "stream"}
                or request["generation"] != self.generation
            ):
                raise ScreenError("STALE_GENERATION")
            viewer, stream = _identity(request["viewer"]), _identity(request["stream"])
            key = (viewer, stream)
            if not self.viewers.available(viewer, self.generation) or not self.observers.is_current(
                viewer, stream
            ):
                raise ScreenError("VIEWER_REVOKED")
            backend = socket.socket(socket.AF_UNIX)
            backend.settimeout(3)
            backend.connect(str(self.rfb))
            with self._lock:
                if self._stop.is_set() or key in self._peers:
                    raise ScreenError("SCREEN_BUSY")
                _send(connection, {"ok": True})
                admitted = True
                peer = SharedRfbPeer(connection, backend)
                self._peers[key] = peer
            while not self._stop.wait(0.2) and not peer.closed:
                if not self.observers.is_current(viewer, stream) or not self.viewers.available(
                    viewer, self.generation
                ):
                    # Stop capture immediately, retain only a broker-owned
                    # backend needed to release held input and prove drain.
                    peer.detach()
        except (OSError, ScreenError):
            if not admitted:
                try:
                    _send(connection, {"ok": False})
                except OSError:
                    pass
        finally:
            if peer:
                peer.join()
            elif backend:
                backend.close()
            connection.close()
            with self._lock:
                if key is not None and self._peers.get(key) is peer:
                    self._peers.pop(key, None)
                self._connections.discard(connection)
            self._slots.release()

    def close(self):
        self._stop.set()
        self._listener.close()
        self._thread.join(1)
        with self._lock:
            peers, connections = list(self._peers.values()), list(self._connections)
        for peer in peers:
            peer.close()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._workers.shutdown(wait=True)
        self.path.unlink(missing_ok=True)
