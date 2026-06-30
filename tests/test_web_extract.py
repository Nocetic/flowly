"""web_extract tool dispatch + local readability extract fallback."""

from __future__ import annotations

import asyncio
import json

import pytest

import flowly.agent.tools.web as webmod
from flowly.agent.tools.web import WebExtractTool, _run_provider_extract
from flowly.agent.tools.web_providers import registry as reg
from flowly.agent.tools.web_providers.base import WebSearchProvider
from flowly.agent.tools.web_providers.local import LocalExtractProvider


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reg._reset_for_tests()
    reg._loaded = True  # skip plugin discovery
    monkeypatch.setattr(reg, "_read_config_backend", lambda cap: "")
    monkeypatch.setattr(reg, "_read_default_backend", lambda: "")
    yield
    reg._reset_for_tests()


class _FakeExtract(WebSearchProvider):
    @property
    def name(self):
        return "fake"

    def is_available(self):
        return True

    def supports_search(self):
        return False

    def supports_extract(self):
        return True

    def extract(self, urls, **kwargs):
        return [{"url": u, "title": "T", "content": "C-" + u, "raw_content": "C"} for u in urls]


def _run(tool, **kw):
    return json.loads(asyncio.run(tool.execute(**kw)))


def test_dispatches_to_active_extract_provider():
    reg.register_provider(_FakeExtract())
    out = _run(WebExtractTool(), urls=["https://a.com"])
    assert out["success"] is True
    assert out["backend"] == "fake"
    assert out["results"][0]["content"].startswith("C-")


def test_falls_back_to_local_when_no_provider(monkeypatch):
    async def fake_fetch(url, query=None, extract_mode="markdown", max_chars=50000):
        return {"finalUrl": url, "title": "Loc", "text": "local content", "status": 200}

    monkeypatch.setattr(webmod, "_fetch_readable", fake_fetch)
    out = _run(WebExtractTool(), urls=["https://b.com"])
    assert out["success"] is True
    assert out["backend"] == "local"
    assert out["results"][0]["title"] == "Loc"
    assert out["results"][0]["content"] == "local content"


def test_ssrf_blocks_private_urls():
    out = _run(WebExtractTool(), urls=["http://127.0.0.1/secret"])
    assert out["success"] is False
    assert out["results"][0]["error"]


def test_truncates_to_cap(monkeypatch):
    async def fake_fetch(url, query=None, extract_mode="markdown", max_chars=50000):
        return {"finalUrl": url, "title": "X", "text": "y" * 1000, "status": 200}

    monkeypatch.setattr(webmod, "_fetch_readable", fake_fetch)
    out = _run(WebExtractTool(), urls=["https://b.com"], maxChars=100)
    r = out["results"][0]
    assert len(r["content"]) == 100
    assert r.get("truncated") is True


def test_run_provider_extract_sync_and_async():
    class _Sync(WebSearchProvider):
        @property
        def name(self):
            return "s"

        def is_available(self):
            return True

        def supports_extract(self):
            return True

        def extract(self, urls, **kw):
            return [{"url": urls[0]}]

    class _Async(WebSearchProvider):
        @property
        def name(self):
            return "a"

        def is_available(self):
            return True

        def supports_extract(self):
            return True

        async def extract(self, urls, **kw):
            return [{"url": urls[0], "async": True}]

    assert asyncio.run(_run_provider_extract(_Sync(), ["u"]))[0]["url"] == "u"
    assert asyncio.run(_run_provider_extract(_Async(), ["u"]))[0]["async"] is True


def test_local_provider_maps_to_extract_shape(monkeypatch):
    async def fake_fetch(url, query=None, extract_mode="markdown", max_chars=50000):
        return {"finalUrl": url, "title": "L", "text": "content"}

    monkeypatch.setattr(webmod, "_fetch_readable", fake_fetch)
    res = asyncio.run(LocalExtractProvider().extract(["https://c.com"]))
    assert res[0]["title"] == "L"
    assert res[0]["content"] == "content"
    assert res[0]["metadata"]["sourceURL"] == "https://c.com"


def test_local_provider_is_extract_only():
    p = LocalExtractProvider()
    assert p.is_available() is True
    assert p.supports_extract() is True
    assert p.supports_search() is False
