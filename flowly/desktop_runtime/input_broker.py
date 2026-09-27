"""Private RFB input connection with an acknowledged drain boundary.

No framebuffer requests are made on this connection. Fence responses therefore
cannot be confused with bytes inside compressed pixel updates. The caller must
keep the broker alive across viewer disconnects; abandoning it never gives
control back. This is a synchronous worker API, not an event-loop callback.
"""

from __future__ import annotations

import secrets
import socket
import struct
import threading
import time
from pathlib import Path

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.desktop_keymap import EXTRA_SLOTS, needs_mapping

REQUEST = 1 << 31
BLOCK_BEFORE = 1
MAX_TEXT = 64 * 1024


def _chunks(value: bytes, limit: int):
    """Split validated input at message boundaries so that each part presses at
    most ``limit`` distinct keysyms that may need a keymap entry."""
    start = offset = 0
    pressed: list[int] = []
    while offset < len(value):
        kind = value[offset]
        size = 8 if kind == 4 else 6 if kind == 5 else 8 + int.from_bytes(value[offset + 4:offset + 8], "big")
        if kind == 4:
            _, down, _, key = struct.unpack_from("!BBHI", value, offset)
            if down and needs_mapping(key) and key not in pressed:
                if len(pressed) == limit:
                    yield value[start:offset], pressed
                    start, pressed = offset, []
                pressed.append(key)
        offset += size
    yield value[start:], pressed


class ScreenInputBroker:
    def __init__(self, authority: ScreenAuthority, viewer: str, generation: str,
                 epoch: int, socket_path: Path, *, timeout: float = 5, keymap=None):
        if not 0 < timeout <= 5:
            raise ScreenError("INVALID_PARAMS")
        self.authority = authority
        self.keymap = keymap
        self.viewer = viewer
        self.generation = generation
        self.epoch = epoch
        self.timeout = timeout
        self._lock = threading.Lock()
        self._keys: set[int] = set()
        self._pointer: tuple[int, int] | None = None
        self._closed = False
        self._socket: socket.socket | None = None
        self._admission = authority.begin_input(viewer, generation, epoch)
        try:
            self._socket = socket.socket(socket.AF_UNIX)
            self._deadline = time.monotonic() + timeout
            self._remaining()
            self._socket.connect(str(socket_path))
            self._handshake()
        except (OSError, ScreenError):
            self._abort()
            # Handshake sends metadata only; no input was admitted to Xvnc.
            authority.end_input(self._admission)
            raise ScreenError("INPUT_TRANSPORT_UNAVAILABLE") from None

    def _remaining(self) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ScreenError("INPUT_OUTCOME_UNKNOWN")
        assert self._socket is not None
        self._socket.settimeout(remaining)

    def _read(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            self._remaining()
            part = self._socket.recv(size - len(result))
            if not part:
                raise ScreenError("INPUT_OUTCOME_UNKNOWN")
            result.extend(part)
        return bytes(result)

    def _write(self, value: bytes) -> None:
        self._remaining()
        self._socket.sendall(value)

    def _read_fence(self) -> tuple[int, bytes]:
        # Bell/cut-buffer messages can arrive without a framebuffer request.
        # Bound their total work and discard clipboard contents immediately.
        for _ in range(32):
            kind = self._read(1)[0]
            if kind == 2:
                continue
            if kind == 3:
                header = self._read(7)
                size = int.from_bytes(header[3:7], "big")
                if size > MAX_TEXT:
                    break
                self._read(size)
                continue
            if kind == 248:
                header = self._read(8)
                size = header[7]
                if size > 64:
                    break
                return int.from_bytes(header[3:7], "big"), self._read(size)
            break
        raise ScreenError("INVALID_RFB_STREAM")

    @staticmethod
    def _fence(flags: int, payload: bytes) -> bytes:
        return struct.pack("!B3xIB", 248, flags, len(payload)) + payload

    def _reply(self, flags: int, payload: bytes) -> None:
        # This connection never requests pixels and has no asynchronous work.
        # BlockBefore/BlockAfter are satisfied; SyncNext is not implemented.
        self._write(self._fence(flags & 3, payload))

    def _drain(self) -> None:
        token = secrets.token_bytes(24)
        self._write(self._fence(REQUEST | BLOCK_BEFORE, token))
        for _ in range(16):
            flags, payload = self._read_fence()
            if flags & REQUEST:
                self._reply(flags, payload)
                continue
            if payload != token or flags != BLOCK_BEFORE:
                break
            return
        raise ScreenError("INPUT_OUTCOME_UNKNOWN")

    def _handshake(self) -> None:
        if self._read(12) != b"RFB 003.008\n":
            raise ScreenError("INVALID_RFB_STREAM")
        self._write(b"RFB 003.008\n")
        count = self._read(1)[0]
        if not 1 <= count <= 32 or 1 not in self._read(count):
            raise ScreenError("INVALID_RFB_STREAM")
        self._write(b"\x01")
        if self._read(4) != bytes(4):
            raise ScreenError("INVALID_RFB_STREAM")
        self._write(b"\x01")  # shared; never evict the viewing connections
        init = self._read(24)
        name_size = int.from_bytes(init[20:24], "big")
        if name_size > 4096:
            raise ScreenError("INVALID_RFB_STREAM")
        self._read(name_size)
        self._write(struct.pack("!BBHi", 2, 0, 1, -312))  # Fence capability
        flags, payload = self._read_fence()
        if not flags & REQUEST:
            raise ScreenError("INVALID_RFB_STREAM")
        self._reply(flags, payload)
        self._drain()  # require an actual BlockBefore acknowledgement

    def _validate_input(self, value: bytes) -> tuple[set[int], tuple[int, int] | None]:
        if not isinstance(value, bytes) or not value or len(value) > MAX_TEXT + 8:
            raise ScreenError("INVALID_RFB_INPUT")
        keys, pointer = set(self._keys), self._pointer
        offset = 0
        while offset < len(value):
            kind = value[offset]
            if kind == 4 and offset + 8 <= len(value):
                _, down, _, key = struct.unpack_from("!BBHI", value, offset)
                if down not in (0, 1):
                    raise ScreenError("INVALID_RFB_INPUT")
                if down:
                    keys.add(key)
                else:
                    keys.discard(key)
                if len(keys) > 256:
                    raise ScreenError("INVALID_RFB_INPUT")
                offset += 8
            elif kind == 5 and offset + 6 <= len(value):
                _, buttons, x, y = struct.unpack_from("!BBHH", value, offset)
                # Extended buttons are not negotiated on this connection.
                if buttons & 0x80:
                    raise ScreenError("INVALID_RFB_INPUT")
                pointer = (x, y)
                offset += 6
            elif kind == 6 and offset + 8 <= len(value):
                size = int.from_bytes(value[offset + 4:offset + 8], "big")
                if size > MAX_TEXT or offset + 8 + size > len(value):
                    raise ScreenError("INVALID_RFB_INPUT")
                offset += 8 + size
            else:
                raise ScreenError("INVALID_RFB_INPUT")
        return keys, pointer

    def send(self, value: bytes) -> None:
        """Send complete basic RFB input messages and await their effects.

        Input contents are never persisted. Unknown delivery poisons this
        connection and keeps the durable reservation; nothing is retried.
        """
        with self._lock:
            if self._closed:
                raise ScreenError("INPUT_OUTCOME_UNKNOWN")
            keys, pointer = self._validate_input(value)
            parts = list(_chunks(value, EXTRA_SLOTS)) if self.keymap is not None else [(value, [])]
            for index, (part, pressed) in enumerate(parts):
                if pressed:
                    # Characters outside the keymap become keys before they are typed.
                    self.keymap.prepare(pressed)
                if not self.authority.may_input(self.viewer, self.generation, self.epoch):
                    if index == 0:
                        raise ScreenError("VIEWER_MISMATCH")
                    self._abort()  # An earlier part was delivered.
                    raise ScreenError("INPUT_OUTCOME_UNKNOWN")
                try:
                    self._deadline = time.monotonic() + self.timeout
                    self._write(part)
                    self._drain()
                except (OSError, ScreenError):
                    self._abort()
                    raise ScreenError("INPUT_OUTCOME_UNKNOWN") from None
            self._keys, self._pointer = keys, pointer

    def finish(self, *, release: bool = False) -> dict:
        """Release pressed keys/buttons, prove drain, then optionally hand back."""
        with self._lock:
            if self._closed:
                raise ScreenError("INPUT_OUTCOME_UNKNOWN")
            try:
                self._deadline = time.monotonic() + self.timeout
                reset = b"".join(struct.pack("!BBHI", 4, 0, 0, key) for key in sorted(self._keys))
                if self._pointer is not None:
                    reset += struct.pack("!BBHH", 5, 0, *self._pointer)
                if reset:
                    self._write(reset)
                self._drain()
                self._abort()
            except (OSError, ScreenError):
                self._abort()
                raise ScreenError("INPUT_OUTCOME_UNKNOWN") from None
            return self.authority.end_input(self._admission, release=release)

    def _abort(self) -> None:
        self._closed = True
        if self._socket is not None:
            self._socket.close()

    def abandon(self) -> None:
        """Dispose after worker shutdown without claiming drain or handback."""
        with self._lock:
            self._abort()
