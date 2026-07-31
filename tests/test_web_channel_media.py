"""Relay delivery for non-image media (flowly/channels/web.py).

The web channel used to walk the turn's media list and ``continue`` past
anything that wasn't an image. A generated clip therefore vanished between the
agent saying "here's your video" and the bubble the user actually saw — no
attachment, no error, nothing in the log.

Video now travels as a ``mediaId`` plus its poster, and the bytes stay on this
machine: clients that only reach the relay play the clip THROUGH the relay,
which bridges each playback window to this socket as a ``media.fetch`` request.
Nothing is uploaded anywhere, and no account is involved — delivery depends on
nothing but the bot being online.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.media.assets import ASSETS_META_KEY, MediaAsset, assets_to_meta


@pytest.fixture
def flowly_home(tmp_path, monkeypatch):
    """Isolate ~/.flowly so media resolution sees the test's own directory."""
    home = tmp_path / "home"
    (home / "media").mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    from flowly import profile

    if hasattr(profile, "_cached_home"):
        profile._cached_home = None
    yield home
    if hasattr(profile, "_cached_home"):
        profile._cached_home = None


@pytest.fixture
def channel():
    ch = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    capture: list[dict] = []

    async def fake_send_or_queue(payload: str) -> None:
        capture.append(json.loads(payload))

    ch._send_or_queue = fake_send_or_queue  # type: ignore[method-assign]
    ch._capture = capture
    return ch


class FakeWs:
    """Captures what the channel would send back to the relay."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


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


async def _send(channel, asset, path, **meta):
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


# ── attachment shape on the wire ────────────────────────────────────────────


async def test_video_is_delivered_by_media_id_with_no_account_involved(channel, tmp_path):
    """The whole point: delivery must not depend on a signed-in account."""
    path, asset = _video(tmp_path)

    data = await _send(channel, asset, path)

    atts = data["attachments"]
    assert len(atts) == 1
    att = atts[0]
    assert att["kind"] == "video"
    assert att["mimeType"] == "video/mp4"
    assert att["mediaId"] == "clip.mp4"
    assert att["durationMs"] == 6000
    assert (att["width"], att["height"]) == (1080, 1920)
    assert att["status"] == "ready"
    assert "cdnUrl" not in att, "nothing was uploaded, so nothing may claim to be hosted"


async def test_video_never_rides_the_websocket_as_base64(channel, tmp_path):
    path, asset = _video(tmp_path)

    data = await _send(channel, asset, path)

    blocks = data["message"]["content"]
    assert all(b["type"] == "text" for b in blocks)
    assert "base64" not in json.dumps(data["message"])


async def test_poster_travels_as_the_video_thumbnail(channel, tmp_path):
    from PIL import Image

    poster = tmp_path / "clip.jpg"
    Image.new("RGB", (640, 360), (12, 34, 56)).save(poster)
    path, asset = _video(tmp_path, poster=poster)

    data = await _send(channel, asset, path)

    assert base64.b64decode(data["attachments"][0]["thumbnail"])


async def test_local_paths_never_leak_onto_the_wire(channel, tmp_path):
    path, asset = _video(tmp_path)
    data = await _send(channel, asset, path)
    assert str(tmp_path) not in json.dumps(data)


async def test_images_keep_the_existing_base64_content_block(channel, tmp_path):
    """Regression guard: the image path clients use today must not move."""
    from PIL import Image

    img = tmp_path / "pic.png"
    Image.new("RGB", (64, 48), (7, 7, 7)).save(img)

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

    assert "attachments" not in data  # old wire shape, unchanged
    kinds = [b["type"] for b in data["message"]["content"]]
    assert "image" in kinds


async def test_text_only_turn_omits_the_attachments_field(channel):
    await channel.send(
        OutboundMessage(
            channel="web", chat_id="conv-1", content="No media here.", metadata={"run_id": "r"}
        )
    )
    assert "attachments" not in channel._capture[-1]["data"]


async def test_video_without_an_asset_entry_is_still_delivered(channel, tmp_path):
    """A tool that produced no descriptors must not lose its video."""
    path = tmp_path / "bare.mp4"
    path.write_bytes(b"\x00" * 128)

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
    assert att["mediaId"] == "bare.mp4"


async def test_remote_urls_are_left_alone(channel):
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


# ── media.fetch: the relay bridging a client's playback request ─────────────


async def _fetch(channel, ws, **params):
    await channel._serve_media_fetch(ws, {"type": "media.fetch", **params})
    return ws.sent[-1]


async def test_a_window_request_returns_one_result_frame(channel, flowly_home):
    data = bytes(range(256)) * 16  # 4096 bytes
    (flowly_home / "media" / "clip.mp4").write_bytes(data)
    ws = FakeWs()

    reply = await _fetch(
        channel, ws, requestId="r1", mediaId="clip.mp4", offset=256, length=512
    )

    assert reply["type"] == "media.result"
    assert reply["requestId"] == "r1"
    assert reply["ok"] is True
    assert reply["size"] == 4096
    assert reply["mimeType"] == "video/mp4"
    assert base64.b64decode(reply["data"]) == data[256:768]
    assert reply["eof"] is False
    assert len(ws.sent) == 1, "one window, ONE frame — the protocol is stateless"


async def test_the_last_window_reports_eof(channel, flowly_home):
    (flowly_home / "media" / "clip.mp4").write_bytes(b"\x00" * 100)
    ws = FakeWs()

    reply = await _fetch(channel, ws, requestId="r", mediaId="clip.mp4", offset=50, length=999)
    assert reply["ok"] is True
    assert len(base64.b64decode(reply["data"])) == 50
    assert reply["eof"] is True


async def test_a_zero_length_request_is_a_metadata_probe(channel, flowly_home):
    (flowly_home / "media" / "clip.mp4").write_bytes(b"\x00" * 4096)
    ws = FakeWs()

    reply = await _fetch(channel, ws, requestId="r", mediaId="clip.mp4", offset=0, length=0)
    assert reply["ok"] is True
    assert reply["size"] == 4096
    assert "data" not in reply, "a HEAD-shaped question gets a HEAD-shaped answer"


async def test_traversal_ids_are_answered_with_an_error_not_served(channel, flowly_home, tmp_path):
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"top secret")
    ws = FakeWs()

    for bad in ("../secret.mp4", "a/b.mp4", ".hidden.mp4", ""):
        reply = await _fetch(channel, ws, requestId="r", mediaId=bad, offset=0, length=100)
        assert reply["ok"] is False, bad
        assert "data" not in reply
    assert "top secret" not in json.dumps(ws.sent)


async def test_a_missing_file_is_an_error_the_relay_can_forward(channel, flowly_home):
    ws = FakeWs()
    reply = await _fetch(channel, ws, requestId="r", mediaId="gone.mp4", offset=0, length=100)
    assert reply == {"type": "media.result", "requestId": "r", "ok": False, "error": "not_found"}


async def test_non_media_files_are_refused(channel, flowly_home):
    (flowly_home / "media" / "notes.txt").write_text("hello")
    ws = FakeWs()
    reply = await _fetch(channel, ws, requestId="r", mediaId="notes.txt", offset=0, length=100)
    assert reply["ok"] is False
    assert reply["error"] == "unsupported_type"


async def test_a_request_with_no_id_gets_no_reply(channel, flowly_home):
    """Nothing to correlate a reply to — answering would just confuse the relay."""
    ws = FakeWs()
    await channel._serve_media_fetch(ws, {"type": "media.fetch", "mediaId": "clip.mp4"})
    assert ws.sent == []


async def test_oversized_window_requests_are_clamped(channel, flowly_home):
    """The reply must always fit one relay frame, whatever the relay asked for."""
    from flowly.media.serving import MAX_WINDOW_BYTES

    (flowly_home / "media" / "big.mp4").write_bytes(b"\x00" * (MAX_WINDOW_BYTES + 1024))
    ws = FakeWs()

    reply = await _fetch(
        channel, ws, requestId="r", mediaId="big.mp4", offset=0, length=MAX_WINDOW_BYTES * 10
    )
    assert reply["ok"] is True
    assert len(base64.b64decode(reply["data"])) == MAX_WINDOW_BYTES
    assert reply["eof"] is False


async def test_the_dispatch_loop_serves_media_fetch_without_blocking(channel, flowly_home):
    """The handler runs as a task off the receive loop; the reply still lands."""
    (flowly_home / "media" / "clip.mp4").write_bytes(b"\x00" * 64)
    ws = FakeWs()

    await channel._handle_relay_message(
        ws,
        {"type": "media.fetch", "requestId": "r9", "mediaId": "clip.mp4", "offset": 0, "length": 64},
    )
    # The work happens on a spawned task — give it a beat.
    for _ in range(50):
        if ws.sent:
            break
        await asyncio.sleep(0.01)

    assert ws.sent and ws.sent[0]["requestId"] == "r9"
    assert ws.sent[0]["ok"] is True
