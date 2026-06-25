"""The direct gateway WS path must surface reply media as fetchable attachments.

``_execute_chat`` (live final) and the ``chat.history`` RPC both build a
message's ``attachments`` via ``_reply_media_attachments`` — local files carry an
inline base64 ``thumbnail`` (renders with no fetch, since there is no relay/S3 on
this path) plus a ``mediaId`` for full-res via ``GET /api/media?id=…``; remote
URLs pass through as ``cdnUrl``. This is what makes generated media
(image_generate / screenshot) show in the reply over a remote gateway
(iOS / desktop direct WS).
"""

from __future__ import annotations

from flowly.gateway.server import _reply_media_attachments


def test_missing_file_is_skipped(tmp_path):
    from PIL import Image

    real = tmp_path / "real.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(real)
    atts = _reply_media_attachments([str(real), "/no/such/file.png"])
    names = [a.get("fileName") for a in atts]
    assert names == ["real.png"]  # the missing path is dropped, not handed to the client


def test_reply_media_local_carries_inline_thumbnail(tmp_path):
    import base64

    from PIL import Image

    p = tmp_path / "img-xyz.png"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(p)
    att = _reply_media_attachments([str(p)])[0]
    assert att["fileName"] == "img-xyz.png"
    assert att["mediaId"] == "img-xyz.png"
    assert att["mimeType"].startswith("image/")
    # inline base64 preview — what a remote (iOS / desktop) renders with no fetch
    assert base64.b64decode(att["thumbnail"])  # decodes to non-empty bytes


def test_reply_media_thumbnail_is_small_for_large_image(tmp_path):
    """The inline thumbnail must stay a lightweight preview — a large original is
    downscaled (≤512 px) and re-encoded so history doesn't ship megabytes of
    base64; the full-res original is served separately via mediaId."""
    import base64
    import io

    from PIL import Image

    # A big, noisy original (noise resists JPEG compression → forces a resize).
    p = tmp_path / "big.png"
    import os
    Image.frombytes("RGB", (2000, 1500), os.urandom(2000 * 1500 * 3)).save(p)
    assert p.stat().st_size > 48 * 1024  # original is well over the thumbnail budget

    att = _reply_media_attachments([str(p)])[0]
    raw = base64.b64decode(att["thumbnail"])
    assert len(raw) <= 80 * 1024  # small inline preview (budget + JPEG slack)
    with Image.open(io.BytesIO(raw)) as thumb:
        assert max(thumb.size) <= 512  # downscaled to the thumbnail dimension
    assert att["mediaId"] == "big.png"  # full-res still reachable via /api/media


def test_reply_media_remote_url_passes_through(tmp_path):
    att = _reply_media_attachments(["https://fal.media/x.png"])[0]
    assert att["cdnUrl"] == "https://fal.media/x.png"
    assert "thumbnail" not in att and "mediaId" not in att


def test_reply_media_unreadable_file_skipped():
    assert _reply_media_attachments(["/no/such/img.png"]) == []


# ── thumbnail cache: chat.history reloads must not re-compress the same file ──

def test_thumbnail_cache_avoids_recompute(tmp_path, monkeypatch):
    from PIL import Image

    import flowly.channels.web as web
    import flowly.gateway.server as server

    p = tmp_path / "cache.png"
    Image.new("RGB", (900, 700), (10, 20, 30)).save(p)
    server._THUMB_CACHE.clear()

    first = server._thumbnail_b64(p)
    assert first is not None and first[0]  # (base64, mime)

    # A second build (e.g. another chat.history reload) must serve from cache —
    # blow up if the compressor is touched again for the same path+mtime+size.
    def _boom(*args, **kwargs):
        raise AssertionError("re-compressed despite a warm cache")

    monkeypatch.setattr(web, "_compress_image_for_transport", _boom)
    assert server._thumbnail_b64(p) == first


def test_thumbnail_cache_busts_on_mtime(tmp_path):
    import os

    from PIL import Image

    import flowly.gateway.server as server

    p = tmp_path / "edited.png"
    Image.new("RGB", (900, 700), (1, 1, 1)).save(p)
    server._THUMB_CACHE.clear()
    server._thumbnail_b64(p)
    n_before = len(server._THUMB_CACHE)

    # Edit in place (same path) with a bumped mtime → new key, recomputed entry.
    Image.new("RGB", (900, 700), (250, 250, 250)).save(p)
    st = p.stat()
    os.utime(p, (st.st_atime + 10, st.st_mtime + 10))
    server._thumbnail_b64(p)
    assert len(server._THUMB_CACHE) == n_before + 1
