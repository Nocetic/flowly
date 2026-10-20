"""A named bot's screen wears its avatar colour; the main agent's stays black.

The colour is the avatar body colour clients draw (Desktop's
``profileAvatarColor``): a chosen colour with its lightness kept between 34%
and 90%; otherwise the palette's leading colour (saturation 78%, lightness
58%) at a former named tone's hue or at the hue walked from the creation seed
(or hashed from the name when a profile predates the seed).
"""

from __future__ import annotations

import math
import re
from pathlib import Path

_HEX = re.compile(r"^#[0-9a-f]{6}$")
# Desktop's former named tones (ProfileMark.tsx PROFILE_MARK_TONES).
_TONE_HUES = {"aqua": 188, "violet": 266, "rose": 338, "amber": 34, "lime": 102, "sky": 216, "slate": 226}
_HUE_ORIGIN = 188
_GOLDEN_ANGLE = 137.508


def _name_hue(name: str) -> int:
    """Desktop's string hash: JavaScript int32 arithmetic over UTF-16 units."""
    value = 0
    units = name.encode("utf-16-le")
    for index in range(0, len(units), 2):
        value = (((value << 5) - value + int.from_bytes(units[index:index + 2], "little")) + 2 ** 31) % 2 ** 32 - 2 ** 31
    return ((abs(value) % 300) + 175) % 360


def _hsl_to_hex(hue: float, saturation: float, lightness: float) -> str:
    hue = (hue % 360) / 360
    saturation = min(100.0, max(0.0, saturation)) / 100
    lightness = min(100.0, max(0.0, lightness)) / 100
    if saturation == 0:
        channels = [lightness] * 3
    else:
        q = lightness * (1 + saturation) if lightness < 0.5 else lightness + saturation - lightness * saturation
        p = 2 * lightness - q

        def channel(t: float) -> float:
            t = t + 1 if t < 0 else t - 1 if t > 1 else t
            if t < 1 / 6:
                return p + (q - p) * 6 * t
            if t < 1 / 2:
                return q
            if t < 2 / 3:
                return p + (q - p) * (2 / 3 - t) * 6
            return p

        channels = [channel(hue + 1 / 3), channel(hue), channel(hue - 1 / 3)]
    # Math.round, not Python's half-to-even round: the same hex as Desktop.
    return "#" + "".join("%02x" % math.floor(min(255.0, max(0.0, value * 255)) + 0.5) for value in channels)


def _hex_to_hsl(value: str) -> tuple[float, float, float]:
    red, green, blue = (int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))
    maximum, minimum = max(red, green, blue), min(red, green, blue)
    delta = maximum - minimum
    lightness = (maximum + minimum) / 2
    if delta == 0:
        return 0.0, 0.0, lightness * 100
    saturation = delta / (1 - abs(2 * lightness - 1))
    if maximum == red:
        hue = 60 * (((green - blue) / delta) % 6)
    elif maximum == green:
        hue = 60 * ((blue - red) / delta + 2)
    else:
        hue = 60 * ((red - green) / delta + 4)
    return (hue + 360) % 360, saturation * 100, lightness * 100


def avatar_color(name: str, mark_tone: str = "", seed: int | None = None) -> str:
    tone = (mark_tone or "").strip().lower()
    if _HEX.fullmatch(tone):
        hue, saturation, lightness = _hex_to_hsl(tone)
        return _hsl_to_hex(hue, saturation, min(90.0, max(34.0, lightness)))
    if tone in _TONE_HUES:
        hue = _TONE_HUES[tone]
    elif isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0:
        hue = (_HUE_ORIGIN + seed * _GOLDEN_ANGLE) % 360
    else:
        hue = _HUE_ORIGIN if name == "default" else _name_hue(name)
    return _hsl_to_hex(hue, 78, 58)


def screen_color(directory: Path) -> str | None:
    """The avatar colour of the named profile owning this screen directory,
    or None for the main agent (and any home that is not a named profile)."""
    from flowly.profile import _PROFILE_NAME_RE, _PROFILES_ROOT, _profile_metadata, _read_mark_seed

    home = directory.parent
    if home.parent != _PROFILES_ROOT or not _PROFILE_NAME_RE.fullmatch(home.name):
        return None
    metadata = _profile_metadata(home)
    tone = metadata.get("markTone")
    return avatar_color(home.name, tone if isinstance(tone, str) else "", _read_mark_seed(metadata.get("markSeed")))


# Run with the application account inside its X session: paint the root
# window one solid colour. A solid root is what a remote screen encodes
# cheapest. libX11 is present wherever the desktop's X clients run.
ROOT_COLOR_SOURCE = r"""
import ctypes
import re
import sys

value = sys.argv[1]
if not re.fullmatch(r"#[0-9a-f]{6}", value):
    raise SystemExit(2)
x = ctypes.CDLL("libX11.so.6")
x.XOpenDisplay.restype = ctypes.c_void_p
x.XOpenDisplay.argtypes = [ctypes.c_char_p]
x.XDefaultScreen.argtypes = [ctypes.c_void_p]
x.XRootWindow.restype = ctypes.c_ulong
x.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
x.XSetWindowBackground.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
x.XClearWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
x.XCloseDisplay.argtypes = [ctypes.c_void_p]
display = x.XOpenDisplay(None)
if not display:
    raise SystemExit(1)
root = x.XRootWindow(display, x.XDefaultScreen(display))
# The runtime's Xvnc is 24-bit TrueColor: the pixel is the RGB value.
x.XSetWindowBackground(display, root, int(value[1:], 16))
x.XClearWindow(display, root)
x.XSync(display, 0)
x.XCloseDisplay(display)
"""
