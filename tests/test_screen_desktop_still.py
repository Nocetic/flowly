"""The screen's whole-desktop picture: encoder, supervisor service, admission.

An agent asked for "a screenshot of the desktop" found the display's key and
drove ffmpeg/xdotool from a shell (test VPS, 2026-10-20). The screen now gives
that picture itself, inside its session, as one of the run's actions.
"""

from __future__ import annotations

import io
import struct
from unittest.mock import Mock

import pytest

from flowly.desktop_runtime.authority import ScreenAuthority, ScreenError
from flowly.desktop_runtime.browser_provider import HostBrowserProvider
from flowly.desktop_runtime.desktop_still import STILL_SOURCE, StillService, request_still
from flowly.desktop_runtime.execution import ScreenExecution


def _helper():
    namespace = {"__name__": "flowly_still_test"}
    exec(compile(STILL_SOURCE, "still", "exec"), namespace)
    return namespace


def _decode(png):
    from PIL import Image

    return Image.open(io.BytesIO(png)).convert("RGB")


@pytest.mark.parametrize("msb_first", [False, True])
def test_the_helper_encodes_the_x_servers_pixels_as_png(msb_first):
    encode = _helper()["encode_png"]
    width, height, stride = 3, 2, 16  # rows padded beyond width * 4
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (20, 120, 220), (0, 0, 0), (255, 255, 255)]
    data = bytearray(stride * height)
    for index, (red, green, blue) in enumerate(colors):
        y, x = divmod(index, width)
        pixel = struct.pack(">I" if msb_first else "<I", red << 16 | green << 8 | blue)
        data[y * stride + x * 4:y * stride + x * 4 + 4] = pixel
    image = _decode(encode(width, height, bytes(data), stride, (0xFF0000, 0x00FF00, 0x0000FF), msb_first))
    assert image.size == (3, 2)
    assert [image.getpixel((x, y)) for y in range(2) for x in range(3)] == colors


def test_the_helper_refuses_a_pixel_format_it_cannot_read():
    encode = _helper()["encode_png"]
    with pytest.raises(ValueError):
        encode(1, 1, b"\0\0\0\0", 4, (0xF800, 0x07E0, 0x001F))


def test_the_service_answers_one_picture_and_requests_get_it(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 5000
    service = StillService(tmp_path, lambda: png)
    try:
        assert (tmp_path / "still.sock").stat().st_mode & 0o777 == 0o600
        assert request_still(tmp_path / "still.sock") == png
        assert request_still(tmp_path / "still.sock") == png
    finally:
        service.close()
    assert not (tmp_path / "still.sock").exists()


@pytest.mark.parametrize("capture", [lambda: b"", lambda: b"not a png", lambda: 1 / 0])
def test_a_failed_capture_is_an_error_not_bytes(tmp_path, capture):
    service = StillService(tmp_path, capture)
    try:
        with pytest.raises(ScreenError, match="SCREEN_STILL_FAILED"):
            request_still(tmp_path / "still.sock")
    finally:
        service.close()


def test_a_screen_started_before_the_service_has_no_picture(tmp_path):
    with pytest.raises(ScreenError, match="SCREEN_STILL_UNAVAILABLE"):
        request_still(tmp_path / "still.sock")


@pytest.fixture
def provider(tmp_path, monkeypatch):
    authority = ScreenAuthority(tmp_path)
    authority.start("generation")
    runtime = Mock(authority=authority, socket_directory=tmp_path)
    runtime.status.return_value = {"phase": "running", "generation": "generation"}
    asked = []

    def still(path):
        asked.append(path)
        return b"\x89PNG desktop"

    monkeypatch.setattr("flowly.desktop_runtime.desktop_still.request_still", still)
    return HostBrowserProvider(runtime, ScreenExecution(authority)), asked


async def test_the_agent_gets_the_desktop_as_one_of_its_actions(provider):
    instance, asked = provider
    result = await instance.desktop_screenshot("run", "a", "generation")
    assert result["screenshot"].startswith("data:image/png;base64,")
    assert instance.observation_valid(result)
    assert asked


async def test_no_desktop_picture_while_the_person_has_control(provider):
    instance, asked = provider
    instance.runtime.authority.acquire("human", "generation", 1)
    result = await instance.desktop_screenshot("run", "a", "generation")
    assert result["error_code"] == "HUMAN_HAS_CONTROL"
    assert asked == []


async def test_no_desktop_picture_of_a_replaced_screen(provider):
    instance, asked = provider
    result = await instance.desktop_screenshot("run", "a", "old-generation")
    assert result["error_code"] == "STALE_GENERATION"
    assert asked == []
