"""Pre-launch breaking-point fixes.

C1 — config.set must write config.json atomically, back it up, and keep owner-only
     (0600) perms, because it holds provider API keys. config.get must tolerate a
     transiently malformed file.
C2 — switching the active provider must NOT rewrite agents.defaults.model to the
     target's bare default when the target isn't usable (no key) — otherwise the
     cascade serves a different provider that 404s on that id.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


def _dispatch(method: str, params: dict | None = None):
    from flowly.channels import feature_rpc
    return asyncio.run(feature_rpc.dispatch(method, params or {}))


def _config_path() -> Path:
    from flowly.config.loader import get_config_path
    return get_config_path()


# ── C1 ──────────────────────────────────────────────────────────────────────

def test_config_set_is_atomic_secure_and_backed_up(isolated_home):
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"providers": {"openrouter": {"apiKey": "sk-old"}}}), encoding="utf-8")
    os.chmod(path, 0o644)  # simulate a loose-perm file (the regression)

    result, _ = _dispatch("config.set", {"config": {"providers": {"anthropic": {"apiKey": "sk-new"}}}})
    assert result["ok"] is True

    # merge preserved both providers
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["providers"]["openrouter"]["apiKey"] == "sk-old"
    assert data["providers"]["anthropic"]["apiKey"] == "sk-new"

    # a .bak of the previous file exists
    assert path.with_suffix(path.suffix + ".bak").exists()

    # owner-only perms restored (POSIX only — Windows uses ACLs)
    if sys.platform != "win32":
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_config_get_tolerates_malformed_file(isolated_home):
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json", encoding="utf-8")
    result, _ = _dispatch("config.get")  # must not raise
    assert result["config"] == {}


# ── C2 ──────────────────────────────────────────────────────────────────────

def _write_cfg(cfg: dict) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg), encoding="utf-8")


def test_switch_to_unusable_provider_keeps_model(isolated_home):
    # openrouter has a key (the cascade winner); anthropic has NO key.
    _write_cfg({
        "providers": {"active": "openrouter", "openrouter": {"apiKey": "sk-or-x"}},
        "agents": {"defaults": {"model": "openai/gpt-4o"}},
    })
    from flowly.integrations.active_provider import set_active_provider

    changed = set_active_provider("anthropic")
    data = json.loads(_config_path().read_text(encoding="utf-8"))

    # active switched, but the model is LEFT ALONE (anthropic can't serve it,
    # so the cascade keeps serving openrouter/openai-gpt-4o).
    assert data["providers"]["active"] == "anthropic"
    assert changed is None
    assert data["agents"]["defaults"]["model"] == "openai/gpt-4o"


def test_switch_to_usable_provider_rewrites_model(isolated_home):
    # anthropic HAS a key now → switching should adopt its curated default.
    from flowly.integrations.active_provider import DEFAULT_MODELS, set_active_provider

    _write_cfg({
        "providers": {
            "active": "openrouter",
            "openrouter": {"apiKey": "sk-or-x"},
            "anthropic": {"apiKey": "sk-ant-x"},
        },
        "agents": {"defaults": {"model": "openai/gpt-4o"}},
    })

    changed = set_active_provider("anthropic")
    data = json.loads(_config_path().read_text(encoding="utf-8"))

    assert data["providers"]["active"] == "anthropic"
    assert changed == DEFAULT_MODELS["anthropic"]
    assert data["agents"]["defaults"]["model"] == DEFAULT_MODELS["anthropic"]
