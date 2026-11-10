"""A file an app sends to a home that keeps no chats on the server is kept here (A11).

The server marks it ``keep`` on its own delivery; Core takes it into its media
dir before the turn, the agent reads it from there, and ``chat.send`` answers
with its media id: what lets the server delete its own copy. A file that
cannot be taken refuses the message, which comes again and then runs.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from flowly.media import kept_files
from flowly.media.kept_files import KeptFileError, download, keep_files

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
URL = "https://cdn.example.test/users/u1/servers/h1/c1/photo.png"


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    return home / "media"


def _fetch(body: bytes = PNG, seen: list | None = None):
    async def fetch(url: str, destination: Path, limit: int) -> None:
        if seen is not None:
            seen.append(url)
        destination.write_bytes(body)
    return fetch


async def test_a_file_sent_inline_is_kept_here_and_named_by_its_media_id(media_dir):
    out, ids = await keep_files([{"fileName": "photo.png", "mimeType": "image/png",
                                  "content": base64.b64encode(PNG).decode(), "keep": True}], media_dir)
    assert ids and isinstance(ids[0], str)
    kept = media_dir / ids[0]
    assert kept.read_bytes() == PNG
    # The agent reads it from disk; the bytes and the mark do not go further.
    assert out == [{"fileName": "photo.png", "mimeType": "image/png", "filePath": str(kept)}]


async def test_a_file_the_app_uploaded_is_fetched_from_where_the_server_put_it(media_dir):
    seen: list[str] = []
    out, ids = await keep_files([{"fileName": "photo.png", "mimeType": "image/png", "cdnUrl": URL, "keep": True}],
                                media_dir, fetch=_fetch(seen=seen))
    assert seen == [URL]
    assert (media_dir / ids[0]).read_bytes() == PNG
    assert out[0]["filePath"] == str(media_dir / ids[0]) and "cdnUrl" not in out[0]
    # Nothing half-written is left beside it.
    assert not [p for p in media_dir.iterdir() if p.name.startswith(".keep-")]


async def test_the_apps_read_a_kept_file_through_the_bridge_by_that_id(media_dir):
    from flowly.live_voice.authority import HOST_OWNER, request_owner_scope
    from flowly.media.serving import read_media_window

    _, ids = await keep_files([{"fileName": "photo.png", "mimeType": "image/png", "cdnUrl": URL, "keep": True}],
                              media_dir, fetch=_fetch())
    with request_owner_scope(HOST_OWNER):
        window = read_media_window(ids[0], offset=0, length=1024)
    assert window.ok and window.data == PNG and window.mime_type == "image/png"


async def test_a_file_that_cannot_be_taken_refuses_the_message_and_leaves_nothing_half_kept(media_dir):
    async def broken(url, destination, limit):
        destination.write_bytes(b"half")
        raise KeptFileError("the file answered 403")

    inline = {"fileName": "a.png", "mimeType": "image/png", "content": base64.b64encode(PNG).decode(), "keep": True}
    attachment = {"fileName": "photo.png", "mimeType": "image/png", "cdnUrl": URL, "keep": True}
    with pytest.raises(KeptFileError):
        await keep_files([inline, attachment], media_dir, fetch=broken)
    # The one taken before it went too: the message comes again, whole.
    assert not media_dir.exists() or not [p for p in media_dir.iterdir() if p.is_file()]


async def test_nothing_is_taken_when_nothing_asks(media_dir):
    attachments = [{"fileName": "a.png", "mimeType": "image/png", "cdnUrl": URL}]
    out, ids = await keep_files(attachments, media_dir, fetch=_fetch())
    assert out is attachments and ids is None
    # Only a true mark counts; any other is dropped, never followed.
    out, ids = await keep_files([{**attachments[0], "keep": "yes"}, {**attachments[0], "keep": True}], media_dir,
                                fetch=_fetch())
    assert ids[0] is None and out[0] == attachments[0]
    assert isinstance(ids[1], str)


async def test_the_download_takes_https_only_follows_no_redirect_and_stops_at_its_limit(tmp_path):
    with pytest.raises(KeptFileError):
        await download("http://cdn.example.test/a.png", tmp_path / "a")

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "https://elsewhere.test/a.png"})
        return httpx.Response(200, content=b"x" * 64)

    transport = httpx.MockTransport(answer)
    with pytest.raises(KeptFileError):
        await download("https://cdn.example.test/moved", tmp_path / "b", transport=transport)
    with pytest.raises(KeptFileError):
        await download("https://cdn.example.test/a.png", tmp_path / "c", 32, transport=transport)
    await download("https://cdn.example.test/a.png", tmp_path / "d", 64, transport=transport)
    assert (tmp_path / "d").read_bytes() == b"x" * 64


# The channel: chat.send keeps them, and says so in its answer.

class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _channel(monkeypatch):
    from flowly.bus.queue import MessageBus
    from flowly.channels.flowly_server import FlowlyServerChannel
    from flowly.config.schema import FlowlyServerChannelConfig

    config = FlowlyServerChannelConfig(enabled=True, server_url="wss://cloud.example.test/v1/agent",
                                       agent_secret="s3cret", agent_host_id="host-1")
    channel = FlowlyServerChannel(config, MessageBus())
    turns: list[list[str]] = []

    async def process(session_id, session_key, content, run_id, stream_callback=None, media=None,
                      voice_mode=False, render_capabilities=(), extra_metadata=None):
        turns.append(list(media or []))

    channel._process_message = process  # type: ignore[method-assign]
    monkeypatch.setattr(kept_files, "download", _fetch())
    return channel, turns


async def _send(channel, session: str, attachments: list, key: str = "m1") -> _Ws:
    ws = _Ws()
    await channel._dispatch_rpc(ws, {"type": "rpc", "id": "1", "sessionId": session, "method": "chat.send",
                                     "params": {"sessionKey": "web:c1", "idempotencyKey": key, "message": "look",
                                                "attachments": attachments}})
    await asyncio.sleep(0)
    return ws


async def test_the_servers_delivery_has_the_files_kept_and_hears_their_ids(media_dir, monkeypatch):
    from flowly.agent import inflight

    channel, turns = _channel(monkeypatch)
    channel.supports_turn_start = False
    ws = await _send(channel, "srv-default-c1", [
        {"fileName": "photo.png", "mimeType": "image/png", "cdnUrl": URL, "keep": True},
        {"fileName": "note.png", "mimeType": "image/png", "cdnUrl": URL},
    ])
    result = ws.sent[-1]["result"]
    assert result["runId"] == "m1"
    kept, passed = result["files"]
    assert isinstance(kept, str) and passed is None
    # The agent reads the kept one from this computer, the other where it is.
    assert turns[-1] == [str(media_dir / kept), URL]
    inflight.finish("web:c1", "m1")


async def test_an_apps_own_request_never_has_this_computer_fetch_anything(media_dir, monkeypatch):
    from flowly.agent import inflight

    channel, turns = _channel(monkeypatch)
    channel.supports_turn_start = False
    ws = await _send(channel, "c-123", [{"fileName": "photo.png", "mimeType": "image/png", "cdnUrl": URL, "keep": True}],
                     key="m2")
    assert "files" not in ws.sent[-1]["result"]
    assert turns[-1] == [URL]
    assert not media_dir.exists() or not [p for p in media_dir.iterdir() if p.is_file()]
    inflight.finish("web:c1", "m2")


async def test_a_message_whose_file_was_not_kept_is_refused_and_runs_when_it_comes_again(media_dir, monkeypatch):
    from flowly.agent import inflight

    channel, turns = _channel(monkeypatch)
    channel.supports_turn_start = False
    attachment = {"fileName": "photo.png", "mimeType": "image/png", "cdnUrl": URL, "keep": True}

    async def broken(url, destination, limit):
        raise KeptFileError("the file answered 503")

    monkeypatch.setattr(kept_files, "download", broken)
    ws = await _send(channel, "srv-default-c1", [attachment], key="m3")
    assert ws.sent[-1]["error"] == {"code": "FILE_NOT_KEPT", "retryable": True,
                                    "message": "A file in this message could not be taken onto your computer yet."}
    assert turns == [], "never run on the server's copy"

    monkeypatch.setattr(kept_files, "download", _fetch())
    ws = await _send(channel, "srv-default-c1", [attachment], key="m3")
    result = ws.sent[-1]["result"]
    assert result["runId"] == "m3" and result.get("replayed") is not True
    assert isinstance(result["files"][0], str)
    assert turns == [[str(media_dir / result["files"][0])]]
    inflight.finish("web:c1", "m3")
