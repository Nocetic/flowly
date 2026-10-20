import asyncio
import struct

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.stream_input import (
    MAX_BUFFER,
    MAX_FRAME,
    MAX_MESSAGES,
    ScreenInputQueue,
    coalesce_pointer_moves,
)


def frame(epoch, payload):
    return epoch.to_bytes(8, "big") + payload


async def test_batches_only_same_epoch_and_preserves_wire_order():
    queue = ScreenInputQueue(True)
    queue.put(frame(1, b"partial"))
    queue.put(frame(1, b"-key"))
    queue.put(frame(2, b"new"))
    queue.put(frame(1, b"old"))
    assert await queue.get() == (1, b"partial-key")
    assert await queue.get() == (2, b"new")
    assert await queue.get() == (1, b"old")
    waiting = asyncio.create_task(queue.get())
    await asyncio.sleep(0)
    assert not waiting.done()
    queue.put(frame(3, b"wake"))
    assert await waiting == (3, b"wake")


async def test_bounds_bytes_and_batch_size_and_reuses_capacity():
    queue = ScreenInputQueue(False)
    for _ in range(MAX_BUFFER // MAX_FRAME):
        queue.put(b"x" * MAX_FRAME)
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        queue.put(b"y")
    assert await queue.get() == (None, b"x" * MAX_FRAME)
    queue.put(b"y")
    for _ in range(MAX_BUFFER // MAX_FRAME - 1):
        assert await queue.get() == (None, b"x" * MAX_FRAME)
    assert await queue.get() == (None, b"y")


def test_bounds_tiny_message_count():
    queue = ScreenInputQueue(False)
    for _ in range(MAX_MESSAGES):
        queue.put(b"x")
    with pytest.raises(ScreenError, match="SCREEN_BUSY"):
        queue.put(b"x")


@pytest.mark.parametrize("wire", [b"", b"12345678", frame(2**53, b"x"), frame(1, b"x" * (MAX_FRAME + 1))])
def test_rejects_invalid_epoch_envelopes(wire):
    with pytest.raises(ScreenError, match="INVALID_RFB_STREAM"):
        ScreenInputQueue(True).put(wire)


def pointer(x, buttons=0):
    return struct.pack("!BBHH", 5, buttons, x, 10)


def test_pointer_burst_retains_first_and_latest_position():
    assert coalesce_pointer_moves([pointer(x) for x in range(1000)]) == [pointer(0), pointer(999)]


def test_never_moves_click_drag_wheel_or_key_transitions():
    key = struct.pack("!BBHI", 4, 1, 0, ord("a"))
    messages = [pointer(1), pointer(2), pointer(3),
                pointer(4, 1), pointer(5, 1), pointer(6, 1), pointer(7, 1),
                pointer(8), pointer(9), pointer(10), key, pointer(11),
                pointer(12, 8), pointer(12), pointer(12, 8), pointer(12)]
    assert coalesce_pointer_moves(messages) == [pointer(1), pointer(3), pointer(4, 1), pointer(5, 1), pointer(6, 1), pointer(7, 1),
                                               pointer(8), pointer(10), key, pointer(11),
                                               pointer(12, 8), pointer(12), pointer(12, 8), pointer(12)]


async def test_full_desktop_paste_preserves_all_keys():
    queue = ScreenInputQueue(True)
    keys = [struct.pack("!BBHI", 4, down, 0, ord("a")) for _ in range(2048) for down in (1, 0)]
    for key in keys:
        queue.put(frame(2, key))
    assert await queue.get() == (2, b"".join(keys))
    assert coalesce_pointer_moves(keys) == keys
