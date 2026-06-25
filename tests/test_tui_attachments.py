from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from flowly.gateway.server import _save_attachments
from flowly.tui import clipboard as clipboard_mod
from flowly.tui import media_upload
from flowly.tui.attachments import (
    build_attachment,
    detect_image_drop,
    detect_media_drop,
    detect_video_drop,
    render_message_with_attachments,
)
from flowly.tui.client import GatewayClient
from flowly.tui.panes.composer import Composer


def _image(path: Path) -> Path:
    path.write_bytes(b"not really an image, only a parser fixture")
    return path


def _video(path: Path) -> Path:
    path.write_bytes(b"not really a video, only a parser fixture")
    return path


def test_detect_image_drop_absolute_path_with_remainder(tmp_path: Path) -> None:
    image = _image(tmp_path / "cat.png")

    drop = detect_image_drop(f"{image} what is this?")

    assert drop is not None
    assert drop.path == image
    assert drop.remainder == "what is this?"


def test_detect_image_drop_quoted_path_with_spaces(tmp_path: Path) -> None:
    image = _image(tmp_path / "my image.jpg")

    drop = detect_image_drop(f'"{image}" describe it')

    assert drop is not None
    assert drop.path == image
    assert drop.remainder == "describe it"


def test_detect_image_drop_ignores_slash_commands() -> None:
    assert detect_image_drop("/help") is None


def test_detect_image_drop_allows_bare_filename_when_explicit(tmp_path: Path) -> None:
    image = _image(tmp_path / "local.png")

    drop = detect_image_drop("local.png", base_dir=tmp_path, allow_bare=True)

    assert drop is not None
    assert drop.path == image


def test_detect_image_drop_accepts_heic(tmp_path: Path) -> None:
    image = _image(tmp_path / "photo.heic")

    drop = detect_image_drop(str(image))

    assert drop is not None
    assert drop.path == image
    assert drop.kind == "image"


def test_detect_media_drop_accepts_video(tmp_path: Path) -> None:
    video = _video(tmp_path / "clip.mov")

    drop = detect_media_drop(f"{video} what happens?")

    assert drop is not None
    assert drop.path == video
    assert drop.kind == "video"
    assert drop.remainder == "what happens?"


def test_detect_video_drop_rejects_images(tmp_path: Path) -> None:
    image = _image(tmp_path / "photo.png")

    assert detect_video_drop(str(image)) is None


def test_build_attachment_uses_local_file_path(tmp_path: Path) -> None:
    image = _image(tmp_path / "screen.webp")

    payload = build_attachment(image)

    assert payload["filePath"] == str(image)
    assert payload["fileName"] == "screen.webp"
    assert payload["mimeType"] == "image/webp"


def test_gateway_save_attachments_accepts_cdn_url(tmp_path: Path) -> None:
    url = "https://cdn.useflowlyapp.com/users/u/servers/s/conversations/c/clip.mp4"

    paths = _save_attachments([{"cdnUrl": url}], tmp_path)

    assert paths == [url]


@pytest.mark.asyncio
async def test_prepare_video_attachment_requires_login_when_large(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = _video(tmp_path / "large.mp4")
    video.write_bytes(b"x" * 11)
    monkeypatch.setattr(media_upload, "MAX_SIGNED_OUT_INLINE_VIDEO_BYTES", 10)

    with pytest.raises(media_upload.MediaUploadAuthRequired) as exc:
        await media_upload.prepare_media_attachments(
            [video],
            account=None,
            conversation_id="cli:test",
        )

    assert "/login" in str(exc.value)


@pytest.mark.asyncio
async def test_prepare_video_attachment_uploads_when_signed_in(tmp_path: Path) -> None:
    video = _video(tmp_path / "clip.mp4")
    account = SimpleNamespace(id_token="token", server_id="srv_123")
    captured: dict[str, Any] = {}

    async def fake_upload(path: Path, account_arg: Any, conversation_id: str) -> dict[str, Any]:
        captured["path"] = path
        captured["account"] = account_arg
        captured["conversation_id"] = conversation_id
        return {
            "cdnUrl": "https://cdn.useflowlyapp.com/clip.mp4",
            "fileName": "clip.mp4",
            "mimeType": "video/mp4",
            "size": 123,
        }

    payload = await media_upload.prepare_media_attachments(
        [video],
        account=account,
        conversation_id="cli:test",
        upload=fake_upload,
    )

    assert payload == [{
        "cdnUrl": "https://cdn.useflowlyapp.com/clip.mp4",
        "fileName": "clip.mp4",
        "mimeType": "video/mp4",
        "size": 123,
    }]
    assert captured == {
        "path": video,
        "account": account,
        "conversation_id": "cli:test",
    }


@pytest.mark.asyncio
async def test_prepare_small_signed_out_video_falls_back_to_local_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = _video(tmp_path / "small.mp4")
    video.write_bytes(b"x" * 5)
    monkeypatch.setattr(media_upload, "MAX_SIGNED_OUT_INLINE_VIDEO_BYTES", 10)

    payload = await media_upload.prepare_media_attachments(
        [video],
        account=None,
        conversation_id="cli:test",
    )

    assert payload == [{
        "filePath": str(video),
        "fileName": "small.mp4",
        "mimeType": "video/mp4",
    }]


@pytest.mark.asyncio
async def test_upload_media_posts_authenticated_multipart(tmp_path: Path) -> None:
    video = _video(tmp_path / "clip.mp4")
    account = SimpleNamespace(id_token="token", server_id="srv_123")
    captured: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {
                "cdnUrl": "https://cdn.useflowlyapp.com/clip.mp4",
                "fileName": "clip.mp4",
                "mimeType": "video/mp4",
                "size": video.stat().st_size,
            }

    class FakeClient:
        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    payload = await media_upload.upload_media(
        video,
        account=account,
        conversation_id="cli:test",
        client=FakeClient(),
    )

    assert captured["headers"] == {"Authorization": "Bearer token"}
    assert captured["data"] == {"serverId": "srv_123", "conversationId": "cli:test"}
    file_name, _fh, mime = captured["files"]["file"]
    assert (file_name, mime) == ("clip.mp4", "video/mp4")
    assert payload["cdnUrl"] == "https://cdn.useflowlyapp.com/clip.mp4"


def test_render_message_with_attachments() -> None:
    assert render_message_with_attachments("describe", [Path("a.png"), Path("b.jpg")]) == (
        "[image] [image] describe"
    )
    assert render_message_with_attachments("analyze", [Path("clip.mp4")]) == (
        "[video] analyze"
    )


def test_composer_cleans_visible_image_markers_before_send() -> None:
    assert Composer._clean_attachment_markers("[image] describe this") == "describe this"
    assert Composer._clean_attachment_markers("[image] [image] compare") == "compare"
    assert Composer._clean_attachment_markers("[image]bu ne?") == "bu ne?"
    assert Composer._clean_attachment_markers("[video]bu ne?") == "bu ne?"


def test_transcript_render_does_not_duplicate_visible_marker() -> None:
    cleaned = Composer._clean_attachment_markers("[image]bu ne?")
    assert render_message_with_attachments(cleaned, [Path("cat.png")]) == "[image] bu ne?"


@pytest.mark.asyncio
async def test_gateway_client_sends_attachments_payload() -> None:
    client = GatewayClient.__new__(GatewayClient)
    captured: dict[str, Any] = {}

    async def fake_rpc(method: str, params: dict[str, Any]) -> str:
        captured["method"] = method
        captured["params"] = params
        return "rpc-1"

    async def fake_await_reply(rid: str, *, timeout: float) -> dict[str, Any]:
        captured["rid"] = rid
        captured["timeout"] = timeout
        return {"runId": "run-1"}

    client._rpc = fake_rpc
    client._await_reply = fake_await_reply

    run_id = await client.chat_send(
        "describe",
        session_key="tui:test",
        run_id="fixed-id",
        attachments=[{"filePath": "/tmp/cat.png", "fileName": "cat.png"}],
    )

    assert run_id == "run-1"
    assert captured["method"] == "chat.send"
    assert captured["params"] == {
        "message": "describe",
        "sessionKey": "tui:test",
        "idempotencyKey": "fixed-id",
        "attachments": [{"filePath": "/tmp/cat.png", "fileName": "cat.png"}],
    }


def test_save_clipboard_image_uses_macos_pngpaste(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setattr(clipboard_mod.sys, "platform", "darwin")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "pngpaste"
        Path(cmd[1]).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(clipboard_mod.subprocess, "run", fake_run)

    path = clipboard_mod.save_clipboard_image()

    assert path is not None
    assert path.parent == tmp_path / "clipboard"
    assert path.suffix == ".png"
    assert path.read_bytes().startswith(b"\x89PNG")


def test_save_clipboard_image_returns_none_when_no_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setattr(clipboard_mod.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(clipboard_mod, "_is_wsl", lambda: False)

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(clipboard_mod.subprocess, "run", fake_run)

    assert clipboard_mod.save_clipboard_image() is None
