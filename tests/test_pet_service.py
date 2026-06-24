"""Tests for flowly.pet.service + the pet.* feature-RPC wiring."""

import base64
import io

import httpx
import pytest
from PIL import Image

from flowly.channels import feature_rpc
from flowly.config.loader import load_config, save_config
from flowly.pet import manifest, service


@pytest.fixture
def pet_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    manifest.clear_cache()
    yield tmp_path
    manifest.clear_cache()


@pytest.fixture
def png() -> bytes:
    """A full 9-row Petdex atlas (192x1872) with one opaque frame on the idle
    row (0) and the canonical 'running' row (7) — so 'run' must resolve to 7,
    not to row 1, under the real row taxonomy."""
    img = Image.new("RGBA", (192, 208 * 9), (0, 0, 0, 0))
    block = Image.new("RGBA", (192, 208), (255, 0, 0, 255))
    img.paste(block, (0, 0))            # row 0 = idle
    img.paste(block, (0, 208 * 7))      # row 7 = running
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _client(png: bytes) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/manifest"):
            return httpx.Response(200, json={"pets": [{
                "slug": "otter", "name": "Otter",
                "spritesheet": "https://petdex.dev/otter.png",
                "states": ["idle", "run"],
            }]})
        if path.endswith("otter.png"):
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _boom_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("petdex down")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── service ──────────────────────────────────────────────────────────

class TestService:
    def test_get_info_disabled_by_default(self, pet_env):
        assert service.get_info() == {"enabled": False}

    async def test_select_installs_enables_and_returns_info(self, pet_env, png):
        client = _client(png)
        try:
            info = await service.select("otter", client=client)
        finally:
            await client.aclose()

        assert info["enabled"] is True
        assert info["slug"] == "otter"
        assert info["rowByState"] == {"idle": 0, "run": 7}  # 'running' row, not row 1
        assert info["framesByState"] == {"idle": 1, "run": 1}
        assert info["spritesheetMime"] == "image/png"
        assert base64.b64decode(info["spritesheet"]) == png

        cfg = load_config()
        assert cfg.display.pet.slug == "otter"
        assert cfg.display.pet.enabled is True
        assert service.get_info()["enabled"] is True  # standalone read works

    async def test_gallery_online_flags(self, pet_env, png):
        client = _client(png)
        try:
            await service.select("otter", client=client)
            gallery = await service.get_gallery(client=client)
        finally:
            await client.aclose()
        otter = next(p for p in gallery["pets"] if p["slug"] == "otter")
        assert otter["installed"] is True
        assert otter["active"] is True
        assert gallery["offline"] is False

    async def test_gallery_uses_display_name_from_manifest(self, pet_env):
        # Real Petdex manifest entries carry ``displayName``; the gallery must
        # surface it as the name rather than echoing the slug.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/manifest"):
                return httpx.Response(200, json={"pets": [{
                    "slug": "homelander", "displayName": "Homelander",
                    "spritesheetUrl": "https://assets.petdex.dev/x/sprite.webp",
                }]})
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            gallery = await service.get_gallery(client=client)
        finally:
            await client.aclose()
        hl = next(p for p in gallery["pets"] if p["slug"] == "homelander")
        assert hl["name"] == "Homelander"  # displayName, not the slug

    async def test_gallery_offline_falls_back_to_installed(self, pet_env, png):
        online = _client(png)
        try:
            await service.select("otter", client=online)
        finally:
            await online.aclose()
        manifest.clear_cache()

        offline = _boom_client()
        try:
            gallery = await service.get_gallery(client=offline)
        finally:
            await offline.aclose()
        assert gallery["offline"] is True
        assert any(p["slug"] == "otter" for p in gallery["pets"])

    async def test_select_preserves_active_on_failure(self, pet_env):
        cfg = load_config()
        cfg.display.pet.slug = "cat"
        cfg.display.pet.enabled = True
        save_config(cfg)

        client = _boom_client()
        try:
            with pytest.raises(service.PetServiceError):
                await service.select("otter", client=client)
        finally:
            await client.aclose()

        cfg2 = load_config()
        assert cfg2.display.pet.slug == "cat"
        assert cfg2.display.pet.enabled is True

    def test_disable(self, pet_env):
        cfg = load_config()
        cfg.display.pet.slug = "x"
        cfg.display.pet.enabled = True
        save_config(cfg)
        assert service.disable() == {"enabled": False}
        assert load_config().display.pet.enabled is False

    def test_set_scale_clamps_and_persists(self, pet_env):
        result = service.set_scale(99.0)
        assert result["scale"] == 3.0
        assert load_config().display.pet.scale == 3.0


# ── feature-RPC wiring ───────────────────────────────────────────────

class TestDispatch:
    def test_methods_registered(self):
        for m in ("pet.info", "pet.gallery", "pet.select", "pet.disable", "pet.scale"):
            assert m in feature_rpc.FEATURE_METHODS

    async def test_dispatch_info_disabled(self, pet_env):
        result, restart = await feature_rpc.dispatch("pet.info", {})
        assert result == {"enabled": False}
        assert restart is False

    async def test_dispatch_scale_clamps(self, pet_env):
        result, _ = await feature_rpc.dispatch("pet.scale", {"scale": 99})
        assert result["scale"] == 3.0

    async def test_dispatch_select_requires_slug(self, pet_env):
        with pytest.raises(feature_rpc.FeatureRpcError):
            await feature_rpc.dispatch("pet.select", {})
