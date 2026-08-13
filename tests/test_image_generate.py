"""Tests for the FAL image-generation media layer.

httpx + the FAL network are mocked; only the catalog, value assembly, download-
to-media-dir, tool wiring, registry card, and probe are exercised.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import flowly.media.fal as fal
from flowly.agent.tools.image_generate import ImageGenerateTool
from flowly.integrations.probes import probe_fal_image
from flowly.integrations.registry import get_card, list_cards

# The hand-written model list these tests used to pin is gone — the catalog is
# fetched now (see tests/test_media_catalog.py). What remains here is the FAL
# client and the tool contract, neither of which changed.

# ── FAL client (mocked httpx) ────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, json_data=None, content=b""):
        self.status_code = status
        self._json = json_data or {}
        self.content = content
        self.text = "err"

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError("bad")


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **k):
        return _Resp(200, {"images": [{"url": "https://fal.media/x.png"}], "seed": 7})

    async def get(self, url, **k):
        return _Resp(200, content=b"PNGBYTES")


def test_generate_image_downloads_to_media_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    out = asyncio.run(fal.generate_image(api_key="k", model="fal-ai/flux/dev", prompt="a cat"))
    assert out["urls"] == ["https://fal.media/x.png"]
    assert len(out["paths"]) == 1
    p = out["paths"][0]
    assert p.startswith(str(tmp_path)) and p.endswith(".png")
    from pathlib import Path
    assert Path(p).read_bytes() == b"PNGBYTES"


def test_generate_image_requires_key_and_prompt():
    with pytest.raises(fal.FalError):
        asyncio.run(fal.generate_image(api_key="", model="m", prompt="x"))
    with pytest.raises(fal.FalError):
        asyncio.run(fal.generate_image(api_key="k", model="m", prompt="  "))


def test_generate_image_auth_error(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))

    class _Auth(_FakeClient):
        async def post(self, url, **k):
            return _Resp(401)

    monkeypatch.setattr(httpx, "AsyncClient", _Auth)
    with pytest.raises(fal.FalError):
        asyncio.run(fal.generate_image(api_key="bad", model="m", prompt="x"))


# ── tool ─────────────────────────────────────────────────────────────────

def test_tool_metadata():
    t = ImageGenerateTool(api_key="k", model="fal-ai/flux/dev")
    assert t.name == "image_generate"
    assert t.parameters["required"] == ["prompt"]


def test_tool_execute_returns_reply_media_envelope(monkeypatch):
    import json

    async def _gen(**k):
        return {"paths": ["/m/img-abc.png"], "urls": ["u"], "model": k["model"], "seed": 1}

    monkeypatch.setattr(fal, "generate_image", _gen)
    t = ImageGenerateTool(api_key="k", model="fal-ai/flux/dev")
    out = asyncio.run(t.execute(prompt="a fox"))
    # Returns a reply-media envelope (the loop attaches it to the reply), NOT a
    # message-tool instruction.
    parsed = json.loads(out)
    assert parsed["_reply_media"] == ["/m/img-abc.png"]
    assert "attached" in parsed["summary"].lower()
    assert "media_paths" not in out


def test_tool_execute_handles_fal_error(monkeypatch):
    async def _boom(**k):
        raise fal.FalError("rejected key")

    monkeypatch.setattr(fal, "generate_image", _boom)
    t = ImageGenerateTool(api_key="k", model="m")
    out = asyncio.run(t.execute(prompt="x"))
    assert "failed" in out.lower() and "rejected key" in out


def test_tool_execute_requires_prompt():
    t = ImageGenerateTool(api_key="k", model="m")
    out = asyncio.run(t.execute(prompt="   "))
    assert "required" in out.lower()


# ── registry card + probe ────────────────────────────────────────────────

def test_media_card_registered():
    card = get_card("fal_image")
    assert card is not None
    assert card.category == "media"
    # The card covers video as well as images now, so it edits the shared
    # media block. The legacy image-only block is still READ at startup — see
    # flowly.media.settings — so an existing install keeps its key and model.
    assert card.config_path == "tools.media_generation"
    assert card in list_cards("media")


def test_probe_presence(monkeypatch):
    ok = asyncio.run(probe_fal_image({"enabled": True, "api_key": "k"}))
    assert ok.status == "ok"
    none = asyncio.run(probe_fal_image({"enabled": True, "api_key": ""}))
    assert none.status == "not_configured"
    disabled = asyncio.run(probe_fal_image({"enabled": False, "api_key": "k"}))
    assert disabled.status == "disabled"
