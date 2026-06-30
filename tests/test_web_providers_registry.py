"""Tests for the web search/extract provider registry + resolution."""

from __future__ import annotations

import pytest

from flowly.agent.tools.web_providers import registry as reg
from flowly.agent.tools.web_providers.base import WebSearchProvider


class _Fake(WebSearchProvider):
    def __init__(
        self,
        name: str,
        *,
        available: bool = True,
        search: bool = True,
        extract: bool = False,
    ) -> None:
        self._name = name
        self._available = available
        self._search = search
        self._extract = extract

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def supports_search(self) -> bool:
        return self._search

    def supports_extract(self) -> bool:
        return self._extract

    def search(self, query, limit=5):
        return {"success": True, "data": {"web": []}}

    def extract(self, urls, **kwargs):
        return []


@pytest.fixture(autouse=True)
def _reset_registry():
    reg._reset_for_tests()
    # Skip plugin discovery in these hermetic unit tests.
    reg._loaded = True
    yield
    reg._reset_for_tests()


# ── registration ───────────────────────────────────────────────


def test_register_get_list():
    brave = _Fake("brave")
    exa = _Fake("exa", extract=True)
    reg.register_provider(brave)
    reg.register_provider(exa)

    assert reg.get_provider("brave") is brave
    assert reg.get_provider(" exa ") is exa  # trimmed
    assert reg.get_provider("nope") is None
    assert [p.name for p in reg.list_providers()] == ["brave", "exa"]


def test_register_rejects_non_provider():
    with pytest.raises(TypeError):
        reg.register_provider(object())  # type: ignore[arg-type]


def test_reregister_overwrites():
    a = _Fake("brave")
    b = _Fake("brave")
    reg.register_provider(a)
    reg.register_provider(b)
    assert reg.get_provider("brave") is b


# ── resolution: explicit config ────────────────────────────────


def test_explicit_config_wins():
    reg.register_provider(_Fake("brave"))
    reg.register_provider(_Fake("exa", extract=True))
    assert reg._resolve("exa", capability="search").name == "exa"


def test_explicit_unavailable_still_returned():
    # An explicitly configured backend is returned even when unavailable
    # so the tool surfaces a precise "not configured" error.
    reg.register_provider(_Fake("tavily", available=False, extract=True))
    assert reg._resolve("tavily", capability="extract").name == "tavily"


def test_explicit_unknown_falls_back():
    reg.register_provider(_Fake("brave"))
    assert reg._resolve("nope", capability="search").name == "brave"


def test_explicit_capability_mismatch_falls_back():
    # brave is search-only; asked for extract → fall through to exa.
    reg.register_provider(_Fake("brave"))
    reg.register_provider(_Fake("exa", search=False, extract=True))
    assert reg._resolve("brave", capability="extract").name == "exa"


# ── resolution: fallback ───────────────────────────────────────


def test_single_eligible_shortcut():
    reg.register_provider(_Fake("exa", extract=True))
    assert reg._resolve("", capability="extract").name == "exa"


def test_preference_order_when_many():
    # Both available + search-capable → brave wins (first in preference).
    reg.register_provider(_Fake("exa"))
    reg.register_provider(_Fake("brave"))
    assert reg._resolve("", capability="search").name == "brave"


def test_availability_filter_excludes_unavailable():
    reg.register_provider(_Fake("exa", available=False))
    assert reg._resolve("", capability="search") is None


def test_no_provider_returns_none():
    assert reg._resolve("", capability="search") is None


# ── public accessors read config ───────────────────────────────


def test_get_active_uses_config(monkeypatch):
    reg.register_provider(_Fake("brave"))
    reg.register_provider(_Fake("exa", extract=True))
    monkeypatch.setattr(reg, "_read_config_backend", lambda cap: "exa")
    assert reg.get_active_search_provider().name == "exa"
    assert reg.get_active_extract_provider().name == "exa"


def test_get_active_uses_default_flag(monkeypatch):
    # When no explicit backend string is set, the card "default" flag picks
    # the active backend (overriding the availability preference order).
    reg.register_provider(_Fake("brave"))
    reg.register_provider(_Fake("ddgs"))
    monkeypatch.setattr(reg, "_read_config_backend", lambda cap: "")
    monkeypatch.setattr(reg, "_read_default_backend", lambda: "ddgs")
    assert reg.get_active_search_provider().name == "ddgs"


def test_explicit_backend_beats_default_flag(monkeypatch):
    reg.register_provider(_Fake("brave"))
    reg.register_provider(_Fake("ddgs"))
    monkeypatch.setattr(reg, "_read_config_backend", lambda cap: "brave")
    monkeypatch.setattr(reg, "_read_default_backend", lambda: "ddgs")
    assert reg.get_active_search_provider().name == "brave"
