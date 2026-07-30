from __future__ import annotations

import asyncio
import json
import sys
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels._buzz_nostr import hex_to_npub
from flowly.channels.buzz import (
    BuzzChannel,
    CommandResult,
    ConversationState,
    _execute_buzz,
    _private_key_from_sources,
    _resolve_buzz_binary,
)
from flowly.config.schema import BuzzConfig

SELF_PRIVATE_KEY = "00" * 31 + "03"
SELF_PUBLIC_KEY = "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"
OTHER_PUBLIC_KEY = "b" * 64
CHANNEL_ID = "channel-1"


def _channel(**overrides) -> BuzzChannel:
    values = {
        "enabled": True,
        "relay_url": "https://relay.example",
        "allow_from": [OTHER_PUBLIC_KEY],
    }
    values.update(overrides)
    channel = BuzzChannel(BuzzConfig(**values), MessageBus())
    channel._private_key = SELF_PRIVATE_KEY
    channel._self_pubkey = SELF_PUBLIC_KEY
    channel._self_npub = hex_to_npub(SELF_PUBLIC_KEY)
    channel._display_name = "Flowly"
    channel._conversations[CHANNEL_ID] = ConversationState(kind="group")
    channel._channel_names[CHANNEL_ID] = "General"
    return channel


def _event(
    *,
    event_id: str = "event-1",
    content: str = "@Flowly hello",
    pubkey: str = OTHER_PUBLIC_KEY,
    created_at: int = 100,
    kind: int = 9,
    tags: list | None = None,
) -> dict:
    return {
        "id": event_id,
        "content": content,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags or [],
    }


def test_buzz_config_is_private_and_mention_gated_by_default() -> None:
    config = BuzzConfig()

    assert config.enabled is False
    assert config.allow_all_users is False
    assert config.allow_from == []
    assert config.group_policy == "mention"
    assert config.transport == "auto"
    assert config.poll_interval_seconds == 4.0


def test_access_control_is_default_deny_and_accepts_npub_or_hex() -> None:
    denied = _channel(allow_from=[])
    open_channel = _channel(allow_from=[], allow_all_users=True)
    npub_channel = _channel(allow_from=[hex_to_npub(OTHER_PUBLIC_KEY)])

    assert denied.is_allowed(OTHER_PUBLIC_KEY) is False
    assert open_channel.is_allowed(OTHER_PUBLIC_KEY) is True
    assert npub_channel.is_allowed(OTHER_PUBLIC_KEY) is True
    assert npub_channel.is_allowed("c" * 64) is False


def test_binary_resolution_prefers_explicit_path_then_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "buzz"
    binary.write_text("#!/bin/sh\n")

    assert _resolve_buzz_binary(str(binary)) == str(binary)

    monkeypatch.setattr("flowly.channels.buzz.shutil.which", lambda name: "/usr/local/bin/buzz")
    assert _resolve_buzz_binary("") == "/usr/local/bin/buzz"


def test_private_key_resolution_prefers_environment_then_credentials_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "profile"))
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"nsec": "file-secret"}))
    config = BuzzConfig(credentials_file=str(credentials))

    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "env-secret")
    assert _private_key_from_sources(config) == "env-secret"

    monkeypatch.delenv("BUZZ_PRIVATE_KEY")
    assert _private_key_from_sources(config) == "file-secret"


def test_home_channel_falls_back_to_watched_then_discovered_channel() -> None:
    watched = _channel(channels=["watched-channel"])
    assert watched._home_channel() == "watched-channel"

    discovered = _channel(channels=[])
    discovered._channel_names = {
        "joined-channel": "General",
        "another-channel": "Random",
    }
    assert discovered._home_channel() == "joined-channel"


@pytest.mark.asyncio
async def test_cli_execution_keeps_private_key_out_of_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, input_data):
            captured["input"] = input_data
            return b"[]", b""

    async def fake_subprocess(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    result = await _execute_buzz(
        "/usr/local/bin/buzz",
        ["messages", "send", "--channel", CHANNEL_ID, "--content", "-"],
        relay_url="https://relay.example",
        private_key="super-secret-nsec",
        input_text="hello",
    )

    assert result.returncode == 0
    assert "super-secret-nsec" not in repr(captured["args"])
    assert captured["env"]["BUZZ_PRIVATE_KEY"] == "super-secret-nsec"
    assert captured["env"]["BUZZ_RELAY_URL"] == "https://relay.example"
    assert captured["input"] == b"hello"


@pytest.mark.asyncio
async def test_cli_launch_failure_becomes_a_command_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_to_start(*_args, **_kwargs):
        raise FileNotFoundError("missing binary")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_to_start)

    result = await _execute_buzz(
        "/missing/buzz",
        ["users", "get"],
        relay_url="https://relay.example",
        private_key="secret",
    )

    assert result.returncode == 127
    assert "launch_failed" in result.stderr
    assert "secret" not in result.stderr


@pytest.mark.asyncio
async def test_seed_records_history_without_dispatching() -> None:
    channel = _channel()
    received = []
    channel.bus.publish_inbound = received.append

    async def fake_call(args, **_kwargs):
        assert args[:2] == ["messages", "get"]
        return CommandResult(
            0,
            json.dumps(
                [
                    _event(event_id="old-1", created_at=10),
                    _event(event_id="old-2", created_at=20),
                ]
            ),
            "",
        )

    channel._call = fake_call

    await channel._seed_conversation(CHANNEL_ID, kind="group")

    state = channel._conversations[CHANNEL_ID]
    assert state.last_timestamp == 20
    assert list(state.seen) == ["old-1", "old-2"]
    assert received == []


@pytest.mark.asyncio
async def test_dynamic_dm_discovery_starts_at_current_time_without_history_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel()
    channel._conversations.clear()

    async def fake_call(args, **_kwargs):
        if args == ["dms", "list"]:
            return CommandResult(0, json.dumps({"dm_id": "new-dm"}), "")
        return CommandResult(0, "[]", "")

    monkeypatch.setattr("flowly.channels.buzz.time.time", lambda: 500)
    channel._call = fake_call

    await channel._discover_direct_messages(seed=False)

    assert channel._conversations["new-dm"].kind == "dm"
    assert channel._conversations["new-dm"].last_timestamp == 500


@pytest.mark.asyncio
async def test_inbound_event_is_filtered_stripped_and_dispatched_once() -> None:
    channel = _channel()

    async def resolve_name(_pubkey: str) -> str:
        return "Alice"

    channel._resolve_user_name = resolve_name

    await channel._handle_event(CHANNEL_ID, _event(content="@Flowly: /whoami"))
    await channel._handle_event(CHANNEL_ID, _event(content="@Flowly: /whoami"))

    inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=0.1)
    assert inbound.channel == "buzz"
    assert inbound.chat_id == CHANNEL_ID
    assert inbound.sender_id == OTHER_PUBLIC_KEY
    assert inbound.content == "/whoami"
    assert inbound.metadata["message_id"] == "event-1"
    assert inbound.metadata["chat_type"] == "group"
    assert inbound.metadata["sender_name"] == "Alice"
    assert channel.bus.inbound.empty()


@pytest.mark.asyncio
async def test_unmentioned_group_message_is_ignored_but_dm_is_dispatched() -> None:
    channel = _channel()

    await channel._handle_event(CHANNEL_ID, _event(content="hello"))
    assert channel.bus.inbound.empty()

    channel._conversations[CHANNEL_ID].kind = "dm"
    await channel._handle_event(
        CHANNEL_ID,
        _event(event_id="event-2", content="hello"),
    )
    inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=0.1)
    assert inbound.content == "hello"
    assert inbound.metadata["chat_type"] == "dm"


@pytest.mark.asyncio
async def test_self_echo_non_chat_and_unauthorized_events_are_ignored() -> None:
    channel = _channel()

    await channel._handle_event(
        CHANNEL_ID,
        _event(event_id="self", pubkey=SELF_PUBLIC_KEY),
    )
    await channel._handle_event(
        CHANNEL_ID,
        _event(event_id="metadata", kind=44100),
    )
    await channel._handle_event(
        CHANNEL_ID,
        _event(event_id="blocked", pubkey="c" * 64),
    )

    assert channel.bus.inbound.empty()


@pytest.mark.asyncio
async def test_malformed_event_numbers_are_ignored_without_crashing() -> None:
    channel = _channel()

    await channel._handle_event(
        CHANNEL_ID,
        _event(event_id="malformed", created_at="not-a-number", kind="invalid"),
    )

    assert channel.bus.inbound.empty()
    assert "malformed" in channel._conversations[CHANNEL_ID].seen


@pytest.mark.asyncio
async def test_structural_p_tag_latches_dm_before_mention_gate() -> None:
    channel = _channel(channels=[])
    channel._channel_metadata[CHANNEL_ID] = {
        "channel_id": CHANNEL_ID,
        "name": "DM",
        "description": "",
    }

    await channel._handle_event(
        CHANNEL_ID,
        _event(
            content="no mention needed",
            tags=[["p", SELF_PUBLIC_KEY]],
        ),
    )

    inbound = await asyncio.wait_for(channel.bus.consume_inbound(), timeout=0.1)
    assert channel._conversations[CHANNEL_ID].kind == "dm"
    assert inbound.content == "no mention needed"
    assert inbound.metadata["chat_type"] == "dm"


@pytest.mark.asyncio
async def test_real_channel_metadata_prevents_false_dm_latch() -> None:
    channel = _channel(channels=[])
    channel._channel_metadata[CHANNEL_ID] = {
        "channel_id": CHANNEL_ID,
        "name": "General",
        "description": "Community chat",
    }

    await channel._handle_event(
        CHANNEL_ID,
        _event(content="ordinary message", tags=[["p", SELF_PUBLIC_KEY]]),
    )

    assert channel._conversations[CHANNEL_ID].kind == "group"
    assert channel.bus.inbound.empty()


def test_seen_event_cache_is_bounded() -> None:
    channel = _channel()
    state = channel._conversations[CHANNEL_ID]
    state.seen = OrderedDict((f"event-{index}", None) for index in range(550))

    channel._trim_seen(state)

    assert len(state.seen) == 500
    assert "event-0" not in state.seen
    assert "event-549" in state.seen


@pytest.mark.asyncio
async def test_send_uses_stdin_reply_and_media_without_exposing_secret(
    tmp_path: Path,
) -> None:
    channel = _channel()
    channel._binary = "/usr/local/bin/buzz"
    media = tmp_path / "image.png"
    media.write_bytes(b"png")
    captured = {}

    async def fake_call(args, *, input_text=None, timeout=30.0):
        captured["args"] = args
        captured["input_text"] = input_text
        captured["timeout"] = timeout
        return CommandResult(0, json.dumps({"accepted": True, "event_id": "sent-1"}), "")

    channel._call = fake_call
    await channel.send(
        OutboundMessage(
            channel="buzz",
            chat_id=CHANNEL_ID,
            content="hello",
            reply_to="parent-1",
            media=[str(media)],
        )
    )

    assert captured["args"] == [
        "messages",
        "send",
        "--channel",
        CHANNEL_ID,
        "--content",
        "-",
        "--reply-to",
        "parent-1",
        "--file",
        str(media),
    ]
    assert captured["input_text"] == "hello"
    assert "sent-1" in channel._conversations[CHANNEL_ID].seen


@pytest.mark.asyncio
async def test_send_uses_home_channel_when_message_target_is_empty() -> None:
    channel = _channel(home_channel="home-channel")
    channel._binary = "/usr/local/bin/buzz"
    captured = {}

    async def fake_call(args, **kwargs):
        captured["args"] = args
        captured["input_text"] = kwargs["input_text"]
        return CommandResult(0, "{}", "")

    channel._call = fake_call
    await channel.send(OutboundMessage(channel="buzz", chat_id="", content="scheduled"))

    assert captured["args"][3] == "home-channel"
    assert captured["input_text"] == "scheduled"


@pytest.mark.asyncio
async def test_polling_continues_after_one_failed_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel()
    channel._running = True
    attempts = 0

    async def no_wait(_seconds):
        return None

    async def flaky_poll(_channel_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")
        channel._running = False

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    channel._poll_conversation = flaky_poll

    await channel._poll_loop()

    assert attempts == 2


@pytest.mark.asyncio
async def test_polling_overlaps_one_second_and_relies_on_deduplication() -> None:
    channel = _channel()
    channel._conversations[CHANNEL_ID].last_timestamp = 100
    captured = {}

    async def fake_call(args, **_kwargs):
        captured["args"] = args
        return CommandResult(0, "[]", "")

    channel._call = fake_call

    await channel._poll_conversation(CHANNEL_ID)

    assert captured["args"][-2:] == ["--since", "99"]


@pytest.mark.asyncio
async def test_auto_transport_starts_polling_after_websocket_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel(transport="auto")
    channel._running = True
    polling_started = 0

    class FailedConnection:
        async def __aenter__(self):
            raise ConnectionError("relay unavailable")

        async def __aexit__(self, *_args):
            return False

    def fake_connect(*_args, **_kwargs):
        return FailedConnection()

    def start_polling():
        nonlocal polling_started
        polling_started += 1
        channel._running = False

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=fake_connect))
    channel._ensure_polling = start_polling

    await channel._websocket_loop()

    assert polling_started == 1


def test_websocket_url_preserves_path_and_query() -> None:
    channel = _channel(relay_url="https://relay.example/community?token=public")

    assert channel._websocket_url() == "wss://relay.example/community?token=public"

    channel.config.relay_url = "ftp://relay.example"
    with pytest.raises(ValueError):
        channel._websocket_url()


@pytest.mark.asyncio
async def test_websocket_authentication_signs_challenge() -> None:
    channel = _channel(auth_tag=json.dumps(["auth", "d" * 64, "", "e" * 128]))

    class FakeSocket:
        def __init__(self):
            self.responses = [
                json.dumps(["AUTH", "challenge-1"]),
            ]
            self.sent = []

        async def recv(self):
            if self.responses:
                return self.responses.pop(0)
            event_id = self.sent[0][1]["id"]
            return json.dumps(["OK", event_id, True, "accepted"])

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    socket = FakeSocket()
    await channel._authenticate(socket)

    assert socket.sent[0][0] == "AUTH"
    assert socket.sent[0][1]["kind"] == 22242
    assert ["challenge", "challenge-1"] in socket.sent[0][1]["tags"]


@pytest.mark.asyncio
async def test_websocket_subscriptions_resume_with_overlap() -> None:
    channel = _channel()
    channel._conversations[CHANNEL_ID].last_timestamp = 100
    channel._membership_since = 300

    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    socket = FakeSocket()
    subscriptions = await channel._subscribe(socket)

    assert subscriptions["flowly-buzz-0"] == CHANNEL_ID
    assert socket.sent[0][2]["#h"] == [CHANNEL_ID]
    assert socket.sent[0][2]["since"] == 99
    assert socket.sent[1][2]["kinds"] == [44100]
    assert socket.sent[1][2]["since"] == 299
