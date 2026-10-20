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


def test_shared_capture_limits_encodings_and_never_forwards_public_input():
    proposed = [16, 7, 1, 0, -239, -223, -224, -307, -313, -258, -316, -308, -250, -26]
    encodings = struct.pack("!BBH", 2, 0, len(proposed)) + b"".join(struct.pack("!i", e) for e in proposed)
    for fragment in (1, 7, len(encodings)):
        gate = RfbClientGate(shared_capture=True)
        gate.feed(HELLO, lambda: False)
        wire = encodings + KEY + POINTER + CLIPBOARD + REQUEST
        result = b"".join(gate.feed(wire[i:i+fragment], lambda: False) for i in range(0, len(wire), fragment))
        selected = [7, 1, 0, -239, -223, -224, -307, -313, -250, -26, -312]
        expected = struct.pack("!BBH", 2, 0, len(selected)) + b"".join(struct.pack("!i", e) for e in selected)
        assert result == expected + REQUEST
        assert gate.server_fence and not gate.client_fence
        assert gate.feed(struct.pack("!BBHi", 2, 0, 1, -312), lambda: False) == struct.pack("!BBHi", 2, 0, 1, -312)
        assert gate.client_fence


@pytest.mark.parametrize("bpp,depth,truecolor,red,green,blue", [
    (32, 24, 1, 16, 8, 0), (32, 24, 1, 0, 8, 16),
    (16, 16, 1, 11, 5, 0), (32, 24, 0, 16, 8, 0), (32, 24, 1, 16, 16, 0),
])
def test_shared_capture_fixed_pixel_format(bpp, depth, truecolor, red, green, blue):
    gate = RfbClientGate(shared_capture=True)
    gate.feed(HELLO, lambda: False)
    message = bytes(4) + struct.pack("!BBBBHHHBBB3x", bpp, depth, 0, truecolor, 255, 255, 255, red, green, blue)
    if bpp == 32 and truecolor and sorted((red, green, blue)) == [0, 8, 16]:
        assert gate.feed(message, lambda: False) == message
    else:
        with pytest.raises(ScreenError, match="INVALID_RFB_STREAM"):
            gate.feed(message, lambda: False)


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


def test_routed_input_never_reaches_capture_socket_and_strips_unsupported_encodings():
    gate, inputs = RfbClientGate(), []
    gate.feed(HELLO, lambda: False, epoch=0, on_input=inputs.append)
    encodings = struct.pack("!BBHiiiiI", 2, 0, 5, 0, -258, -316, -308, 0xc0a1e5ce)
    output = gate.feed(encodings + KEY + REQUEST + POINTER, lambda: True, epoch=2, on_input=inputs.append)
    assert output == struct.pack("!BBHi", 2, 0, 1, 0) + REQUEST
    assert inputs == [KEY, POINTER]


def test_partial_input_cannot_be_retagged_at_a_control_epoch_boundary():
    gate, inputs = RfbClientGate(), []
    gate.feed(HELLO + KEY[:3], lambda: False, epoch=0, on_input=inputs.append)
    with pytest.raises(ScreenError, match="INVALID_RFB_STREAM"):
        gate.feed(KEY[3:], lambda: True, epoch=2, on_input=inputs.append)
    assert inputs == []


@pytest.mark.parametrize("message", [EXTENDED_KEY, RESIZE, b"\x05\x80\0\0\0\0\x01",
                                    b"\x06\0\0\0\xff\xff\xff\xfc" + bytes(4), b"\x99"])
def test_unnegotiated_input_rejects_whole_chunk_without_dispatch(message):
    gate, inputs = RfbClientGate(), []
    gate.feed(HELLO, lambda: False, epoch=0, on_input=inputs.append)
    with pytest.raises(ScreenError, match="INVALID_RFB_STREAM"):
        gate.feed(KEY + message, lambda: True, epoch=2, on_input=inputs.append)
    assert inputs == []


def test_extended_pointer_is_parsed_as_seven_bytes_even_when_dropped():
    gate = RfbClientGate()
    gate.feed(HELLO, lambda: False)
    assert gate.feed(b"\x05\x80\0\0\0\0\x01" + REQUEST, lambda: False) == REQUEST
