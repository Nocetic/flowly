import struct

import pytest

from flowly.desktop_runtime.authority import ScreenError
from flowly.desktop_runtime.rfb import RfbClientGate

HELLO = b"RFB 003.008\n\x01\x00"
KEY = struct.pack("!BBHI", 4, 1, 0, ord("x"))
POINTER = struct.pack("!BBHH", 5, 1, 100, 200)
REQUEST = struct.pack("!BBHHHH", 3, 1, 0, 0, 1440, 900)
CLIPBOARD = b"\x06\0\0\0" + struct.pack("!i", 6) + b"secret"
EXTENDED_KEY = b"\xff\0" + bytes(10)
RESIZE = b"\xfb\0\x05\xa0\x03\x84\x01\0" + bytes(16)


@pytest.mark.parametrize("split", range(len(HELLO) + 1))
def test_fragmented_handshake_forces_shared_mode(split):
    gate = RfbClientGate()
    output = gate.feed(HELLO[:split], lambda: False) + gate.feed(HELLO[split:], lambda: False)
    assert output == HELLO[:-1] + b"\x01"


@pytest.mark.parametrize("message", [KEY, POINTER, CLIPBOARD, EXTENDED_KEY, RESIZE])
def test_observers_cannot_inject_any_supported_input(message):
    gate = RfbClientGate()
    gate.feed(HELLO, lambda: False)
    assert gate.feed(REQUEST + message + REQUEST, lambda: False) == REQUEST * 2


@pytest.mark.parametrize("message", [KEY, POINTER, CLIPBOARD, EXTENDED_KEY, RESIZE])
def test_holders_can_send_input(message):
    gate = RfbClientGate()
    gate.feed(HELLO, lambda: True)
    assert gate.feed(message, lambda: True) == message


def test_input_is_reauthorized_after_fragmented_message_completes():
    gate = RfbClientGate()
    gate.feed(HELLO, lambda: True)
    assert gate.feed(KEY[:4], lambda: True) == b""
    assert gate.feed(KEY[4:], lambda: False) == b""


def test_each_input_checks_current_authority():
    gate = RfbClientGate()
    gate.feed(HELLO, lambda: True)
    permissions = iter([True, False])
    assert gate.feed(KEY * 2, lambda: next(permissions)) == KEY


@pytest.mark.parametrize("bad", [
    b"\x99", b"\xff\x01", b"\x02\0\xff\xff",
    b"\x06\0\0\0\x7f\xff\xff\xff", b"\x06\0\0\0\x80\0\0\0",
    b"\xf8" + bytes(7) + b"\xff", b"\xfb" + bytes(7),
])
def test_malformed_input_permanently_closes_parser(bad):
    gate = RfbClientGate()
    gate.feed(HELLO, lambda: True)
    with pytest.raises(ScreenError, match="INVALID_RFB_STREAM"):
        gate.feed(bad, lambda: True)
    with pytest.raises(ScreenError, match="INVALID_RFB_STREAM"):
        gate.feed(REQUEST, lambda: True)


def test_all_byte_fragmentations_preserve_protocol():
    original = HELLO + REQUEST + KEY + POINTER + CLIPBOARD
    for chunk_size in range(1, len(original) + 1):
        gate = RfbClientGate()
        output = b"".join(gate.feed(original[i:i+chunk_size], lambda: True)
                          for i in range(0, len(original), chunk_size))
        assert output == HELLO[:-1] + b"\x01" + original[len(HELLO):]


def test_bad_handshake_and_oversized_chunk_are_rejected():
    for value in (b"RFB 003.003\n", bytes(RfbClientGate.MAX_CHUNK + 1)):
        with pytest.raises(ScreenError, match="INVALID_RFB_STREAM"):
            RfbClientGate().feed(value, lambda: True)
