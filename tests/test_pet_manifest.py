"""Tests for flowly.pet.manifest — fetch, TTL cache, structured errors."""

import httpx
import pytest

from flowly.pet import manifest


@pytest.fixture(autouse=True)
def _clear_cache():
    manifest.clear_cache()
    yield
    manifest.clear_cache()


def _counting_client(payload, counter):
    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json=payload)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestFetchAndCache:
    async def test_second_call_served_from_cache(self):
        counter = {"n": 0}
        client = _counting_client({"pets": [{"slug": "otter"}]}, counter)
        try:
            m1 = await manifest.fetch_manifest(client=client)
            m2 = await manifest.fetch_manifest(client=client)
        finally:
            await client.aclose()
        assert m1 == m2 == {"pets": [{"slug": "otter"}]}
        assert counter["n"] == 1  # cached on the second call

    async def test_force_bypasses_cache(self):
        counter = {"n": 0}
        client = _counting_client({"pets": []}, counter)
        try:
            await manifest.fetch_manifest(client=client)
            await manifest.fetch_manifest(client=client, force=True)
        finally:
            await client.aclose()
        assert counter["n"] == 2

    async def test_cache_expires_after_ttl(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(manifest, "_now", lambda: clock["t"])
        counter = {"n": 0}
        client = _counting_client({"pets": []}, counter)
        try:
            await manifest.fetch_manifest(client=client)
            clock["t"] += manifest.CACHE_TTL_SECONDS + 1
            await manifest.fetch_manifest(client=client)
        finally:
            await client.aclose()
        assert counter["n"] == 2  # cache went stale


class TestErrors:
    async def test_network_error_is_structured(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(manifest.PetManifestError):
                await manifest.fetch_manifest(client=client)
        finally:
            await client.aclose()

    async def test_non_object_manifest_rejected(self):
        client = _counting_client(["not", "an", "object"], {"n": 0})
        try:
            with pytest.raises(manifest.PetManifestError):
                await manifest.fetch_manifest(client=client)
        finally:
            await client.aclose()


class TestRedirect:
    async def test_follows_onhost_redirect_to_assets_cdn(self):
        # petdex.dev/api/manifest 307s to assets.petdex.dev — must be followed.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/manifest"):
                return httpx.Response(
                    307, headers={"location": "https://assets.petdex.dev/manifests/v1.json"}
                )
            if request.url.host == "assets.petdex.dev":
                return httpx.Response(200, json={"pets": [{"slug": "otter"}]})
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            m = await manifest.fetch_manifest(client=client)
        finally:
            await client.aclose()
        assert m == {"pets": [{"slug": "otter"}]}

    async def test_rejects_offhost_redirect(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "petdex.dev":
                return httpx.Response(
                    307, headers={"location": "https://evil.example/manifest.json"}
                )
            return httpx.Response(200, json={"pets": [{"slug": "evil"}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(manifest.PetManifestError):
                await manifest.fetch_manifest(client=client)
        finally:
            await client.aclose()


class TestExtract:
    def test_pets_from_manifest_filters_non_dicts(self):
        out = manifest.pets_from_manifest({"pets": [{"slug": "a"}, "bad", {"slug": "b"}]})
        assert out == [{"slug": "a"}, {"slug": "b"}]

    def test_pets_from_manifest_missing_key(self):
        assert manifest.pets_from_manifest({}) == []
