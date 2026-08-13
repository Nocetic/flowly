"""Tests for the ``connections.*`` feature-RPC surface.

These RPCs back the integration cards the desktop Dashboard (RemoteConnectionsPanel)
and the iOS Connections view render generically — field-by-type — over both the
relay channel and the direct gateway. Contract pinned here:

* the FAL image-generation card (category ``media``) is exposed alongside
  channels/tools/voice, so it appears in BOTH clients with no client change;
* ``connections.set`` persists its values and ``connections.list`` masks the
  PASSWORD field back, exposing only a non-reusable suffix preview;
* a full secret requires an explicit, field-scoped ``connections.secret.get``.
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


def test_connections_list_includes_media_generation(isolated_home):
    by_key = _list_by_key(isolated_home)
    assert "fal_image" in by_key, "the media card must be listed for remote clients"
    card = by_key["fal_image"]
    assert card["category"] == "media"
    # Rendered generically by the clients → it must carry its fields. The card
    # now covers video as well as images, with one model choice per kind.
    field_keys = {f["key"] for f in card["fields"]}
    assert {
        "api_key",
        "defaults.text_to_image",
        "defaults.text_to_video",
        "defaults.image_to_video",
    } <= field_keys
    api_field = next(f for f in card["fields"] if f["key"] == "api_key")
    assert api_field["type"] == "password"


def test_model_fields_ask_for_a_picker_without_pinning_a_list(isolated_home):
    """The catalog changes weekly; a baked-in choice list would go stale.

    So model fields stay plain text with a picker HINT: a client that
    understands it shows a searchable picker fed by media.models.search, and
    one that doesn't still renders a usable input.
    """
    card = _list_by_key(isolated_home)["fal_image"]
    video = next(f for f in card["fields"] if f["key"] == "defaults.text_to_video")

    assert video["type"] == "text"
    assert video["picker"] == "media_model:text-to-video"
    assert video["choices"] == []


def test_fields_without_a_picker_omit_the_hint(isolated_home):
    """Old clients must see exactly the payload they saw before."""
    card = _list_by_key(isolated_home)["fal_image"]
    api_field = next(f for f in card["fields"] if f["key"] == "api_key")
    assert "picker" not in api_field


def test_nested_model_choices_round_trip_to_config(isolated_home):
    result, _restart = _dispatch("connections.set", {
        "key": "fal_image",
        "values": {
            "enabled": True,
            "api_key": "fal-secret",
            "defaults.text_to_video": "vendor/t2v",
        },
    })
    assert result["ok"] is True

    from flowly.config.loader import load_config
    from flowly.media.settings import resolve_media_settings

    settings = resolve_media_settings(load_config().tools)
    assert settings.text_to_video == "vendor/t2v"
    assert settings.video_ready is True


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
    assert card["secretPreviews"]["api_key"] == "••••••••…cret"
    assert "fal-secret" not in repr(card)


def test_connection_secret_requires_explicit_password_field(isolated_home):
    _dispatch("connections.set", {
        "key": "fal_image",
        "values": {"enabled": True, "api_key": "fal-secret"},
    })

    result, restart = _dispatch("connections.secret.get", {
        "key": "fal_image",
        "field": "api_key",
    })
    assert result == {"value": "fal-secret"}
    assert restart is False

    with pytest.raises(feature_rpc.FeatureRpcError, match="only password fields"):
        _dispatch("connections.secret.get", {
            "key": "fal_image",
            "field": "enabled",
        })


def test_short_connection_secret_never_leaks_in_preview(isolated_home):
    _dispatch("connections.set", {
        "key": "fal_image",
        "values": {"enabled": True, "api_key": "tiny"},
    })
    card = _list_by_key(isolated_home)["fal_image"]
    assert card["secretPreviews"]["api_key"] == "••••••••"
    assert "tiny" not in repr(card)


def test_connections_set_clear_disables_media(isolated_home):
    _dispatch("connections.set", {"key": "fal_image", "values": {"enabled": True, "api_key": "fal-x"}})
    _dispatch("connections.set", {"key": "fal_image", "clear": True})
    card = _list_by_key(isolated_home)["fal_image"]
    assert card["enabled"] is False


def test_provider_rpc_sets_zai_coding_key_in_credential_store(isolated_home, monkeypatch):
    from flowly.auth import zai_coding

    monkeypatch.setattr(zai_coding, "_try_keyring", lambda: None)
    monkeypatch.setenv("OPENCODE_AUTH_PATH", str(Path(isolated_home) / "missing-opencode.json"))

    result, restart = _dispatch("provider.set_key", {
        "key": "zai_coding",
        "value": "glm-plan-key",
    })

    assert result["ok"] is True
    assert result["hasKey"] is True
    assert restart is True
    payload = zai_coding.load_token_payload(include_external=False)
    assert payload is not None
    assert payload.api_key == "glm-plan-key"

    providers, _ = _dispatch("provider.list")
    by_key = {p["key"]: p for p in providers["providers"]}
    assert by_key["zai_coding"]["keyable"] is True
    assert by_key["zai_coding"]["hasKey"] is True
