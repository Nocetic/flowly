"""Tests for MCP image-content caching (E3).

We confirm a base64 ImageContent block is decoded to a file under
``$FLOWLY_HOME/media/mcp/`` and surfaced as a ``MEDIA:<abspath>`` token,
and that malformed / non-image blocks return ``None`` without raising.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from flowly.mcp.media_cache import cache_image_block, cache_media_block


class _Block:
    def __init__(self, data=None, mimeType=None, text=None):
        if data is not None:
            self.data = data
        if mimeType is not None:
            self.mimeType = mimeType
        if text is not None:
            self.text = text


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


def test_caches_png_and_returns_media_tag(isolated_home: Path):
    # 1x1 transparent PNG
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    block = _Block(data=base64.b64encode(png_bytes).decode(), mimeType="image/png")
    tag = cache_image_block(block)
    assert tag is not None
    assert tag.startswith("MEDIA:")
    path = Path(tag[len("MEDIA:"):])
    assert path.exists()
    assert path.read_bytes() == png_bytes
    assert path.suffix == ".png"
    assert path.parent == isolated_home / "media" / "mcp"


def test_jpeg_extension(isolated_home: Path):
    block = _Block(data=base64.b64encode(b"\xff\xd8\xff\xe0jpegish").decode(), mimeType="image/jpeg")
    tag = cache_image_block(block)
    assert tag is not None
    assert Path(tag[len("MEDIA:"):]).suffix == ".jpg"


def test_non_image_block_returns_none(isolated_home: Path):
    assert cache_image_block(_Block(text="hello")) is None
    assert cache_image_block(_Block(data="x", mimeType="text/plain")) is None


def test_malformed_base64_returns_none(isolated_home: Path):
    block = _Block(data="!!!not base64!!!", mimeType="image/png")
    # base64 is lenient; an empty/garbage decode should yield None safely.
    result = cache_image_block(block)
    assert result is None


def test_audio_is_cached_with_media_metadata(isolated_home: Path):
    raw = b"RIFF-audio"
    block = _Block(data=base64.b64encode(raw).decode(), mimeType="audio/wav")
    cached = cache_media_block(block)
    assert cached is not None
    assert cached.path.suffix == ".wav"
    assert cached.size == len(raw)
    assert cached.media_tag.startswith("MEDIA:")
    assert cached.path.read_bytes() == raw


def test_cache_is_deduplicated_and_private(isolated_home: Path):
    block = _Block(data=base64.b64encode(b"same").decode(), mimeType="image/png")
    first = cache_media_block(block)
    second = cache_media_block(block)
    assert first is not None and second is not None
    assert first.path == second.path
    assert first.path.stat().st_mode & 0o077 == 0


def test_size_limit_rejects_before_writing(isolated_home: Path):
    block = _Block(data=base64.b64encode(b"12345").decode(), mimeType="image/png")
    assert cache_media_block(block, max_bytes=4) is None
    assert not (isolated_home / "media" / "mcp").exists()
