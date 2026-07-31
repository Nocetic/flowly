"""Relay delivery for non-image media (flowly/channels/web.py).

The web channel used to walk the turn's media list and ``continue`` past
anything that wasn't an image. A generated clip therefore vanished between the
agent saying "here's your video" and the bubble the user actually saw — no
attachment, no error, nothing in the log.

Video now takes the route a user's own attachment already takes: hosted upload,
then a ``cdnUrl`` on the wire. What this file pins is that it can no longer
disappear — delivered as an attachment when the upload works, and marked
``failed`` when it doesn't.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.media.assets import ASSETS_META_KEY, MediaAsset, assets_to_meta


@pytest.fixture
def channel():
    ch = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    capture: list[dict] = []

    async def fake_send_or_queue(payload: str) -> None:
        capture.append(json.loads(payload))

    ch._send_or_queue = fake_send_or_queue  # type: ignore[method-assign]
    ch._capture = capture
    return ch


ACCOUNT = SimpleNamespace(id_token="tok", server_id="srv_1")


def _video(tmp_path, name="clip.mp4", *, poster=None, duration_ms=6000):
    path = tmp_path / name
    path.write_bytes(b"\x00" * 4096)
    return path, MediaAsset(
        path=str(path),
        kind="video",
        file_name=name,
        mime_type="video/mp4",
        size=4096,
        width=1080,
        height=1920,
        duration_ms=duration_ms,
        poster_path=str(poster) if poster else None,
        id="media_clip",
    )


def _patch_upload(monkeypatch, *, result=None, error=None, account=ACCOUNT):
    import flowly.account.auth as auth
    import flowly.media.hosted as hosted

    async def fake_load():
        return account

    async def fake_upload(path, *, account, conversation_id, client=None):
        if error is not None:
            raise error
        return result or {
            "cdnUrl": f"https://cdn.example/{path.name}",
            "fileName": path.name,
            "mimeType": "video/mp4",
            "size": path.stat().st_size,
            "s3Key": f"users/u/{path.name}",
        }

    monkeypatch.setattr(auth, "load_account_refreshing", fake_load)
    monkeypatch.setattr(hosted, "upload_media", fake_upload)


async def _send(channel, tmp_path, asset, path, **meta):
    await channel.send(
        OutboundMessage(
            channel="web",
            chat_id="conv-1",
            content="Here is the clip.",
            media=[str(path)],
            metadata={"run_id": "run-1", ASSETS_META_KEY: assets_to_meta([asset]), **meta},
        )
    )
    return channel._capture[-1]["data"]


async def test_video_is_delivered_as_a_hosted_attachment(channel, tmp_path, monkeypatch):
    path, asset = _video(tmp_path)
    _patch_upload(monkeypatch)

    data = await _send(channel, tmp_path, asset, path)

    atts = data["attachments"]
    assert len(atts) == 1
    att = atts[0]
    assert att["kind"] == "video"
    assert att["mimeType"] == "video/mp4"
    assert att["cdnUrl"] == "https://cdn.example/clip.mp4"
    assert att["durationMs"] == 6000
    assert (att["width"], att["height"]) == (1080, 1920)
    assert att["s3Key"] == "users/u/clip.mp4"
    assert att["status"] == "ready"


async def test_video_never_rides_the_websocket_as_base64(channel, tmp_path, monkeypatch):
    """The whole reason hosted upload exists — a clip must not inflate a frame."""
    path, asset = _video(tmp_path)
    _patch_upload(monkeypatch)

    data = await _send(channel, tmp_path, asset, path)

    blocks = data["message"]["content"]
    assert all(b["type"] == "text" for b in blocks)
    assert "base64" not in json.dumps(data["message"])


async def test_poster_travels_as_the_video_thumbnail(channel, tmp_path, monkeypatch):
    from PIL import Image

    poster = tmp_path / "clip.jpg"
    Image.new("RGB", (640, 360), (12, 34, 56)).save(poster)
    path, asset = _video(tmp_path, poster=poster)
    _patch_upload(monkeypatch)

    data = await _send(channel, tmp_path, asset, path)

    import base64

    assert base64.b64decode(data["attachments"][0]["thumbnail"])


async def test_upload_failure_surfaces_instead_of_dropping_the_clip(
    channel, tmp_path, monkeypatch
):
    import flowly.media.hosted as hosted

    path, asset = _video(tmp_path)
    _patch_upload(monkeypatch, error=hosted.HostedUploadError("bucket on fire"))

    data = await _send(channel, tmp_path, asset, path)

    att = data["attachments"][0]
    assert att["status"] == "failed"
    assert att["kind"] == "video"
    assert "cdnUrl" not in att
    # The provider-side reason must not be smuggled onto the wire.
    assert "bucket on fire" not in json.dumps(data)


async def test_signed_out_user_gets_a_failed_attachment_not_silence(
    channel, tmp_path, monkeypatch
):
    path, asset = _video(tmp_path)
    _patch_upload(monkeypatch, account=None)

    data = await _send(channel, tmp_path, asset, path)

    assert data["attachments"][0]["status"] == "failed"


async def test_images_keep_the_existing_base64_content_block(channel, tmp_path, monkeypatch):
    """Regression guard: the image path clients use today must not move."""
    from PIL import Image

    img = tmp_path / "pic.png"
    Image.new("RGB", (64, 48), (7, 7, 7)).save(img)

    called = False

    async def fake_upload(*_a, **_k):
        nonlocal called
        called = True
        raise AssertionError("images must not go through hosted upload")

    import flowly.media.hosted as hosted

    monkeypatch.setattr(hosted, "upload_media", fake_upload)

    await channel.send(
        OutboundMessage(
            channel="web",
            chat_id="conv-1",
            content="Here is the picture.",
            media=[str(img)],
            metadata={"run_id": "run-1"},
        )
    )
    data = channel._capture[-1]["data"]

    assert called is False
    assert "attachments" not in data  # old wire shape, unchanged
    kinds = [b["type"] for b in data["message"]["content"]]
    assert "image" in kinds


async def test_text_only_turn_omits_the_attachments_field(channel, tmp_path):
    await channel.send(
        OutboundMessage(
            channel="web", chat_id="conv-1", content="No media here.", metadata={"run_id": "r"}
        )
    )
    assert "attachments" not in channel._capture[-1]["data"]


async def test_video_without_an_asset_entry_is_still_uploaded(channel, tmp_path, monkeypatch):
    """A tool that produced no descriptors must not lose its video."""
    path = tmp_path / "bare.mp4"
    path.write_bytes(b"\x00" * 128)
    _patch_upload(monkeypatch)

    await channel.send(
        OutboundMessage(
            channel="web",
            chat_id="conv-1",
            content="clip",
            media=[str(path)],
            metadata={"run_id": "r"},
        )
    )
    att = channel._capture[-1]["data"]["attachments"][0]
    assert att["kind"] == "video"
    assert att["cdnUrl"].endswith("bare.mp4")


async def test_remote_urls_are_left_alone(channel, tmp_path, monkeypatch):
    async def fake_upload(*_a, **_k):
        raise AssertionError("a remote URL is already hosted")

    import flowly.media.hosted as hosted

    monkeypatch.setattr(hosted, "upload_media", fake_upload)

    await channel.send(
        OutboundMessage(
            channel="web",
            chat_id="conv-1",
            content="x",
            media=["https://cdn.example/already.mp4"],
            metadata={"run_id": "r"},
        )
    )
    assert "attachments" not in channel._capture[-1]["data"]
