"""Image edits, the saved FAL_KEY, and where an attached image is (2026-11-01).

The owner saved FAL_KEY when the agent asked for it, and nothing used it: the
image tool read only Media settings. Then they pasted a screenshot and asked
for an edit: the agent saw the picture but had no path to it, and the image
tool took no source image, so it said the screenshot "is not on disk" and
generated a new picture instead.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import flowly.media.fal as fal
from flowly.agent.tools.image_generate import ImageGenerateTool, edit_endpoint
from flowly.media.settings import resolve_media_settings
from flowly.secrets import envelope, redact, service
from flowly.secrets import requests as secret_requests
from flowly.secrets import vault as vault_mod

FAL_KEY = "fal-key-0123456789abcdef"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(vault_mod, "_keyring", lambda: None)
    for module in (vault_mod, secret_requests, redact):
        module._reset_for_tests()
    yield tmp_path / "home"
    for module in (vault_mod, secret_requests, redact):
        module._reset_for_tests()


def save_fal_key() -> None:
    key = service.status()["key"]
    sealed = envelope.seal({"FAL_KEY": FAL_KEY}, envelope.unb64url(key["publicKey"]), kid=key["kid"],
                           request_id=None, description="Images")
    asyncio.run(service.submit({"names": ["FAL_KEY"], "kid": key["kid"], "sealed": sealed}))


def tools(*, enabled: bool = False, api_key: str = "") -> SimpleNamespace:
    media = SimpleNamespace(enabled=enabled, api_key=api_key, provider="fal", defaults=None, video_timeout_seconds=900)
    return SimpleNamespace(media_generation=media, image_generation=None)


# ── the saved key ───────────────────────────────────────────────────────


def test_a_saved_fal_key_turns_image_generation_on_when_settings_have_none(home):
    assert not resolve_media_settings(tools()).image_ready
    save_fal_key()
    settings = resolve_media_settings(tools())
    assert settings.image_ready and settings.api_key == FAL_KEY


def test_a_key_in_media_settings_wins_over_the_saved_one(home):
    save_fal_key()
    settings = resolve_media_settings(tools(enabled=True, api_key="from-settings"))
    assert settings.api_key == "from-settings"


def test_the_agent_gets_the_tool_on_its_next_turn_after_the_key_is_saved(home):
    from flowly.agent.loop import AgentLoop

    registered: dict[str, object] = {}
    loop = SimpleNamespace(
        _main_config=SimpleNamespace(tools=tools()),
        tools=SimpleNamespace(get=registered.get, register=lambda tool: registered.__setitem__(tool.name, tool),
                              unregister=lambda name: registered.pop(name, None)),
    )
    AgentLoop._refresh_media_tools(loop)
    assert "image_generate" not in registered
    save_fal_key()
    AgentLoop._refresh_media_tools(loop)
    assert registered["image_generate"]._api_key == FAL_KEY
    tool = registered["image_generate"]
    AgentLoop._refresh_media_tools(loop)
    assert registered["image_generate"] is tool, "unchanged settings keep the tool as it is"


# ── edits ───────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status=200, json_data=None, content=b""):
        self.status_code, self._json, self.content, self.text = status, json_data or {}, content, "err"

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError("bad")


def fake_fal(calls: list) -> type:
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            calls.append((url, k["json"]))
            return _Resp(200, {"images": [{"url": "https://fal.media/out.png"}]})

        async def get(self, url, **k):
            return _Resp(200, content=b"PNGBYTES")

    return Client


def test_an_edit_sends_the_source_image_to_the_models_edit_endpoint(home, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "AsyncClient", fake_fal(calls))
    shot = tmp_path / "screenshot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    tool = ImageGenerateTool(api_key="k", model="openai/gpt-image-2")

    out = asyncio.run(tool.execute(prompt="Add a soft background", source_images=[str(shot), "https://cdn.example/a.png"]))

    assert "Generated 1 image" in out
    url, payload = calls[0]
    assert url == "https://fal.run/openai/gpt-image-2/edit"
    assert payload["image_urls"][0] == "data:image/png;base64," + base64.b64encode(shot.read_bytes()).decode()
    assert payload["image_urls"][1] == "https://cdn.example/a.png"
    assert "image_size" not in payload, "an edit keeps its source's shape"


def test_without_a_source_it_generates_as_before(home, monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "AsyncClient", fake_fal(calls))
    asyncio.run(ImageGenerateTool(api_key="k", model="openai/gpt-image-2").execute(prompt="an apple"))
    url, payload = calls[0]
    assert url == "https://fal.run/openai/gpt-image-2"
    assert "image_urls" not in payload and payload["image_size"] == "landscape_4_3"


def test_a_model_that_cannot_edit_says_so_and_calls_nothing(home, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "AsyncClient", fake_fal(calls))
    shot = tmp_path / "s.png"
    shot.write_bytes(b"png")
    out = asyncio.run(ImageGenerateTool(api_key="k", model="fal-ai/flux/dev").execute(prompt="x", source_images=[str(shot)]))
    assert out.startswith("Error:") and calls == []


def test_the_chosen_image_to_image_model_is_the_edit_endpoint():
    assert edit_endpoint("fal-ai/flux/dev", "fal-ai/flux-kontext/dev") == "fal-ai/flux-kontext/dev"
    assert edit_endpoint("openai/gpt-image-2") == "openai/gpt-image-2/edit"
    assert edit_endpoint("fal-ai/flux/dev") is None


def test_a_missing_or_non_image_source_is_an_error(home, tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("hi")
    tool = ImageGenerateTool(api_key="k", model="openai/gpt-image-2")
    assert "not found" in asyncio.run(tool.execute(prompt="x", source_images=[str(tmp_path / "gone.png")]))
    assert "not an image" in asyncio.run(tool.execute(prompt="x", source_images=[str(notes)]))


# ── where an attached image is ──────────────────────────────────────────


def test_the_model_is_told_where_each_attached_image_is(home, tmp_path):
    from PIL import Image

    from flowly.agent.context import ContextBuilder

    shot = tmp_path / "shot.png"
    Image.new("RGB", (8, 8)).save(shot)
    url = "https://cdn.example/users/u/servers/s/conversations/c/abc.png"
    content = ContextBuilder._build_user_content(object.__new__(ContextBuilder), "edit this", [str(shot), url])

    text = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
    assert str(shot) in text and url in text
    assert sum(block.get("type") == "image_url" for block in content) == 2
