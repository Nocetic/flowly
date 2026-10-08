"""Bounded server-side RFB framing for a shared capture/input connection.

A fence acknowledgement must be a protocol message, never a matching byte
sequence inside pixels or clipboard data. This parser does not decode images;
it streams supported image bodies in bounded pieces and separates fences.
The caller must constrain client pixel formats/encodings to this wire subset.
"""

from __future__ import annotations

import struct
from collections.abc import Callable

from flowly.desktop_runtime.authority import ScreenError

BLOCK = 64 * 1024
MAX_BODY = 64 * 1024 * 1024
# A fixed 32bpp / 24-bit true-colour stream. noVNC's default format fits this.
ENCODINGS = {0, 1, 7, -239, -223, -224, -307, -312, -313}


class RfbServerFramer:
    def __init__(self, forward: Callable[[bytes], None], fence: Callable[[int, bytes], None]):
        self.forward, self.fence = forward, fence
        self.ready = False
        self.failed = False
        self._buffer = bytearray()
        self._parser = self._messages()
        self._needed = next(self._parser)

    def feed(self, data: bytes) -> None:
        if self.failed or not isinstance(data, bytes) or len(data) > 256 * 1024:
            raise ScreenError("INVALID_RFB_STREAM")
        self._buffer.extend(data)
        try:
            while len(self._buffer) >= self._needed:
                part = bytes(self._buffer[: self._needed])
                del self._buffer[: self._needed]
                self._needed = self._parser.send(part)
        except (ValueError, ScreenError, struct.error):
            self.failed = True
            self._buffer.clear()
            raise ScreenError("INVALID_RFB_STREAM") from None

    def _read(self, count: int, *, forward=True):
        if count == 0:
            return b""
        value = yield count
        if forward:
            self.forward(value)
        return value

    def _body(self, count: int):
        if not 0 <= count <= MAX_BODY:
            raise ValueError()
        while count:
            size = min(count, BLOCK)
            yield from self._read(size)
            count -= size

    def _compact_length(self):
        value = (yield from self._read(1))[0]
        length = value & 127
        if value & 128:
            value = (yield from self._read(1))[0]
            length |= (value & 127) << 7
            if value & 128:
                length |= (yield from self._read(1))[0] << 14
        return length

    def _tight(self, width: int, height: int):
        mode = (yield from self._read(1))[0] >> 4
        if mode == 8:
            yield from self._read(3)  # fill, 24-bit RGB
            return
        if mode == 9:
            yield from self._body((yield from self._compact_length()))
            return
        if mode > 7:
            raise ValueError()
        filter_id = (yield from self._read(1))[0] if mode & 4 else 0
        if filter_id not in (0, 1, 2):
            raise ValueError()
        row_size = width * 3
        if filter_id == 1:
            colors = (yield from self._read(1))[0] + 1
            yield from self._read(colors * 3)
            row_size = (width + 7) // 8 if colors == 2 else width
        size = row_size * height
        yield from self._body(size if size < 12 else (yield from self._compact_length()))

    def _messages(self):
        if (yield from self._read(12)) != b"RFB 003.008\n":
            raise ValueError()
        if (yield from self._read(2)) != b"\x01\x01":
            raise ValueError()
        if (yield from self._read(4)) != bytes(4):
            raise ValueError()
        init = yield from self._read(24)
        if init[4:8] not in (b"\x20\x18\x00\x01", b"\x20\x18\x01\x01"):
            raise ValueError()
        if init[8:14] != b"\x00\xff" * 3 or sorted(init[14:17]) != [0, 8, 16]:
            raise ValueError()
        name_size = int.from_bytes(init[20:24], "big")
        if name_size > 4096:
            raise ValueError()
        yield from self._body(name_size)
        self.ready = True
        while True:
            kind = yield from self._read(1, forward=False)
            if kind == b"\xf8":
                header = yield from self._read(8, forward=False)
                flags, size = int.from_bytes(header[3:7], "big"), header[7]
                if size > 64 or flags & ~0x80000007:
                    raise ValueError()
                payload = yield from self._read(size, forward=False)
                self.fence(flags, payload)
                continue
            self.forward(kind)
            if kind in (b"\x02", b"\x96"):
                continue  # Bell / EndOfContinuousUpdates
            if kind == b"\x03":
                header = yield from self._read(7)
                size = abs(int.from_bytes(header[3:7], "big", signed=True))
                if size > 64 * 1024:
                    raise ValueError()
                yield from self._body(size)
                continue
            if kind != b"\x00":
                raise ValueError()
            header = yield from self._read(3)
            rectangles = int.from_bytes(header[1:3], "big")
            for _ in range(rectangles):
                rect = yield from self._read(12)
                _, _, width, height, encoding = struct.unpack("!HHHHi", rect)
                if encoding == -224:
                    break
                if encoding == -223:
                    continue
                if encoding == -307:
                    size = int.from_bytes((yield from self._read(4)), "big")
                    if size > 4096:
                        raise ValueError()
                    yield from self._body(size)
                    continue
                if width * height * 4 > MAX_BODY:
                    raise ValueError()
                if encoding == 0:
                    yield from self._body(width * height * 4)
                elif encoding == 1:
                    yield from self._read(4)
                elif encoding == -239:
                    yield from self._body(width * height * 4 + ((width + 7) // 8) * height)
                elif encoding == 7:
                    yield from self._tight(width, height)
                else:
                    raise ValueError()
