"""Incremental RFB 3.8 client parser for a private, no-auth local Xvnc socket.

The outer authenticated stream owns admission. Parsing is still mandatory:
viewer-side viewOnly cannot authorize keyboard, pointer or clipboard writes.
This parser is only used with the runtime's pinned RFB 3.8/None server policy.
"""

from collections.abc import Callable

from flowly.desktop_runtime.authority import ScreenError


class RfbClientGate:
    MAX_CLIPBOARD = 64 * 1024
    MAX_ENCODINGS = 256
    MAX_CHUNK = 256 * 1024
    _FIXED = {0: 20, 3: 10, 4: 8, 5: 6, 150: 10, 255: 12}
    _INPUT = {4, 5, 6, 251, 255}
    # noVNC 1.7: QEMU keys, extended mouse, desktop resize and extended clipboard.
    _UNSUPPORTED = {value & 0xffffffff for value in (-258, -316, -308, 0xc0a1e5ce)}

    def __init__(self, *, shared_capture: bool = False):
        self.shared_capture = shared_capture
        self.client_fence = False
        self.server_fence = False
        self._buffer = bytearray()
        self._stage = 0
        self._failed = False
        self._buffer_epoch: int | None = None

    def _reject(self) -> None:
        self._failed = True
        self._buffer.clear()
        raise ScreenError("INVALID_RFB_STREAM")

    def feed(self, chunk: bytes, allow_input: Callable[[], bool], *, epoch: int | None = None,
             on_input: Callable[[bytes], None] | None = None) -> bytes:
        """Filter complete messages; callers serialize output with lease changes.

        A partial input message is checked when completed, never when its first
        fragment arrived. An invalid chunk returns no partial output.
        """
        if self._failed or len(chunk) > self.MAX_CHUNK:
            self._reject()
        if self._buffer and self._buffer_epoch != epoch:
            # Never finish an old partial key/paste under a newer permission.
            self._reject()
        self._buffer_epoch = epoch
        self._buffer.extend(chunk)
        output = bytearray()
        inputs = []
        while self._buffer:
            if self._stage == 0:
                if len(self._buffer) < 12:
                    break
                if self._buffer[:12] != b"RFB 003.008\n":
                    self._reject()
                output.extend(self._buffer[:12])
                del self._buffer[:12]
                self._stage = 1
                continue
            if self._stage == 1:
                if self._buffer[0] != 1:
                    self._reject()
                output.append(1)
                del self._buffer[:1]
                self._stage = 2
                continue
            if self._stage == 2:
                if self._buffer[0] not in (0, 1):
                    self._reject()
                # Every attach is shared; a viewer may not evict other clients.
                output.append(1)
                del self._buffer[:1]
                self._stage = 3
                continue

            kind = self._buffer[0]
            size = self._message_size(kind)
            if size is None or len(self._buffer) < size:
                break
            message = bytes(self._buffer[:size])
            del self._buffer[:size]
            if self.shared_capture and kind == 0:
                # The shared server framer supports the fixed true-colour format
                # negotiated by Desktop. Never parse images using a stale bpp.
                if (message[4:8] not in (b"\x20\x18\x00\x01", b"\x20\x18\x01\x01")
                        or message[8:14] != b"\x00\xff" * 3
                        or sorted(message[14:17]) != [0, 8, 16]):
                    self._reject()
                output.extend(message)
            elif self.shared_capture and kind == 2:
                from flowly.desktop_runtime.rfb_server import ENCODINGS

                proposed = [int.from_bytes(message[index:index + 4], "big", signed=True)
                            for index in range(4, len(message), 4)]
                self.client_fence = -312 in proposed
                self.server_fence = True
                selected = [value for value in proposed if value in ENCODINGS
                            or -256 <= value <= -247 or -32 <= value <= -23]
                if -312 not in selected:
                    selected.append(-312)  # input drain still needs server acknowledgement
                output.extend(b"\x02\x00" + len(selected).to_bytes(2, "big") + b"".join(
                    value.to_bytes(4, "big", signed=True) for value in selected))
            elif on_input is not None and kind == 2:
                encodings = [message[index:index + 4] for index in range(4, len(message), 4)
                             if int.from_bytes(message[index:index + 4], "big") not in self._UNSUPPORTED]
                output.extend(b"\x02\x00" + len(encodings).to_bytes(2, "big") + b"".join(encodings))
            elif on_input is not None and kind in self._INPUT:
                if (kind in {251, 255} or (kind == 5 and message[1] & 0x80)
                        or (kind == 6 and message[4] & 0x80)):
                    self._reject()
                if allow_input():
                    inputs.append(message)
            elif kind not in self._INPUT or allow_input():
                output.extend(message)
        if len(self._buffer) > self.MAX_CLIPBOARD + 8:
            self._reject()
        # An invalid chunk must not dispatch an otherwise valid prefix.
        if on_input is not None:
            for message in inputs:
                on_input(message)
        return bytes(output)

    def _message_size(self, kind: int) -> int | None:
        if kind in self._FIXED:
            if kind == 255 and len(self._buffer) >= 2 and self._buffer[1] != 0:
                self._reject()
            if kind == 5:
                if len(self._buffer) < 2:
                    return None
                return 7 if self._buffer[1] & 0x80 else 6
            return self._FIXED[kind]
        if kind == 2:
            if len(self._buffer) < 4:
                return None
            count = int.from_bytes(self._buffer[2:4], "big")
            if count > self.MAX_ENCODINGS:
                self._reject()
            return 4 + count * 4
        if kind == 6:
            if len(self._buffer) < 8:
                return None
            size = abs(int.from_bytes(self._buffer[4:8], "big", signed=True))
            if size > self.MAX_CLIPBOARD:
                self._reject()
            return 8 + size
        if kind == 248:
            if len(self._buffer) < 9:
                return None
            size = self._buffer[8]
            if size > 64:
                self._reject()
            return 9 + size
        if kind == 251:
            if len(self._buffer) < 8:
                return None
            count = self._buffer[6]
            if not 1 <= count <= 16:
                self._reject()
            return 8 + count * 16
        self._reject()
        return None
