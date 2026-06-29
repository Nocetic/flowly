"""WebSearchTool provider dispatch + Brave provider behaviour."""

from __future__ import annotations

import asyncio

import pytest

from flowly.agent.tools.web import WebSearchTool, _format_search_results
from flowly.agent.tools.web_providers import registry as reg
from flowly.agent.tools.web_providers.base import WebSearchProvider
from flowly.agent.tools.web_providers.brave import BraveWebSearchProvider


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reg._reset_for_tests()
    reg._loaded = True  # skip plugin discovery in unit tests
    # Make resolution independent of the developer's real config.
    monkeypatch.setattr(reg, "_read_config_backend", lambda cap: "")
    yield
    reg._reset_for_tests()


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── formatter ──────────────────────────────────────────────────


def test_format_generic_results():
    data = {"success": True, "data": {"web": [
        {"title": "T", "url": "http://u", "description": "d", "position": 1},
    ]}}
    out = _format_search_results("q", data)
    assert "Results for: q" in out
    assert "1. T" in out and "http://u" in out and "d" in out


def test_format_brave_enrichments():
    data = {"success": True, "data": {
        "summary": "the gist",
        "web": [{
            "title": "B", "url": "http://b", "description": "bd",
            "extra_snippets": ["s1", "s2"], "age": "1d",
            "page_age": "2024", "language": "en",
        }],
        "news": [{"title": "N", "url": "http://n", "source": "src", "age": "2h"}],
    }}
    out = _format_search_results("q", data)
    assert "Summary: the gist" in out
    assert "> s1" in out and "> s2" in out
    assert "1d" in out and "published: 2024" in out and "lang: en" in out
    assert "Recent News:" in out and "N (src, 2h)" in out


def test_format_error_passthrough():
    assert _format_search_results("q", {"success": False, "error": "boom"}) == "boom"


def test_format_no_results():
    out = _format_search_results("q", {"success": True, "data": {"web": []}})
    assert out == "No results for: q"


# ── dispatch ───────────────────────────────────────────────────


def test_dispatch_to_registered_provider():
    class _Exa(WebSearchProvider):
        @property
        def name(self):
            return "exa"

        def is_available(self):
            return True

        def supports_extract(self):
            return True

        def search(self, query, limit=5):
            return {"success": True, "data": {"web": [
                {"title": "X", "url": "http://x", "description": "hi", "position": 1},
            ]}}

    reg.register_provider(_Exa())
    out = asyncio.run(WebSearchTool().execute("q"))
    assert "1. X" in out and "http://x" in out


def test_fallback_to_brave_when_no_provider(monkeypatch):
    import flowly.agent.tools.web_providers.brave as bmod

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResp({"web": {"results": [
            {"title": "FB", "url": "http://fb", "description": "d"},
        ]}})

    monkeypatch.setattr(bmod.httpx, "get", fake_get)
    out = asyncio.run(WebSearchTool(api_key="k").execute("q"))
    assert "FB" in out


def test_no_creds_no_provider_returns_error():
    tool = WebSearchTool()  # no api_key, no proxy creds, no registered provider
    out = asyncio.run(tool.execute("q"))
    assert "not available" in out.lower()


# ── Brave provider ─────────────────────────────────────────────


def test_brave_is_available():
    assert BraveWebSearchProvider(api_key="k").is_available()
    assert BraveWebSearchProvider(
        api_key="", proxy_url="http://p", server_id="s", auth_token="t"
    ).is_available()
    assert not BraveWebSearchProvider(
        api_key="", proxy_url="", server_id="", auth_token=""
    ).is_available()


def test_brave_direct_search(monkeypatch):
    import flowly.agent.tools.web_providers.brave as bmod
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResp({"web": {"results": [
            {"title": "B", "url": "http://b", "description": "bd",
             "extra_snippets": ["s1"], "age": "1d"},
        ]}})

    monkeypatch.setattr(bmod.httpx, "get", fake_get)
    data = BraveWebSearchProvider(api_key="k").search("q", 5)
    assert data["success"] is True
    assert data["data"]["web"][0]["title"] == "B"
    assert data["data"]["web"][0]["position"] == 1
    assert "api.search.brave.com" in captured["url"]


def test_brave_proxy_search(monkeypatch):
    import flowly.agent.tools.web_providers.brave as bmod

    def fake_post(url, json=None, headers=None, timeout=None):
        assert headers["X-Flowly-Server-Id"] == "s"
        return _FakeResp({
            "results": [{"title": "P", "url": "http://p1", "description": "pd"}],
            "summary": "sum",
        })

    monkeypatch.setattr(bmod.httpx, "post", fake_post)
    data = BraveWebSearchProvider(
        api_key="", proxy_url="http://proxy", server_id="s", auth_token="t"
    ).search("q", 5)
    assert data["success"] is True
    assert data["data"]["summary"] == "sum"
    assert data["data"]["web"][0]["title"] == "P"


def test_brave_proxy_rate_limit(monkeypatch):
    import flowly.agent.tools.web_providers.brave as bmod

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResp({"error": {"message": "slow down"}}, status=429)

    monkeypatch.setattr(bmod.httpx, "post", fake_post)
    data = BraveWebSearchProvider(
        api_key="", proxy_url="http://proxy", server_id="s", auth_token="t"
    ).search("q", 5)
    assert data["success"] is False
    assert "rate limit" in data["error"].lower()


# ── bundled plugin smoke test ──────────────────────────────────


def test_bundled_web_brave_plugin_registers(tmp_path, monkeypatch):
    from flowly.agent.hooks import HookRegistry
    from flowly.agent.tools.registry import ToolRegistry
    from flowly.plugins import PluginManager, _reset_for_tests

    _reset_for_tests()
    # Isolate user/project plugin dirs so only bundled plugins load.
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))

    mgr = PluginManager(tool_registry=ToolRegistry(), hook_registry=HookRegistry())
    mgr.discover_and_load(enabled=set(), disabled=set())

    info = {p["key"]: p for p in mgr.list_plugins()}
    assert info["web-brave"]["enabled"] is True
    assert info["web-brave"]["web_providers"] == ["brave"]
    assert reg.get_provider("brave") is not None

    _reset_for_tests()
