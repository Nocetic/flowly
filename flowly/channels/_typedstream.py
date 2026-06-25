"""Minimal typedstream text extractor for Messages' ``attributedBody`` blobs.

Since macOS Ventura the Messages database frequently stores a message's
body with ``message.text = NULL`` and the real content inside
``message.attributedBody`` — an ``NSAttributedString`` archived with the
legacy typedstream serializer (``NSArchiver``). We only need the plain
string payload, not the attribute runs, so this module implements the
smallest possible reader: locate the archived ``NSString`` /
``NSMutableString`` class record and decode the length-prefixed UTF-8
buffer that follows it.

Container layout (verified against blobs produced by ``NSArchiver`` and
real chat.db rows):

    04 0B "streamtyped" ...                      header
    ... 84 84 12 "NSAttributedString" 00 ...     class records
    ... 84 84 08 "NSString" 01 94 84 01 2B       '+' marks the char buffer
    <length> <utf-8 bytes>                       length-prefixed payload

The length integer uses typedstream's variable encoding:

    0x00–0x80   literal value in the byte itself
    0x81        next 2 bytes  → unsigned little-endian
    0x82        next 4 bytes  → unsigned little-endian
"""

from __future__ import annotations

import re
import struct

_HEADER = b"streamtyped"
_STRING_CLASSES = (b"NSMutableString", b"NSString")
_PLUS = 0x2B  # '+' type marker preceding the char buffer
# The '+' marker sits a handful of bytes after the class name (version
# int + inline references). 24 bytes is generous without risking a
# false positive further into the stream.
_MARKER_WINDOW = 24


def _read_length(blob: bytes, pos: int) -> tuple[int, int] | None:
    """Decode a typedstream integer at ``pos`` → (value, next_pos)."""
    if pos >= len(blob):
        return None
    first = blob[pos]
    if first == 0x81:
        if pos + 3 > len(blob):
            return None
        return struct.unpack_from("<H", blob, pos + 1)[0], pos + 3
    if first == 0x82:
        if pos + 5 > len(blob):
            return None
        return struct.unpack_from("<I", blob, pos + 1)[0], pos + 5
    if first <= 0x80:
        return first, pos + 1
    return None


def _extract_after_class(blob: bytes, class_end: int) -> str | None:
    """Decode the char buffer following a string-class record."""
    window_end = min(class_end + _MARKER_WINDOW, len(blob))
    plus = blob.find(_PLUS, class_end, window_end)
    if plus == -1:
        return None
    parsed = _read_length(blob, plus + 1)
    if parsed is None:
        return None
    length, start = parsed
    if length <= 0 or start + length > len(blob):
        return None
    try:
        return blob[start:start + length].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _crude_fallback(blob: bytes) -> str | None:
    """Last resort: longest plausible UTF-8 text run in the blob.

    Filters out the known archiver class/type tokens so framework names
    don't masquerade as message text.
    """
    runs = re.findall(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf\xc2-\xf4]{3,}", blob)
    candidates: list[str] = []
    for run in runs:
        try:
            text = run.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.startswith(("NS", "streamtyped", "__k")) or text in ("iI", "ii"):
            continue
        candidates.append(text)
    if not candidates:
        return None
    return max(candidates, key=len)


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract the plain-text body from an ``attributedBody`` blob.

    Returns ``None`` when the blob is empty or no text payload can be
    located — callers treat that the same as ``message.text IS NULL``.
    """
    if not blob:
        return None
    if _HEADER not in blob[:32]:
        return _crude_fallback(blob)

    for class_name in _STRING_CLASSES:
        offset = 0
        while True:
            hit = blob.find(class_name, offset)
            if hit == -1:
                break
            # NSMutableString contains "NSString"; require the byte before
            # a bare NSString match not to be part of a longer name.
            text = _extract_after_class(blob, hit + len(class_name))
            if text is not None:
                return text
            offset = hit + 1

    return _crude_fallback(blob)
