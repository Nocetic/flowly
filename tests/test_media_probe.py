"""Media probing must degrade, never raise (flowly/media/probe.py).

ffmpeg/ffprobe are NOT Flowly dependencies. A machine without them must still
generate, deliver and play video — it just loses the poster frame and the
duration hint. These tests pin both halves: the real probe when the binaries
exist, and the graceful "unknown" answer when they don't.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from flowly.media import probe as probe_mod

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
requires_ffprobe = pytest.mark.skipif(not HAS_FFPROBE, reason="ffprobe not installed")


def _make_video(path, *, seconds=1, width=320, height=240):
    """Render a tiny real MP4 with ffmpeg so probing has something to chew on."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=10",
            "-t", str(seconds), "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return path


def test_probe_image_reads_dimensions(tmp_path):
    from PIL import Image

    p = tmp_path / "a.png"
    Image.new("RGB", (33, 17), (5, 5, 5)).save(p)
    result = probe_mod.probe_image(p)
    assert (result.width, result.height) == (33, 17)


def test_probe_image_on_garbage_returns_empty(tmp_path):
    p = tmp_path / "not-an-image.png"
    p.write_bytes(b"definitely not a png")
    assert probe_mod.probe_image(p) == probe_mod.MediaProbe()


@requires_ffmpeg
@requires_ffprobe
def test_probe_video_reads_dimensions_and_duration(tmp_path):
    p = _make_video(tmp_path / "clip.mp4", seconds=1, width=320, height=240)
    result = probe_mod.probe_video(p)
    assert (result.width, result.height) == (320, 240)
    assert result.duration_ms is not None
    assert 500 <= result.duration_ms <= 2000  # ~1s, container rounding tolerated


@requires_ffmpeg
def test_extract_poster_produces_a_real_jpeg(tmp_path):
    from PIL import Image

    video = _make_video(tmp_path / "clip.mp4", seconds=2, width=320, height=240)
    poster = tmp_path / "clip.jpg"
    assert probe_mod.extract_poster(video, poster) is True
    assert poster.stat().st_size > 0
    with Image.open(poster) as img:
        assert img.format == "JPEG"


@requires_ffmpeg
def test_extract_poster_retries_from_frame_zero_for_ultra_short_clips(tmp_path):
    """A clip shorter than the seek offset must still yield a poster."""
    video = _make_video(tmp_path / "blink.mp4", seconds=1, width=160, height=120)
    poster = tmp_path / "blink.jpg"
    assert probe_mod.extract_poster(video, poster) is True
    assert poster.stat().st_size > 0


def test_probe_video_without_ffprobe_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_mod.shutil, "which", lambda _name: None)
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 32)
    assert probe_mod.probe_video(p) == probe_mod.MediaProbe()


def test_extract_poster_without_ffmpeg_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_mod.shutil, "which", lambda _name: None)
    poster = tmp_path / "out.jpg"
    assert probe_mod.extract_poster(tmp_path / "clip.mp4", poster) is False
    assert not poster.exists()


def test_probe_video_survives_a_broken_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_mod.shutil, "which", lambda _name: "/usr/bin/ffprobe")

    def _boom(*_args, **_kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(probe_mod.subprocess, "run", _boom)
    assert probe_mod.probe_video(tmp_path / "clip.mp4") == probe_mod.MediaProbe()


def test_probe_video_survives_garbage_ffprobe_output(tmp_path, monkeypatch):
    monkeypatch.setattr(probe_mod.shutil, "which", lambda _name: "/usr/bin/ffprobe")

    class _Result:
        returncode = 0
        stdout = "not json at all"
        stderr = ""

    monkeypatch.setattr(probe_mod.subprocess, "run", lambda *a, **k: _Result())
    assert probe_mod.probe_video(tmp_path / "clip.mp4") == probe_mod.MediaProbe()


def test_probe_dispatches_by_mime(tmp_path):
    from PIL import Image

    img = tmp_path / "a.png"
    Image.new("RGB", (10, 10)).save(img)
    assert probe_mod.probe(img, "image/png").width == 10
    # An unknown kind is never shelled out for.
    assert probe_mod.probe(img, "application/pdf") == probe_mod.MediaProbe()
