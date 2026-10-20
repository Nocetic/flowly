"""Bounded inbound RFB batches, separate from WebSocket control-frame reads."""

import asyncio
from collections import deque

from flowly.desktop_runtime.authority import ScreenError

MAX_FRAME = 256 * 1024
MAX_BUFFER = 1024 * 1024
# The Desktop paste limit can produce 4,096 separate key down/up frames.
MAX_MESSAGES = 8192


class ScreenInputQueue:
    """Event-loop owned. Overload closes the stream rather than retaining input.

    Only same-epoch wire fragments are batched. The RFB parser and supervisor
    still authorize delivery; enqueuing never grants permission to send input.
    """

    def __init__(self, framed: bool):
        self.framed = framed
        self._frames: deque[tuple[int | None, bytes]] = deque()
        self._bytes = 0
        self._ready = asyncio.Event()

    def put(self, wire: bytes) -> None:
        epoch = None
        if not 0 < len(wire) <= MAX_FRAME + (8 if self.framed else 0):
            raise ScreenError("INVALID_RFB_STREAM")
        if self.framed:
            if len(wire) <= 8:
                raise ScreenError("INVALID_RFB_STREAM")
            epoch = int.from_bytes(wire[:8], "big")
            if epoch >= 2**53:
                raise ScreenError("INVALID_RFB_STREAM")
            wire = wire[8:]
        if len(self._frames) >= MAX_MESSAGES or self._bytes + len(wire) > MAX_BUFFER:
            raise ScreenError("SCREEN_BUSY")
        self._frames.append((epoch, wire))
        self._bytes += len(wire)
        self._ready.set()

    async def get(self) -> tuple[int | None, bytes]:
        await self._ready.wait()
        epoch, first = self._frames.popleft()
        parts, size = [first], len(first)
        while self._frames and self._frames[0][0] == epoch and size + len(self._frames[0][1]) <= MAX_FRAME:
            _, part = self._frames.popleft()
            parts.append(part)
            size += len(part)
        self._bytes -= size
        if not self._frames:
            self._ready.clear()
        return epoch, b"".join(parts)


def coalesce_pointer_moves(messages: list[bytes]) -> list[bytes]:
    """Keep the first/last position of uninterrupted unpressed mouse motion.

    First positions may press/release buttons. Never move that transition to
    a later coordinate, cross a key/clipboard message, alter a drag path, or
    drop wheel clicks. Pressed-button motion is delivered without coalescing.
    Input here has already passed RfbClientGate's complete-message validation.
    """
    result: list[bytes] = []
    for message in messages:
        if (len(result) >= 2 and message[0] == result[-1][0] == result[-2][0] == 5
                and message[1] == result[-1][1] == result[-2][1] == 0):
            result[-1] = message
        else:
            result.append(message)
    return result
