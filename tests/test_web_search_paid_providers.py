"""tavily / exa / firecrawl / parallel providers (mocked network + SDKs)."""

from __future__ import annotations

import asyncio

import flowly.agent.tools.web_providers.exa as exa_mod
import flowly.agent.tools.web_providers.firecrawl as fc_mod
import flowly.agent.tools.web_providers.parallel as par_mod
import flowly.agent.tools.web_providers.tavily as tav_mod
from flowly.agent.tools.web_providers.exa import ExaWebSearchProvider
from flowly.agent.tools.web_providers.firecrawl import FirecrawlWebSearchProvider
from flowly.agent.tools.web_providers.parallel import ParallelWebSearchProvider
from flowly.agent.tools.web_providers.tavily import TavilyWebSearchProvider


class _Section:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ── tavily ─────────────────────────────────────────────────────


def test_tavily_search(monkeypatch):
    monkeypatch.setattr(tav_mod, "_tavily_request",
                        lambda ep, payload: {"results": [{"title": "T", "url": "u", "content": "c"}]})
    data = TavilyWebSearchProvider().search("q")
    assert data["success"] is True
    assert data["data"]["web"][0]["title"] == "T"
    assert data["data"]["web"][0]["description"] == "c"


def test_tavily_extract(monkeypatch):
    monkeypatch.setattr(tav_mod, "_tavily_request",
                        lambda ep, payload: {"results": [{"url": "u", "title": "T", "raw_content": "RC"}]})
    res = TavilyWebSearchProvider().extract(["u"])
    assert res[0]["content"] == "RC"
    assert res[0]["metadata"]["sourceURL"] == "u"


def test_tavily_is_available(monkeypatch):
    monkeypatch.setattr(tav_mod, "provider_section", lambda n: _Section(enabled=True, api_key="k"))
    monkeypatch.setattr(tav_mod, "_tavily_key", lambda: "k")
    assert TavilyWebSearchProvider().is_available() is True
    monkeypatch.setattr(tav_mod, "provider_section", lambda n: _Section(enabled=False, api_key="k"))
    assert TavilyWebSearchProvider().is_available() is False


# ── exa ────────────────────────────────────────────────────────


class _ExaResult:
    def __init__(self, url, title, highlights=None, text=None):
        self.url, self.title, self.highlights, self.text = url, title, highlights, text


class _ExaResp:
    def __init__(self, results):
        self.results = results


class _ExaClient:
    def search(self, q, num_results=5, contents=None):
        return _ExaResp([_ExaResult("u", "T", highlights=["h1", "h2"])])

    def get_contents(self, urls, text=True):
        return _ExaResp([_ExaResult("u", "T", text="CONTENT")])


def test_exa_search(monkeypatch):
    monkeypatch.setattr(exa_mod, "_get_client", lambda: _ExaClient())
    data = ExaWebSearchProvider().search("q")
    assert data["success"] is True
    assert data["data"]["web"][0]["description"] == "h1 h2"


def test_exa_extract(monkeypatch):
    monkeypatch.setattr(exa_mod, "_get_client", lambda: _ExaClient())
    res = ExaWebSearchProvider().extract(["u"])
    assert res[0]["content"] == "CONTENT"


# ── firecrawl ──────────────────────────────────────────────────


class _FCClient:
    def search(self, query, limit):
        return {"data": [{"title": "T", "url": "u", "description": "d"}]}

    def scrape(self, url, formats):
        return {"markdown": "MD", "metadata": {"title": "FT", "sourceURL": url}}


def test_firecrawl_search(monkeypatch):
    monkeypatch.setattr(fc_mod, "_get_client", lambda: _FCClient())
    data = FirecrawlWebSearchProvider().search("q")
    assert data["data"]["web"][0]["title"] == "T"


def test_firecrawl_extract(monkeypatch):
    monkeypatch.setattr(fc_mod, "_get_client", lambda: _FCClient())
    res = asyncio.run(FirecrawlWebSearchProvider().extract(["https://example.com"]))
    assert res[0]["content"] == "MD"
    assert res[0]["title"] == "FT"


# ── parallel ───────────────────────────────────────────────────


class _PResult:
    def __init__(self, url, title, excerpts=None, full_content=None):
        self.url, self.title, self.excerpts, self.full_content = url, title, excerpts, full_content


class _PResp:
    def __init__(self, results, errors=None):
        self.results, self.errors = results, errors


class _Beta:
    def __init__(self, search_resp=None, extract_resp=None):
        self._s, self._e = search_resp, extract_resp

    def search(self, **kw):
        return self._s

    async def extract(self, **kw):
        return self._e


class _PClient:
    def __init__(self, beta):
        self.beta = beta


def test_parallel_search(monkeypatch):
    resp = _PResp([_PResult("u", "T", excerpts=["e1", "e2"])])
    monkeypatch.setattr(par_mod, "_get_sync_client", lambda: _PClient(_Beta(search_resp=resp)))
    data = ParallelWebSearchProvider().search("q")
    assert data["data"]["web"][0]["description"] == "e1 e2"


def test_parallel_extract(monkeypatch):
    resp = _PResp([_PResult("u", "T", full_content="FC")])
    monkeypatch.setattr(par_mod, "_get_async_client", lambda: _PClient(_Beta(extract_resp=resp)))
    res = asyncio.run(ParallelWebSearchProvider().extract(["u"]))
    assert res[0]["content"] == "FC"


# ── capabilities ───────────────────────────────────────────────


def test_all_paid_providers_support_search_and_extract():
    for p in (TavilyWebSearchProvider(), ExaWebSearchProvider(),
              FirecrawlWebSearchProvider(), ParallelWebSearchProvider()):
        assert p.supports_search() is True
        assert p.supports_extract() is True
