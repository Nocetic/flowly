import struct

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.rfb_server import BLOCK, RfbServerFramer


def hello():
    pixel_format = struct.pack("!BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
    return (
        b"RFB 003.008\n\x01\x01"
        + bytes(4)
        + struct.pack("!HH", 1440, 900)
        + pixel_format
        + struct.pack("!I", 4)
        + b"test"
    )


def fence(payload=b"private-fence-token", flags=1):
    return struct.pack("!B3xIB", 248, flags, len(payload)) + payload


def update(encoding, payload, width=1, height=1):
    return b"\0\0\0\1" + struct.pack("!HHHHi", 0, 0, width, height, encoding) + payload


@pytest.mark.parametrize("step", [1, 2, 7, 64 * 1024])
def test_fragmentation_preserves_pixels_and_only_extracts_real_fences(step):
    token = fence()
    pixel_body = (token + bytes(128))[:128]
    clipboard = b"\x03" + bytes(3) + struct.pack("!I", len(token)) + token
    wire = hello() + update(0, pixel_body, width=8, height=4) + clipboard + token
    output, acks = [], []
    parser = RfbServerFramer(output.append, lambda flags, payload: acks.append((flags, payload)))
    for offset in range(0, len(wire), step):
        parser.feed(wire[offset : offset + step])
    assert parser.ready
    assert b"".join(output) == wire[: -len(token)]
    assert acks == [(1, b"private-fence-token")]


@pytest.mark.parametrize(
    ("width", "height", "payload"),
    [
        (1, 1, b"\x80\x12\x34\x56"),  # fill
        (2, 1, b"\x00" + bytes(6)),  # uncompressed copy filter
        (4, 1, b"\x00\x05abcde"),  # compressed copy filter
        (8, 1, b"\x40\x01\x01" + bytes(6) + b"\xff"),  # two-colour palette
        (16, 16, b"\x40\x01\x02" + bytes(9) + b"\x03abc"),
        (4, 4, b"\x40\x02\x03abc"),  # gradient filter
        (1, 1, b"\x90\x03abc"),  # JPEG
        (1, 1, b"\x90\x80\x01" + bytes(128)),
        (1, 1, b"\x90\x80\x80\x01" + bytes(16384)),
    ],
)
def test_tight_boundaries_cannot_swallow_following_ack(width, height, payload):
    output, acks = [], []
    parser = RfbServerFramer(output.append, lambda flags, payload: acks.append(payload))
    wire = hello() + update(7, payload, width, height)
    for byte in wire + fence():
        parser.feed(bytes([byte]))
    assert b"".join(output) == wire
    assert acks == [b"private-fence-token"]


def test_large_pixels_forward_in_bounded_chunks():
    output = []
    parser = RfbServerFramer(output.append, lambda *_: None)
    parser.feed(hello())
    pixels = bytes(BLOCK * 3)
    wire = update(0, pixels, 256, 192)
    for offset in range(0, len(wire), BLOCK):
        parser.feed(wire[offset : offset + BLOCK])
    assert max(map(len, output)) <= BLOCK
    assert b"".join(output) == hello() + wire


@pytest.mark.parametrize(
    "message",
    [
        b"\x01",  # colour map forbidden for fixed true colour
        update(16, b"garbage"),  # unnegotiated ZRLE
        update(7, b"\xa0"),  # unnegotiated Tight PNG
        update(7, b"\x40\x03"),  # undefined Tight filter
        b"\xf8" + bytes(7) + b"\x41",  # oversized fence
        b"\xf8" + bytes(3) + struct.pack("!I", 8) + b"\x00",
        b"\x03" + bytes(3) + struct.pack("!I", 65537),
    ],
)
def test_unknown_or_oversized_messages_poison_parser(message):
    parser = RfbServerFramer(lambda _: None, lambda *_: None)
    parser.feed(hello())
    with pytest.raises(ScreenError, match="INVALID_RFB_STREAM"):
        parser.feed(message)
    with pytest.raises(ScreenError, match="INVALID_RFB_STREAM"):
        parser.feed(fence())


def test_cursor_copyrect_lastrect_and_desktop_name_preserve_wire():
    output, acks = [], []
    parser = RfbServerFramer(output.append, lambda *_: acks.append(True))
    messages = (
        update(-239, bytes(5))
        + update(1, bytes(4))
        + update(-307, struct.pack("!I", 4) + b"test")
        + update(-223, b"")
        + b"\0\0\xff\xff"
        + struct.pack("!HHHHi", 0, 0, 0, 0, -224)
        + b"\x96\x02"
    )
    parser.feed(hello() + messages + fence())
    assert b"".join(output) == hello() + messages and acks == [True]
