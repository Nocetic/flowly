from __future__ import annotations

import asyncio
import json
from pathlib import Path

from flowly.config.schema import Config
from flowly.integrations.active_provider import _build_for, resolve_active_provider


def test_ollama_builds_without_api_key() -> None:
    cfg = Config()
    active = _build_for(cfg, "ollama")

    assert active is not None
    assert active.key == "ollama"
    assert active.api_key == "ollama"
    assert active.api_base == "http://127.0.0.1:11434/v1"
    assert active.source == "Local · Ollama"


def test_ollama_is_not_used_by_empty_cascade() -> None:
    cfg = Config()
    cfg.providers.flowly.enabled = False
    cfg.providers.openai_codex.enabled = False
    cfg.providers.xai_oauth.enabled = False
    cfg.providers.zai_coding.enabled = False

    assert resolve_active_provider(cfg) is None


def test_explicit_ollama_active_resolves() -> None:
    cfg = Config()
    cfg.providers.active = "ollama"
    cfg.providers.ollama.api_base = "http://localhost:11434/v1"

    active = resolve_active_provider(cfg)

    assert active is not None
    assert active.key == "ollama"
    assert active.api_base == "http://localhost:11434/v1"


def test_config_legacy_helpers_support_active_ollama() -> None:
    cfg = Config()
    cfg.providers.active = "ollama"

    assert cfg.get_api_key() == "ollama"
    assert cfg.get_active_provider_name() == "ollama"
    assert cfg.get_api_base() == "http://127.0.0.1:11434/v1"


def test_set_active_provider_accepts_ollama_without_rewriting_model(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    from flowly.config.loader import get_config_path
    from flowly.integrations.active_provider import set_active_provider

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"agents": {"defaults": {"model": "minimax-m2.5:cloud"}}}),
        encoding="utf-8",
    )

    changed = set_active_provider("ollama")
    data = json.loads(path.read_text(encoding="utf-8"))

    assert changed is None
    assert data["providers"]["active"] == "ollama"
    assert data["agents"]["defaults"]["model"] == "minimax-m2.5:cloud"


def test_provider_list_surfaces_ollama_as_non_keyable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-cli-home"))

    from flowly.channels import feature_rpc

    result = feature_rpc.provider_list()
    by_key = {p["key"]: p for p in result["providers"]}

    assert "ollama" in by_key
    assert by_key["ollama"]["keyable"] is False
    assert by_key["ollama"]["hasKey"] is False


def test_ollama_model_catalog(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))

    from flowly.config.schema import Config
    from flowly.config.loader import save_config
    from flowly.integrations import model_catalog

    cfg = Config()
    cfg.providers.ollama.api_base = "http://ollama.test/v1"
    save_config(cfg)

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [
                    {"id": "minimax-m2.5:cloud", "owned_by": "library"},
                    {"id": "gemma4", "owned_by": "library"},
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, headers: dict):
            assert url == "http://ollama.test/v1/models"
            assert headers["Authorization"] == "Bearer ollama"
            return _Response()

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", _Client)
    model_catalog.flush_cache()

    models = asyncio.run(model_catalog.fetch_models("ollama", force_refresh=True))

    assert [m.id for m in models] == ["gemma4", "minimax-m2.5:cloud"]
    assert models[0].tags == ["local"]
    assert models[1].tags == ["cloud"]
