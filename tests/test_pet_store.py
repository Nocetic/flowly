"""Tests for flowly.pet.store — slug safety, host pinning, atomic downloads."""

import httpx
import pytest

from flowly.pet import store


# ── slug normalisation ──────────────────────────────────────────────

class TestSafeSlug:
    def test_lowercases(self):
        assert store.safe_slug("Otter") == "otter"

    def test_strips_path_traversal(self):
        assert store.safe_slug("../../etc/passwd") == "etcpasswd"

    def test_keeps_allowed_chars(self):
        assert store.safe_slug("good-slug_1") == "good-slug_1"

    def test_rejects_empty_after_strip(self):
        with pytest.raises(store.PetStoreError):
            store.safe_slug("...")
        with pytest.raises(store.PetStoreError):
            store.safe_slug("")


# ── host pinning ────────────────────────────────────────────────────

class TestHostPinning:
    def test_allows_petdex_and_subdomains(self):
        assert store.is_allowed_url("https://petdex.dev/otter.webp")
        assert store.is_allowed_url("https://cdn.petdex.dev/otter.webp")

    def test_blocks_non_https(self):
        assert not store.is_allowed_url("http://petdex.dev/otter.webp")

    def test_blocks_suffix_trick(self):
        # "evilpetdex.dev" must NOT pass as a petdex.dev host.
        assert not store.is_allowed_url("https://evilpetdex.dev/x.webp")

    def test_blocks_subdomain_trick(self):
        assert not store.is_allowed_url("https://petdex.dev.evil.com/x.webp")

    def test_blocks_foreign_and_garbage(self):
        assert not store.is_allowed_url("https://example.com/x.webp")
        assert not store.is_allowed_url("not a url")


# ── metadata + listing (profile-aware via FLOWLY_HOME) ──────────────

class TestMeta:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
        store.write_meta("otter", {"slug": "otter", "name": "Otter"})
        assert store.read_meta("otter") == {"slug": "otter", "name": "Otter"}

    def test_read_missing_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
        assert store.read_meta("nope") is None

    def test_list_installed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
        assert store.list_installed() == []
        store.write_meta("otter", {"slug": "otter"})
        store.write_meta("cat", {"slug": "cat"})
        assert store.list_installed() == ["cat", "otter"]


# ── atomic writes ───────────────────────────────────────────────────

class TestAtomicWrite:
    def test_writes_and_leaves_no_part(self, tmp_path):
        dest = tmp_path / "sub" / "f.bin"
        store.atomic_write_bytes(dest, b"data")
        assert dest.read_bytes() == b"data"
        assert not list((tmp_path / "sub").glob("*.part*"))


# ── downloads ───────────────────────────────────────────────────────

class TestDownload:
    async def test_blocks_foreign_host_before_network(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
        with pytest.raises(store.PetStoreError):
            await store.download_asset("https://evil.com/x.webp", tmp_path / "x.webp")

    async def test_success_writes_atomically(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"SPRITESHEET")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dest = store.pet_dir("otter") / "spritesheet.webp"
        try:
            await store.download_asset("https://petdex.dev/otter.webp", dest, client=client)
        finally:
            await client.aclose()

        assert dest.read_bytes() == b"SPRITESHEET"
        assert not list(dest.parent.glob("*.part*"))

    async def test_size_cap_rejects_and_cleans_up(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
        monkeypatch.setattr(store, "MAX_ASSET_BYTES", 4)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"way-too-long")  # 12 bytes > 4

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dest = tmp_path / "big.webp"
        try:
            with pytest.raises(store.PetStoreError):
                await store.download_asset("https://petdex.dev/big.webp", dest, client=client)
        finally:
            await client.aclose()

        assert not dest.exists()
        assert not list(tmp_path.glob("*.part*"))
