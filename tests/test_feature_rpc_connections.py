"""Tests for the ``connections.*`` feature-RPC surface.

These RPCs back the integration cards the desktop Dashboard (RemoteConnectionsPanel)
and the iOS Connections view render generically — field-by-type — over both the
relay channel and the direct gateway. Contract pinned here:

* the FAL image-generation card (category ``media``) is exposed alongside
  channels/tools/voice, so it appears in BOTH clients with no client change;
* ``connections.set`` persists its values and ``connections.list`` masks the
  PASSWORD field back, never echoing the key in clear.
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


def test_media_is_a_connection_category():
    # The single gate that surfaces media cards to remote clients.
    assert "media" in feature_rpc.CONNECTION_CATEGORIES


def _list_by_key(isolated_home) -> dict:
    result, _ = _dispatch("connections.list")
    return {c["key"]: c for c in result["connections"]}


def test_connections_list_includes_fal_image(isolated_home):
    by_key = _list_by_key(isolated_home)
    assert "fal_image" in by_key, "FAL image card must be listed for remote clients"
    card = by_key["fal_image"]
    assert card["category"] == "media"
    # Rendered generically by the clients → it must carry its fields.
    field_keys = {f["key"] for f in card["fields"]}
    assert {"api_key", "model"} <= field_keys
    api_field = next(f for f in card["fields"] if f["key"] == "api_key")
    assert api_field["type"] == "password"


def test_connections_set_round_trips_media(isolated_home):
    result, restart = _dispatch("connections.set", {
        "key": "fal_image",
        "values": {"enabled": True, "api_key": "fal-secret"},
    })
    assert result["ok"] is True
    assert restart is True  # fal_image needs a gateway restart

    card = _list_by_key(isolated_home)["fal_image"]
    assert card["enabled"] is True
    # Key persisted but masked on the way out — never echoed in clear.
    assert card["values"]["api_key"] == "••••••••"
    assert card["values"]["api_key"] != "fal-secret"


def test_connections_set_clear_disables_media(isolated_home):
    _dispatch("connections.set", {"key": "fal_image", "values": {"enabled": True, "api_key": "fal-x"}})
    _dispatch("connections.set", {"key": "fal_image", "clear": True})
    card = _list_by_key(isolated_home)["fal_image"]
    assert card["enabled"] is False
