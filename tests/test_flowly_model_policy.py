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


def test_reconcile_never_rewrites_a_user_choice_absent_from_stale_catalog(
    tmp_path: Path, monkeypatch,
) -> None:
    """The Ozan bug: a freshly released model the user picked is missing from
    yesterday's cached catalog — reconcile must NOT snap it back to the
    backend default on every restart. Only legacy schema defaults migrate."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    path = _write_config(tmp_path, "deepseek/deepseek-v4-flash-0731")
    monkeypatch.setattr(
        catalog,
        "_CACHE",
        {
            "flowly": [
                Model(id="anthropic/claude-haiku-4.5", name="Claude Haiku 4.5"),
                Model(id="deepseek/deepseek-chat", name="DeepSeek Chat"),
            ]
        },
    )
    monkeypatch.setattr(
        catalog,
        "_FLOWLY_POLICY",
        FlowlyModelPolicy(plan="pro", default_model="anthropic/claude-haiku-4.5"),
    )

    changed = asyncio.run(catalog.reconcile_flowly_model(force_refresh=False))

    assert changed is None
    assert (
        json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"]
        == "deepseek/deepseek-v4-flash-0731"
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


def test_a_model_the_plan_lacks_is_locked_with_the_plan_that_opens_it() -> None:
    row = catalog._flowly_catalog_row({
        "id": "anthropic/claude-sonnet-5.5", "name": "Claude Sonnet 5.5", "allowed": False,
        "required_plan": "pro", "pricing": {"prompt": "0.000002", "completion": "0.00001"},
    })
    assert row is not None
    assert "locked" in row.tags and "plan:pro" in row.tags


def test_an_allowed_model_carries_no_lock_and_odd_rows_are_skipped() -> None:
    row = catalog._flowly_catalog_row({"id": "deepseek/deepseek-v4-flash", "allowed": True, "required_plan": "starter", "pricing": {}})
    assert row is not None
    assert "locked" not in row.tags and not any(tag.startswith("plan:") for tag in row.tags)
    assert catalog._flowly_catalog_row({"id": ""}) is None
    assert catalog._flowly_catalog_row("nope") is None
    weird = catalog._flowly_catalog_row({"id": "x/y", "allowed": False, "required_plan": "galaxy"})
    assert weird is not None and "locked" in weird.tags and not any(tag.startswith("plan:") for tag in weird.tags)


def _plan(monkeypatch, plan: str, default: str, listed: list[str]) -> None:
    monkeypatch.setattr(catalog, "_CACHE", {"flowly": [Model(id=m, name=m) for m in listed]})
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", FlowlyModelPolicy(plan=plan, default_model=default))


def _model(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"]


def test_a_free_account_moves_off_the_paid_default_it_cannot_call(tmp_path: Path, monkeypatch) -> None:
    """2026-11-12, a new macOS user on Free: the agent started on Haiku 5.5,
    which Free does not list, and its first message came back 403."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    path = _write_config(tmp_path, "anthropic/claude-haiku-5.5")
    _plan(monkeypatch, "free", "anthropic/claude-haiku-4.5",
          ["anthropic/claude-haiku-4.5", "deepseek/deepseek-v4-flash", "tencent/hy3"])

    assert asyncio.run(catalog.reconcile_flowly_model(force_refresh=False)) == "anthropic/claude-haiku-4.5"
    assert _model(path) == "anthropic/claude-haiku-4.5"


def test_a_paid_account_moves_off_free_default_to_its_own(tmp_path: Path, monkeypatch) -> None:
    """Starter and up run Haiku 5.5: an agent upgraded from Free leaves 4.5,
    which a paid plan keeps callable but never lists."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    path = _write_config(tmp_path, "anthropic/claude-haiku-4.5")
    _plan(monkeypatch, "starter", "anthropic/claude-haiku-5.5",
          ["anthropic/claude-haiku-5.5", "deepseek/deepseek-v4-flash"])

    assert asyncio.run(catalog.reconcile_flowly_model(force_refresh=False)) == "anthropic/claude-haiku-5.5"
    assert _model(path) == "anthropic/claude-haiku-5.5"


def test_a_default_the_plan_lists_stays(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    path = _write_config(tmp_path, "anthropic/claude-haiku-5.5")
    _plan(monkeypatch, "pro", "anthropic/claude-haiku-5.5",
          ["anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5"])

    assert asyncio.run(catalog.reconcile_flowly_model(force_refresh=False)) is None
    assert _model(path) == "anthropic/claude-haiku-5.5"


def test_no_catalog_no_change(tmp_path: Path, monkeypatch) -> None:
    """Offline or signed out: the policy is unknown, so nothing moves."""
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    path = _write_config(tmp_path, "anthropic/claude-haiku-5.5")
    monkeypatch.setattr(catalog, "_CACHE", {"flowly": [Model(id="anthropic/claude-haiku-4.5", name="x")]})
    monkeypatch.setattr(catalog, "_FLOWLY_POLICY", None)

    assert asyncio.run(catalog.reconcile_flowly_model(force_refresh=False)) is None
    assert _model(path) == "anthropic/claude-haiku-5.5"


def test_the_gateway_switches_the_running_provider_only_when_the_model_moved(tmp_path: Path, monkeypatch) -> None:
    from flowly.cli.gateway_cmd import _reconcile_flowly_default

    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    path = _write_config(tmp_path, "anthropic/claude-haiku-5.5")
    _plan(monkeypatch, "free", "anthropic/claude-haiku-4.5", ["anthropic/claude-haiku-4.5"])
    reloads: list[int] = []

    async def reload() -> dict:
        reloads.append(1)
        return {"ok": True}

    assert asyncio.run(_reconcile_flowly_default(reload)) == "anthropic/claude-haiku-4.5"
    assert reloads == [1]
    assert asyncio.run(_reconcile_flowly_default(reload)) is None
    assert reloads == [1]
    assert _model(path) == "anthropic/claude-haiku-4.5"
