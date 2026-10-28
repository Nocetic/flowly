"""A fresh install with no Flowly account and no key can still search.

Brave answers only with a key or a signed-in account, and DuckDuckGo was
off until turned on, so the first research a new owner asked for had no web
search at all (164 s of the agent working around it, 22 s with ddgs on).
"""

from __future__ import annotations

import pytest

import flowly.agent.tools.web_providers.ddgs as ddgs_mod
from flowly.agent.tools.web_providers import registry
from flowly.agent.tools.web_providers.brave import BraveWebSearchProvider
from flowly.agent.tools.web_providers.ddgs import DDGSWebSearchProvider
from flowly.config.schema import DdgsProviderConfig


class _Brave(BraveWebSearchProvider):
    """Brave as a fresh install has it: no key, no account."""

    available = False

    def is_available(self) -> bool:
        return self.available


@pytest.fixture
def nothing_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(registry, "_providers", {})
    monkeypatch.setattr(registry, "_loaded", True)
    registry.register_provider(_Brave())
    registry.register_provider(DDGSWebSearchProvider())
    monkeypatch.setattr(registry, "_read_config_backend", lambda capability: "")
    monkeypatch.setattr(registry, "_read_default_backend", lambda: "")
    monkeypatch.setattr(ddgs_mod, "_ddgs_importable", lambda: True)


def _ddgs_config(monkeypatch: pytest.MonkeyPatch, config: DdgsProviderConfig) -> None:
    monkeypatch.setattr(ddgs_mod, "provider_section", lambda name: config)


def test_a_fresh_install_searches_with_duckduckgo(monkeypatch, nothing_configured) -> None:
    _ddgs_config(monkeypatch, DdgsProviderConfig())

    provider = registry.get_active_search_provider()

    assert provider is not None and provider.name == "ddgs"


def test_duckduckgo_turned_off_stays_off(monkeypatch, nothing_configured) -> None:
    _ddgs_config(monkeypatch, DdgsProviderConfig(enabled=False))

    assert registry.get_active_search_provider() is None


def test_a_working_backend_still_wins(monkeypatch, nothing_configured) -> None:
    _ddgs_config(monkeypatch, DdgsProviderConfig())
    brave = registry.get_provider("brave")
    assert brave is not None
    monkeypatch.setattr(brave, "available", True)

    assert registry.get_active_search_provider() is brave


def test_its_card_shows_it_on_until_turned_off() -> None:
    from flowly.integrations.registry import get_card

    card = get_card("web_ddgs")
    assert card is not None
    assert next(field for field in card.fields if field.key == "enabled").default is True
