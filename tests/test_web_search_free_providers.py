"""ddgs + searxng search providers (free, search-only)."""

from __future__ import annotations

import flowly.agent.tools.web_providers.ddgs as ddgs_mod
import flowly.agent.tools.web_providers.searxng as searxng_mod
from flowly.agent.tools.web_providers.ddgs import DDGSWebSearchProvider
from flowly.agent.tools.web_providers.searxng import SearXNGWebSearchProvider


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Section:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ── ddgs ───────────────────────────────────────────────────────


def test_ddgs_search(monkeypatch):
    monkeypatch.setattr(ddgs_mod, "_ddgs_importable", lambda: True)
    monkeypatch.setattr(
        ddgs_mod, "_run_ddgs_search",
        lambda q, n: [{"title": "D", "url": "http://d", "description": "dd", "position": 1}],
    )
    data = DDGSWebSearchProvider().search("q", 5)
    assert data["success"] is True
    assert data["data"]["web"][0]["title"] == "D"


def test_ddgs_not_installed(monkeypatch):
    monkeypatch.setattr(ddgs_mod, "_ddgs_importable", lambda: False)
    data = DDGSWebSearchProvider().search("q")
    assert data["success"] is False
    assert "ddgs" in data["error"]


def test_ddgs_is_available(monkeypatch):
    monkeypatch.setattr(ddgs_mod, "_ddgs_importable", lambda: True)
    monkeypatch.setattr(ddgs_mod, "provider_section", lambda name: _Section(enabled=True))
    assert DDGSWebSearchProvider().is_available() is True
    monkeypatch.setattr(ddgs_mod, "provider_section", lambda name: _Section(enabled=False))
    assert DDGSWebSearchProvider().is_available() is False


# ── searxng ────────────────────────────────────────────────────


def test_searxng_search_sorts_by_score(monkeypatch):
    monkeypatch.setattr(searxng_mod, "_searxng_url", lambda: "http://sx")

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResp({"results": [
            {"title": "A", "url": "http://a", "content": "ca", "score": 1},
            {"title": "B", "url": "http://b", "content": "cb", "score": 5},
        ]})

    monkeypatch.setattr(searxng_mod.httpx, "get", fake_get)
    data = SearXNGWebSearchProvider().search("q", 5)
    assert data["success"] is True
    # Higher score first.
    assert data["data"]["web"][0]["title"] == "B"


def test_searxng_no_url(monkeypatch):
    monkeypatch.setattr(searxng_mod, "_searxng_url", lambda: "")
    data = SearXNGWebSearchProvider().search("q")
    assert data["success"] is False


def test_searxng_is_available(monkeypatch):
    monkeypatch.setattr(
        searxng_mod, "provider_section", lambda name: _Section(enabled=True, url="http://sx"),
    )
    monkeypatch.setattr(searxng_mod, "_searxng_url", lambda: "http://sx")
    assert SearXNGWebSearchProvider().is_available() is True

    monkeypatch.setattr(
        searxng_mod, "provider_section", lambda name: _Section(enabled=False, url="http://sx"),
    )
    assert SearXNGWebSearchProvider().is_available() is False
