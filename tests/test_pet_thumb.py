"""Tests for pet.thumb — server-side thumbnail (spritesheet render + manifest fallback)."""

import base64
import io

import httpx
import pytest
from PIL import Image

from flowly.channels import feature_rpc
from flowly.pet import manifest, service


@pytest.fixture
def pet_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    manifest.clear_cache()
    yield tmp_path
    manifest.clear_cache()


def _png(w: int, h: int) -> bytes:
    img = Image.new("RGBA", (w, h), (10, 20, 30, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _sheet_client(sheet: bytes) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/manifest"):
            return httpx.Response(200, json={"pets": [{
                "slug": "otter", "name": "Otter",
                "spritesheet": "https://petdex.dev/otter.png",
                "states": ["idle", "run"],
            }]})
        if path.endswith("otter.png"):
            return httpx.Response(200, content=sheet, headers={"content-type": "image/png"})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _thumb_client(thumb: bytes) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/manifest"):
            return httpx.Response(200, json={"pets": [{
                "slug": "foxy", "thumb": "https://petdex.dev/foxy.png",
            }]})
        if path.endswith("foxy.png"):
            return httpx.Response(200, content=thumb, headers={"content-type": "image/png"})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestThumb:
    async def test_render_from_installed_spritesheet(self, pet_env):
        sheet = _png(192, 416)  # 2 rows of the Petdex frame size
        client = _sheet_client(sheet)
        try:
            await service.select("otter", client=client)
        finally:
            await client.aclose()

        res = await service.get_thumb("otter")
        assert res["slug"] == "otter"
        assert res["dataUri"].startswith("data:image/png;base64,")
        # thumb is cached on disk + the call is idempotent
        assert (pet_env / "pets" / "otter" / "thumb.png").is_file()
        assert (await service.get_thumb("otter"))["dataUri"] == res["dataUri"]
        # and it is a real, decodable PNG no larger than THUMB_MAX on its long side
        raw = base64.b64decode(res["dataUri"].split(",", 1)[1])
        img = Image.open(io.BytesIO(raw))
        assert max(img.size) <= service.THUMB_MAX

    async def test_manifest_fallback_when_not_installed(self, pet_env):
        thumb = _png(64, 64)
        client = _thumb_client(thumb)
        try:
            res = await service.get_thumb("foxy", client=client)
        finally:
            await client.aclose()
        assert base64.b64decode(res["dataUri"].split(",", 1)[1]) == thumb
        assert (pet_env / "pets" / "foxy" / "thumb.png").is_file()

    async def test_invalid_slug(self, pet_env):
        with pytest.raises(service.PetServiceError):
            await service.get_thumb("...")

    async def test_not_found(self, pet_env):
        client = _thumb_client(_png(8, 8))  # manifest only has "foxy"
        try:
            with pytest.raises(service.PetServiceError):
                await service.get_thumb("ghost", client=client)
        finally:
            await client.aclose()

    async def test_dispatch_requires_slug(self, pet_env):
        assert "pet.thumb" in feature_rpc.FEATURE_METHODS
        with pytest.raises(feature_rpc.FeatureRpcError):
            await feature_rpc.dispatch("pet.thumb", {})
