"""Image analysis keeps provider choice and local-file authority intact."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from flowly.agent.media_files import read_media_file
from flowly.agent.tools.image_analyze import ImageAnalyzeTool


@pytest.fixture
def image_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    current = SimpleNamespace(provider=SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="A blue square"))), model="chosen-vision-model")
    tool = ImageAnalyzeTool(provider_getter=lambda: current.provider, model_getter=lambda: current.model, workspace=workspace)
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, format="PNG")
    source = workspace / "input.png"
    source.write_bytes(output.getvalue())
    return tool, current, source


@pytest.mark.parametrize("kind", ["file", "data"])
async def test_image_analysis_passes_native_input_to_current_provider_without_model_substitution(image_tool, kind):
    tool, current, source = image_tool
    data = "data:image/png;base64," + base64.b64encode(source.read_bytes()).decode()
    result = await tool.execute(str(source) if kind == "file" else data, "Describe this")
    assert json.loads(result) == {"analysis": "A blue square"}
    call = current.provider.chat.call_args.kwargs
    assert call["model"] == "chosen-vision-model"
    assert call["messages"][0]["content"][1]["image_url"]["url"] == data
    current.model = "user-reconfigured-model"
    current.provider = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="New provider")))
    assert json.loads(await tool.execute(data, "Question"))["analysis"] == "New provider"
    assert current.provider.chat.call_args.kwargs["model"] == "user-reconfigured-model"


@pytest.mark.parametrize("source", [
    "http://127.0.0.1/private", "http://169.254.169.254/latest/meta-data", "http://[::1]/private",
    "https://user:private@public.example/image.png", "data:image/svg+xml;base64,PHN2Zy8+",
    "data:image/png;base64,not-valid!", "data:image/png;base64,YmFkLWltYWdl",
])
async def test_invalid_or_private_images_never_reach_provider(image_tool, source):
    tool, current, _ = image_tool
    assert (await tool.execute(source, "Question")).startswith("Error:")
    current.provider.chat.assert_not_awaited()


async def test_symlink_outside_workspace_cannot_disclose_file(image_tool, tmp_path):
    tool, current, source = image_tool
    secret = tmp_path / "secret.png"
    secret.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(secret)
    assert (await tool.execute(str(source), "Question")).startswith("Error:")
    current.provider.chat.assert_not_awaited()


async def test_oversized_image_and_decompression_bomb_never_reach_provider(image_tool, monkeypatch):
    import flowly.agent.tools.image_analyze as module

    tool, current, source = image_tool
    monkeypatch.setattr(module, "_MAX_IMAGE_BYTES", 16)
    assert (await tool.execute(str(source), "Question")).startswith("Error:")
    monkeypatch.setattr(module, "_MAX_IMAGE_BYTES", 1024)

    def bomb(*args, **kwargs):
        raise Image.DecompressionBombError("too many pixels")

    monkeypatch.setattr(Image, "open", bomb)
    assert (await tool.execute(str(source), "Question")).startswith("Error:")
    current.provider.chat.assert_not_awaited()


async def test_provider_failure_does_not_leak_credentials_or_change_model(image_tool):
    tool, current, source = image_tool
    current.provider.chat.side_effect = RuntimeError("private-provider-credential")
    result = await tool.execute(str(source), "Question")
    assert result.startswith("Error:")
    assert "private-provider-credential" not in result
    assert current.model == "chosen-vision-model"


async def test_image_analysis_propagates_cancellation(image_tool):
    tool, current, source = image_tool
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def wait(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    current.provider.chat.side_effect = wait
    pending = asyncio.create_task(tool.execute(str(source), "Question"))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cancelled.is_set()


def test_media_reader_rejects_directory_symlink_replacement_after_resolution(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    nested = allowed / "nested"
    nested.mkdir(parents=True)
    (nested / "image.png").write_bytes(b"allowed")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "image.png").write_bytes(b"private")
    original_open = os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "nested" and not swapped:
            swapped = True
            nested.rename(allowed / "old-nested")
            nested.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, racing_open})
    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(OSError):
        read_media_file(nested / "image.png", (allowed,), 1024)
    assert swapped


def test_media_reader_refuses_fifo_without_waiting(tmp_path):
    fifo = tmp_path / "pipe.png"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        read_media_file(fifo, (tmp_path,), 1024)


def test_media_reader_allows_symlink_resolving_inside_workspace(tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"allowed")
    link = tmp_path / "link.png"
    link.symlink_to(source)
    assert read_media_file(link, (tmp_path,), 1024) == b"allowed"
