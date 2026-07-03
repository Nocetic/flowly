"""Z.AI GLM Coding Plan credential resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowly.auth import zai_coding
from flowly.config.schema import Config
from flowly.integrations.active_provider import _build_for, resolve_active_provider


@pytest.fixture(autouse=True)
def isolated_flowly_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "flowly"))
    monkeypatch.setenv("OPENCODE_AUTH_PATH", str(tmp_path / "opencode" / "auth.json"))
    monkeypatch.delenv(zai_coding.FLOWLY_ZAI_CODING_API_KEY_ENV, raising=False)
    monkeypatch.delenv("OPENCODE_AUTH_CONTENT", raising=False)
    monkeypatch.setattr(zai_coding, "_try_keyring", lambda: None)


def test_base_url_is_pinned_to_coding_plan_endpoint():
    assert (
        zai_coding.validate_zai_coding_base_url("https://api.z.ai/api/coding/paas/v4/")
        == zai_coding.DEFAULT_ZAI_CODING_BASE_URL
    )
    with pytest.raises(zai_coding.ZaiCodingAuthError):
        zai_coding.validate_zai_coding_base_url("http://api.z.ai/api/coding/paas/v4")
    with pytest.raises(zai_coding.ZaiCodingAuthError):
        zai_coding.validate_zai_coding_base_url("https://evil.example/api/coding/paas/v4")
    with pytest.raises(zai_coding.ZaiCodingAuthError):
        zai_coding.validate_zai_coding_base_url("https://api.z.ai/api/paas/v4")


def test_token_storage_roundtrip_uses_credentials_dir():
    backend = zai_coding.save_api_key("zai-plan-key")
    assert backend.startswith("file:")
    assert zai_coding.credentials_path().exists()

    loaded = zai_coding.load_token_payload()
    assert loaded is not None
    assert loaded.api_key == "zai-plan-key"
    assert loaded.source == "flowly"
    assert loaded.base_url == zai_coding.DEFAULT_ZAI_CODING_BASE_URL


def test_opencode_auth_json_is_a_fallback_source():
    path = zai_coding.opencode_auth_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "zai": {
            "type": "api",
            "key": "opencode-zai-key",
            "metadata": {"baseURL": zai_coding.DEFAULT_ZAI_CODING_BASE_URL},
        },
    }), encoding="utf-8")

    loaded = zai_coding.load_token_payload()

    assert loaded is not None
    assert loaded.api_key == "opencode-zai-key"
    assert loaded.source == "opencode"
    assert loaded.provider_id == "zai"


def test_opencode_regular_api_endpoint_is_not_used_as_coding_plan():
    path = zai_coding.opencode_auth_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "zai": {
            "type": "api",
            "key": "regular-zai-key",
            "metadata": {"baseURL": "https://api.z.ai/api/paas/v4"},
        },
    }), encoding="utf-8")

    assert zai_coding.load_token_payload() is None


def test_flowly_store_wins_over_opencode_auth_json():
    zai_coding.save_api_key("flowly-key")
    path = zai_coding.opencode_auth_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "zai": {"type": "api", "key": "opencode-key"},
    }), encoding="utf-8")

    loaded = zai_coding.load_token_payload()

    assert loaded is not None
    assert loaded.api_key == "flowly-key"
    assert loaded.source == "flowly"


def test_active_provider_resolves_zai_coding_from_flowly_store():
    zai_coding.save_api_key("flowly-key")
    cfg = Config()
    cfg.providers.active = "zai_coding"

    ap = _build_for(cfg, "zai_coding")

    assert ap is not None
    assert ap.key == "zai_coding"
    assert ap.api_key == "flowly-key"
    assert ap.api_base == zai_coding.DEFAULT_ZAI_CODING_BASE_URL
    assert "GLM Coding Plan" in ap.source
    assert resolve_active_provider(cfg) == ap

    cfg.providers.zai_coding.enabled = False
    assert _build_for(cfg, "zai_coding") is None


def test_env_key_is_last_resort(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(zai_coding.FLOWLY_ZAI_CODING_API_KEY_ENV, "env-zai-key")

    loaded = zai_coding.load_token_payload()

    assert loaded is not None
    assert loaded.api_key == "env-zai-key"
    assert loaded.source == "env"
