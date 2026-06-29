"""Tests for web-search backends on the ``connections.*`` feature-RPC surface.

Web-search providers are rendered as integration cards in the Desktop /
iOS / Android connections tab. The single gate that surfaces them to remote
clients (over both relay and the direct gateway) is the ``web_search``
connection category.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flowly.channels import feature_rpc


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


def _dispatch(method: str, params: dict | None = None):
    return asyncio.run(feature_rpc.dispatch(method, params or {}))


def _list_by_key() -> dict:
    result, _ = _dispatch("connections.list")
    return {c["key"]: c for c in result["connections"]}


def test_web_search_is_a_connection_category():
    assert "web_search" in feature_rpc.CONNECTION_CATEGORIES


def test_connections_list_includes_brave(isolated_home):
    by_key = _list_by_key()
    assert "web_brave" in by_key, "Brave card must be listed for remote clients"
    card = by_key["web_brave"]
    assert card["category"] == "web_search"
    field_keys = {f["key"] for f in card["fields"]}
    assert {"enabled", "api_key"} <= field_keys
    api_field = next(f for f in card["fields"] if f["key"] == "api_key")
    assert api_field["type"] == "password"


def test_brave_card_round_trips(isolated_home):
    result, restart = _dispatch("connections.set", {
        "key": "web_brave",
        "values": {"enabled": True, "api_key": "BSA-secret"},
    })
    assert result["ok"] is True
    # Search providers resolve per call — no gateway restart needed.
    assert restart is False

    card = _list_by_key()["web_brave"]
    assert card["enabled"] is True
    # Key persisted but masked on the way out.
    assert card["values"]["api_key"] == "••••••••"
    assert card["values"]["api_key"] != "BSA-secret"


def test_brave_probe_status(isolated_home):
    # No key, not logged in → not_configured.
    card = _list_by_key()["web_brave"]
    assert card["probeStatus"] == "not_configured"

    # With a key → ok / connected.
    _dispatch("connections.set", {
        "key": "web_brave", "values": {"enabled": True, "api_key": "BSA-x"},
    })
    card = _list_by_key()["web_brave"]
    assert card["probeStatus"] == "ok"
    assert card["connected"] is True


def test_brave_card_clear(isolated_home):
    _dispatch("connections.set", {
        "key": "web_brave", "values": {"enabled": True, "api_key": "BSA-x"},
    })
    _dispatch("connections.set", {"key": "web_brave", "clear": True})
    card = _list_by_key()["web_brave"]
    # Brave is the default backend: clearing removes the custom key but the
    # toggle reverts to its default (on) rather than disabling web search.
    assert not card["values"].get("api_key")
    assert card["enabled"] is True
