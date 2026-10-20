"""One still picture of a profile screen's whole desktop.

The agent's ``screenshot`` on a screen host shows the desktop the person sees,
not only the browser tab. The supervisor serves it on a private socket next to
its other services; the picture is taken inside the X session by a short
helper running as the screen's application account (the display's key never
leaves that account). The gateway asks only as one of a run's screen actions,
so nothing is captured while the person has control.
"""

from __future__ import annotations

import socket
import struct
import threading
from pathlib import Path
from typing import Callable

from flowly.desktop_runtime.authority import ScreenError

# Bound on the picture (a 3840x2160 PNG of a busy desktop stays far below).
MAX_STILL_BYTES = 24 * 1024 * 1024
_REQUEST = b"still"

# Run as the application account with the session's DISPLAY/XAUTHORITY:
# python3 -I -c STILL_SOURCE. Writes one PNG of the root window to stdout.
# Standard library and libX11 only; the encoder is tested by exec'ing this
# source without __name__ == "__main__".
STILL_SOURCE = r'''
import ctypes
import struct
import sys
import zlib


class XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
                ("format", ctypes.c_int), ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
                ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong)]


def _offset(mask, msb_first):
    shift = mask.bit_length() - 8
    if shift < 0 or shift % 8 or mask != 0xFF << shift:
        raise ValueError("unsupported pixel format")
    return 3 - shift // 8 if msb_first else shift // 8


def encode_png(width, height, data, bytes_per_line, masks, msb_first=False):
    """PNG of 32-bit pixels whose 8-bit channels sit at ``masks`` (r, g, b)."""
    red, green, blue = (_offset(mask, msb_first) for mask in masks)
    rows = bytearray()
    row = bytearray(width * 3)
    for y in range(height):
        line = data[y * bytes_per_line:y * bytes_per_line + width * 4]
        row[0::3], row[1::3], row[2::3] = line[red::4], line[green::4], line[blue::4]
        rows += b"\x00" + row

    def chunk(kind, body):
        return struct.pack("!I", len(body)) + kind + body + struct.pack("!I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


def main():
    x = ctypes.CDLL("libX11.so.6")
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x.XRootWindow.restype = ctypes.c_ulong
    x.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XDisplayWidth.argtypes = x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XGetImage.restype = ctypes.POINTER(XImage)
    x.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                            ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
    display = x.XOpenDisplay(None)
    if not display:
        raise SystemExit(3)
    screen = x.XDefaultScreen(display)
    width, height = x.XDisplayWidth(display, screen), x.XDisplayHeight(display, screen)
    if not (0 < width <= 7680 and 0 < height <= 4320):
        raise SystemExit(4)
    image = x.XGetImage(display, x.XRootWindow(display, screen), 0, 0, width, height, 0xFFFFFFFF, 2)
    if not image or image.contents.bits_per_pixel != 32:
        raise SystemExit(5)
    picture = image.contents
    data = ctypes.string_at(picture.data, picture.bytes_per_line * picture.height)
    png = encode_png(picture.width, picture.height, data, picture.bytes_per_line,
                     (picture.red_mask, picture.green_mask, picture.blue_mask), picture.byte_order == 1)
    sys.stdout.buffer.write(png)


if __name__ == "__main__":
    main()
'''


def _receive(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = connection.recv(min(size - len(data), 1 << 20))
        if not part:
            raise ScreenError("SCREEN_STILL_FAILED")
        data += part
    return bytes(data)


class StillService:
    """Supervisor side: one picture per connection, one at a time."""

    def __init__(self, directory: Path, capture: Callable[[], bytes]):
        self.path = directory / "still.sock"
        self._capture = capture
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX)
        try:
            self._listener.bind(str(self.path))
            self.path.chmod(0o600)
            self._listener.listen(2)
            self._listener.settimeout(0.2)
        except OSError:
            self._listener.close()
            raise ScreenError("CONTROL_UNAVAILABLE") from None
        self._thread = threading.Thread(target=self._accept, daemon=True, name="screen-still")
        self._thread.start()

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(15)
                    if _receive(connection, len(_REQUEST)) != _REQUEST:
                        continue
                    with self._lock:
                        try:
                            picture = self._capture()
                        except Exception:
                            picture = b""
                    if not picture.startswith(b"\x89PNG") or len(picture) > MAX_STILL_BYTES:
                        picture = b""
                    connection.sendall(struct.pack("!I", len(picture)) + picture)
                except (OSError, ScreenError):
                    continue

    def close(self) -> None:
        self._stopping.set()
        self._listener.close()
        self._thread.join(timeout=2)
        self.path.unlink(missing_ok=True)


def request_still(path: Path, *, timeout: float = 15) -> bytes:
    """Gateway side: the desktop as PNG bytes, or ScreenError."""
    if not path.exists():
        # A screen started before this service: it has no still picture.
        raise ScreenError("SCREEN_STILL_UNAVAILABLE")
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        try:
            connection.connect(str(path))
            connection.sendall(_REQUEST)
            (size,) = struct.unpack("!I", _receive(connection, 4))
            if not 0 < size <= MAX_STILL_BYTES:
                raise ScreenError("SCREEN_STILL_FAILED")
            return _receive(connection, size)
        except OSError:
            raise ScreenError("SCREEN_STILL_FAILED") from None
