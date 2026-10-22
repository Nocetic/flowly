"""Core config the cloud machine image writes at boot (cloud/image/flowly_cloud_config.py)."""

import importlib.util
from pathlib import Path

import pytest

from flowly.config.schema import Config

SOURCE = Path(__file__).resolve().parents[1] / "cloud" / "image" / "flowly_cloud_config.py"


@pytest.fixture(scope="module")
def writer():
    spec = importlib.util.spec_from_file_location("flowly_cloud_config", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gateway_is_loopback_with_enforced_token_and_flowly_provider(writer):
    config = Config()
    config.gateway.host, config.gateway.port = "0.0.0.0", 1234
    changed = writer.apply(config, {"FLOWLY_GATEWAY_TOKEN": "edge-token", "FLOWLY_ACCOUNT_KEY": "flw_key"})
    assert (config.gateway.host, config.gateway.port) == ("127.0.0.1", 18790)
    assert config.gateway.require_auth is True and config.gateway.token == "edge-token"
    assert config.providers.flowly.enabled and config.providers.flowly.account_key == "flw_key"
    assert changed == ["gateway.token", "providers.flowly"]
    # Names only: the boot log never carries a value.
    assert "edge-token" not in repr(changed) and "flw_key" not in repr(changed)


def test_missing_token_is_generated_once_and_kept(writer):
    config = Config()
    assert writer.apply(config, {}) == ["gateway.token (generated)"]
    token = config.gateway.token
    assert len(token) >= 32
    assert writer.apply(config, {}) == [] and config.gateway.token == token


def test_rewriting_the_same_secrets_changes_nothing(writer):
    config = Config()
    environ = {"FLOWLY_GATEWAY_TOKEN": "t", "FLOWLY_ACCOUNT_KEY": "flw_key"}
    writer.apply(config, environ)
    assert writer.apply(config, environ) == []


def test_agent_secret_enables_the_server_channel_when_core_has_it(writer, capsys):
    config = Config()
    changed = writer.apply(config, {"FLOWLY_AGENT_SECRET": "s", "FLOWLY_GATEWAY_TOKEN": "t"})
    server = getattr(config.channels, "flowly_server", None)
    if server is None:
        assert "channels.flowlyServer" in capsys.readouterr().err
        assert "channels.flowlyServer.enabled" not in changed
    else:
        assert server.enabled and "channels.flowlyServer.enabled" in changed
        assert "s" not in {getattr(server, name, None) for name in ("agent_secret",)}  # secret stays in env
