"""Supervisor-owned VNC capture that can lend its input side to the broker.

The viewer never owns a fence receipt. The server parser extracts receipts from
actual protocol boundaries; pixel/clipboard bytes cannot acknowledge input.
A detached viewer's capture is drained/discarded while the input broker still
owns it, so a crashed gateway cannot prevent a later safe handback.
"""

from __future__ import annotations

import queue
import socket
import struct
import threading

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.rfb import RfbClientGate
from flowly.desktop_runtime.rfb_server import RfbServerFramer

MAX_BUFFER = 1024 * 1024


class SharedRfbPeer:
    def __init__(self, frontend: socket.socket, backend: socket.socket):
        self.frontend, self.backend = frontend, backend
        self.gate = RfbClientGate(shared_capture=True)
        self._state = threading.RLock()
        self._write_lock = threading.Lock()
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=8192)
        self._queued = 0
        self._closed = False
        self._detached = False
        self._claimed = False
        self._pending: bytes | None = None
        self._fences: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        self._ready = threading.Event()
        self._negotiated = threading.Event()
        self.framer = RfbServerFramer(self._forward, self._fence)
        self.backend.settimeout(0.2)
        self.frontend.settimeout(0.2)
        self._threads = [
            threading.Thread(target=target, daemon=True, name="screen-shared-rfb")
            for target in (self._read_backend, self._read_frontend, self._write_frontend)
        ]
        for thread in self._threads:
            thread.start()

    @property
    def closed(self):
        with self._state:
            return self._closed

    def _forward(self, data: bytes):
        with self._state:
            if self._closed or self._detached:
                return
            if self._queued + len(data) > MAX_BUFFER or self._queue.full():
                self.detach()
                return
            self._queued += len(data)
            self._queue.put_nowait(data)

    def _fence(self, flags: int, payload: bytes):
        wire = struct.pack("!B3xIB", 248, flags, len(payload)) + payload
        with self._state:
            if not flags & (1 << 31) and payload == self._pending:
                # Only the broker's exact BlockBefore receipt can finish drain.
                if flags != 1:
                    raise ScreenError("INVALID_RFB_STREAM")
                self._pending = None
                self._fences.put_nowait(wire)
                return
            detached = self._detached
        if flags & (1 << 31) and (detached or not self.gate.client_fence):
            self.write(struct.pack("!B3xIB", 248, flags & 3, len(payload)) + payload)
        else:
            self._forward(wire)

    def write(self, data: bytes):
        with self._write_lock:
            if self.closed:
                raise OSError("RFB capture closed")
            self.backend.sendall(data)

    def _read_backend(self):
        try:
            while not self.closed:
                try:
                    data = self.backend.recv(64 * 1024)
                except socket.timeout:
                    continue
                if not data:
                    break
                self.framer.feed(data)
                if self.framer.ready:
                    self._ready.set()
        except (OSError, ScreenError, queue.Full):
            pass
        finally:
            self.close()

    def _read_frontend(self):
        try:
            while not self.closed:
                try:
                    data = self.frontend.recv(64 * 1024)
                except socket.timeout:
                    continue
                if not data:
                    break
                # Public input cannot bypass the acknowledged broker. Only
                # capture negotiation and frame/fence requests pass this side.
                output = self.gate.feed(data, lambda: False)
                if output:
                    self.write(output)
                    if self.gate.server_fence:
                        self._negotiated.set()
        except (OSError, ScreenError):
            pass
        finally:
            self.detach()

    def _write_frontend(self):
        try:
            while not self.closed:
                try:
                    data = self._queue.get(timeout=0.2)
                except queue.Empty:
                    if self._detached:
                        break
                    continue
                if data is None:
                    break
                # Framing emits small headers as well as pixel bodies. Combine
                # already-available pieces without a batching delay, avoiding
                # a socket write (and gateway validation) per RFB header.
                parts, size = [data], len(data)
                while size < 64 * 1024:
                    try:
                        part = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if part is None:
                        break
                    parts.append(part)
                    size += len(part)
                try:
                    self.frontend.sendall(b"".join(parts))
                finally:
                    with self._state:
                        self._queued -= size
        except OSError:
            pass
        finally:
            self.detach()

    def detach(self):
        with self._state:
            if self._detached:
                return
            self._detached = True
            try:
                self.frontend.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.frontend.close()
            if not self._claimed:
                self.close()

    def claim(self):
        if not self._ready.wait(3) or not self._negotiated.wait(3):
            raise ScreenError("INPUT_TRANSPORT_UNAVAILABLE")
        with self._state:
            if self._closed or self._detached or self._claimed:
                raise ScreenError("INPUT_TRANSPORT_UNAVAILABLE")
            self._claimed = True
            return SharedRfbInput(self)

    def close(self):
        with self._state:
            if self._closed:
                return
            self._closed = True
            self._ready.set()
            self._negotiated.set()
            for connection in (self.backend, self.frontend):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            for channel in (self._fences, self._queue):
                try:
                    channel.put_nowait(None)
                except queue.Full:
                    pass

    def release_input(self):
        """Return a proven-drained input side while preserving a live viewer."""
        with self._state:
            if self._pending is not None:
                raise ScreenError("INPUT_OUTCOME_UNKNOWN")
            self._claimed = False
            if self._detached:
                self.close()

    def join(self):
        self.close()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(1)


class SharedRfbInput:
    """Socket-like broker adapter; only parsed private fence receipts are read."""

    def __init__(self, peer: SharedRfbPeer):
        self.peer = peer
        self.timeout = 5
        self.buffer = bytearray()
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, value: bytes):
        if self.closed:
            raise OSError("RFB input closed")
        if value[:1] == b"\xf8":
            if (
                len(value) < 9
                or len(value) != 9 + value[8]
                or int.from_bytes(value[4:8], "big") != 0x80000001
            ):
                raise ScreenError("INVALID_RFB_STREAM")
            with self.peer._state:
                if self.peer._pending is not None:
                    raise ScreenError("INPUT_OUTCOME_UNKNOWN")
                self.peer._pending = value[9:]
        self.peer.write(value)

    def recv(self, size):
        if self.closed:
            return b""
        if not self.buffer:
            try:
                receipt = self.peer._fences.get(timeout=self.timeout)
            except queue.Empty:
                raise TimeoutError() from None
            if receipt is None:
                return b""
            self.buffer.extend(receipt)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def close(self):
        if not self.closed:
            self.closed = True
            self.peer.close()

    def release(self):
        if not self.closed:
            self.peer.release_input()
            self.closed = True
