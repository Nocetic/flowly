"""Flowly-hosted catalog metadata and default-model reconciliation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import flowly.integrations.model_catalog as catalog
from flowly.config.schema import Config
from flowly.integrations.active_provider import ActiveProvider
from flowly.integrations.model_catalog import FlowlyModelPolicy, Model


def _write_config(home: Path, model: str) -> Path:
    path = home / "config.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "active": "flowly",
                    "flowly": {
                        "enabled": True,
                        "accountKey": "flw_test",
                    },
                },
                "agents": {"defaults": {"model": model}},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_reconcile_replaces_model_missing_from_account_catalog(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    path = _write_config(tmp_path, "moonshotai/kimi-k2.5")
    monkeypatch.setattr(
        catalog,
        "_CACHE",
        {
            "flowly": [
                Model(
                    id="anthropic/claude-haiku-4.5",
                    name="Claude Haiku 4.5",
                ),
                Model(
                    id="deepseek/deepseek-v4-flash",
                    name="DeepSeek V4 Flash",
                ),
            ]
        },
    )
    monkeypatch.setattr(
        catalog,
        "_FLOWLY_POLICY",
        FlowlyModelPolicy(
            plan="free",
            default_model="anthropic/claude-haiku-4.5",
        ),
    )

    changed = asyncio.run(catalog.reconcile_flowly_model(force_refresh=False))

    assert changed == "anthropic/claude-haiku-4.5"
    assert json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"] == changed


def test_reconcile_preserves_valid_paid_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    path = _write_config(tmp_path, "moonshotai/kimi-k2.5")
    monkeypatch.setattr(
        catalog,
        "_CACHE",
        {
            "flowly": [
                Model(
                    id="anthropic/claude-haiku-4.5",
                    name="Claude Haiku 4.5",
                ),
                Model(
                    id="moonshotai/kimi-k2.5",
                    name="Kimi K2.5",
                ),
            ]
        },
    )
    monkeypatch.setattr(
        catalog,
        "_FLOWLY_POLICY",
        FlowlyModelPolicy(
            plan="pro",
            default_model="anthropic/claude-haiku-4.5",
        ),
    )

    changed = asyncio.run(catalog.reconcile_flowly_model(force_refresh=False))

    assert changed is None
    assert (
        json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"]
        == "moonshotai/kimi-k2.5"
    )


def test_flowly_fetch_retains_plan_and_default(monkeypatch) -> None:
    cfg = Config()
    cfg.providers.active = "flowly"
    cfg.providers.flowly.enabled = True
    cfg.providers.flowly.account_key = "flw_test"

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "plan": "free",
                "default_model": "anthropic/claude-haiku-4.5",
                "data": [
                    {
                        "id": "anthropic/claude-haiku-4.5",
                        "name": "Claude Haiku 4.5",
                        "pricing": {"prompt": "0", "completion": "0"},
                        "allowed": True,
                    }
                ],
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, *args, **kwargs) -> _Response:
            return _Response()

    monkeypatch.setattr("flowly.config.loader.load_config", lambda: cfg)
    monkeypatch.setattr(
        "flowly.integrations.active_provider.resolve_active_provider",
        lambda _cfg: ActiveProvider(
            key="flowly",
            api_key="flw_test",
            api_base="https://useflowlyapp.com/api/v1",
            source="test",
        ),
    )
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kwargs: _Client())
    catalog.flush_cache()

    models = asyncio.run(catalog.fetch_models("flowly", force_refresh=True))

    assert [model.id for model in models] == ["anthropic/claude-haiku-4.5"]
    assert catalog.get_flowly_model_policy() == FlowlyModelPolicy(
        plan="free",
        default_model="anthropic/claude-haiku-4.5",
    )
