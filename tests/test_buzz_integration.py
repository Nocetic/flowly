from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from flowly.agent.prompt_blocks import build_platform_hint
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.buzz import BuzzChannel, CommandResult, discover_buzz_channels
from flowly.channels.manager import ChannelManager
from flowly.config.schema import Config
from flowly.diagnostics.checks import check_channels
from flowly.diagnostics.models import DoctorContext, Status
from flowly.integrations.config_io import apply_card_values, read_card_values
from flowly.integrations.probes import probe_buzz
from flowly.integrations.registry import get_card


def _buzz_card():
    card = get_card("buzz")
    assert card is not None
    return card


def test_buzz_card_persists_private_key_only_in_profile_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)
    card = _buzz_card()

    values = {field.key: field.default for field in card.fields}
    values.update(
        {
            "enabled": True,
            "relay_url": "https://relay.example",
            "private_key": "nsec1profile-secret",
            "allow_from": ["a" * 64],
        }
    )
    apply_card_values(card, values)

    raw_config = (tmp_path / "config.json").read_text(encoding="utf-8")
    dotenv = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "nsec1profile-secret" not in raw_config
    assert "privateKey" not in raw_config
    assert "BUZZ_PRIVATE_KEY=nsec1profile-secret" in dotenv
    assert json.loads(raw_config)["channels"]["buzz"]["relayUrl"] == "https://relay.example"
    assert read_card_values(card)["private_key"] == "nsec1profile-secret"


def test_channel_manager_initializes_buzz_adapter() -> None:
    config = Config()
    config.channels.buzz.enabled = True

    manager = ChannelManager(config, MessageBus())

    assert isinstance(manager.channels["buzz"], BuzzChannel)


@pytest.mark.asyncio
async def test_buzz_probe_uses_read_only_identity_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    async def fake_execute(binary, args, **kwargs):
        captured["binary"] = binary
        captured["args"] = args
        captured["private_key"] = kwargs["private_key"]
        return CommandResult(
            0,
            json.dumps({"pubkey": "a" * 64, "display_name": "Flowly Bot"}),
            "",
        )

    monkeypatch.setattr("flowly.channels.buzz._resolve_buzz_binary", lambda _path: "/bin/buzz")
    monkeypatch.setattr("flowly.channels.buzz._execute_buzz", fake_execute)

    result = await probe_buzz(
        {
            "enabled": True,
            "relay_url": "https://relay.example",
            "private_key": "nsec1secret",
        }
    )

    assert result.status == "ok"
    assert result.detail == "as Flowly Bot"
    assert captured["args"] == ["users", "get"]
    assert captured["private_key"] == "nsec1secret"


@pytest.mark.asyncio
async def test_buzz_discovery_returns_named_joined_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def fake_execute(binary, args, **kwargs):
        calls.append((binary, args, kwargs["private_key"]))
        if args == ["users", "get"]:
            return CommandResult(
                0,
                json.dumps({"pubkey": "a" * 64, "display_name": "Flowly"}),
                "",
            )
        assert args == ["channels", "list"]
        return CommandResult(
            0,
            json.dumps(
                [
                    {
                        "channel_id": "b-channel",
                        "name": "Random",
                        "description": "Off topic",
                    },
                    {
                        "channel_id": "a-channel",
                        "name": "General",
                        "description": "Community chat",
                    },
                ]
            ),
            "",
        )

    monkeypatch.setattr("flowly.channels.buzz._resolve_buzz_binary", lambda _path: "/bin/buzz")
    monkeypatch.setattr("flowly.channels.buzz._execute_buzz", fake_execute)

    result = await discover_buzz_channels(
        {
            "relay_url": "https://relay.example",
            "private_key": "nsec1secret",
        }
    )

    assert result["ok"] is True
    assert result["identity"]["displayName"] == "Flowly"
    assert [channel["name"] for channel in result["channels"]] == ["General", "Random"]
    assert [call[1] for call in calls] == [["users", "get"], ["channels", "list"]]
    assert all(call[2] == "nsec1secret" for call in calls)
    assert "nsec1secret" not in repr(result)


def test_buzz_discovery_rpc_reuses_saved_secret_and_is_advertised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)
    card = _buzz_card()
    values = {field.key: field.default for field in card.fields}
    values.update(
        {
            "enabled": True,
            "relay_url": "https://relay.example",
            "private_key": "saved-secret",
        }
    )
    apply_card_values(card, values)
    captured = {}

    async def fake_execute(binary, args, **kwargs):
        captured.setdefault("keys", []).append(kwargs["private_key"])
        if args == ["users", "get"]:
            return CommandResult(
                0,
                json.dumps({"pubkey": "a" * 64, "display_name": "Flowly"}),
                "",
            )
        return CommandResult(
            0,
            json.dumps([{"channel_id": "channel-1", "name": "General"}]),
            "",
        )

    monkeypatch.setattr("flowly.channels.buzz._resolve_buzz_binary", lambda _path: "/bin/buzz")
    monkeypatch.setattr("flowly.channels.buzz._execute_buzz", fake_execute)

    result, restart = asyncio.run(
        feature_rpc.dispatch(
            "connections.discover",
            {
                "key": "buzz",
                "values": {
                    "private_key": "",
                    "relay_url": "https://relay.example",
                },
            },
        )
    )

    assert restart is False
    assert result["channels"] == [
        {"id": "channel-1", "name": "General", "description": ""}
    ]
    assert captured["keys"] == ["saved-secret", "saved-secret"]
    assert "connections.discover" in feature_rpc.FEATURE_METHODS
    assert "saved-secret" not in repr(result)

    listed, _ = asyncio.run(feature_rpc.dispatch("connections.list", {}))
    buzz = next(item for item in listed["connections"] if item["key"] == "buzz")
    assert buzz["discoverable"] is True


def test_doctor_accepts_complete_buzz_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "buzz"
    binary.write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text("BUZZ_PRIVATE_KEY=nsec1secret\n", encoding="utf-8")
    config = Config()
    config.channels.buzz.enabled = True
    config.channels.buzz.relay_url = "https://relay.example"
    config.channels.buzz.cli_path = str(binary)
    config.channels.buzz.allow_from = ["a" * 64]
    ctx = DoctorContext(data_dir=tmp_path, config=config)
    monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)

    check_channels(ctx)

    assert ctx.results[-1].status == Status.OK
    assert "buzz" in ctx.results[-1].message


def test_doctor_reports_buzz_security_and_credentials_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config()
    config.channels.buzz.enabled = True
    ctx = DoctorContext(data_dir=tmp_path, config=config)
    monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("BUZZ_RELAY_URL", raising=False)
    monkeypatch.delenv("BUZZ_CLI_PATH", raising=False)
    monkeypatch.setattr(
        "flowly.diagnostics.checks._buzz_private_key_present",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        "flowly.diagnostics.checks._buzz_binary_present",
        lambda *_args: False,
    )

    check_channels(ctx)

    result = ctx.results[-1]
    assert result.status == Status.WARN
    assert "buzz: community relay URL is missing" in result.detail
    assert "buzz: Nostr private key is missing" in result.detail
    assert "buzz: Buzz CLI binary is missing" in result.detail
    assert "buzz: access is enabled but no senders are allowed" in result.detail


def test_buzz_platform_hint_describes_markdown_and_media() -> None:
    hint = build_platform_hint("buzz")

    assert hint.startswith("# Channel — Buzz")
    assert "Markdown" in hint
    assert "MEDIA:" in hint
