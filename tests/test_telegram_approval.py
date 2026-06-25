from __future__ import annotations

from types import SimpleNamespace

import pytest

from flowly.bus.queue import MessageBus
from flowly.agent.loop import AgentLoop
from flowly.channels.telegram import TELEGRAM_ALLOWED_UPDATES, TelegramChannel
from flowly.config.schema import TelegramConfig


def test_telegram_polling_includes_callback_queries() -> None:
    assert "message" in TELEGRAM_ALLOWED_UPDATES
    assert "callback_query" in TELEGRAM_ALLOWED_UPDATES


@pytest.mark.asyncio
async def test_telegram_approval_callback_warns_when_not_pending(monkeypatch) -> None:
    class FakeManager:
        def resolve(self, approval_id: str, decision: str) -> bool:
            assert approval_id == "approval-1"
            assert decision == "allow-once"
            return False

    monkeypatch.setattr(
        "flowly.exec.approval_manager.get_approval_manager",
        lambda: FakeManager(),
    )

    answers: list[dict] = []
    edits: list[dict] = []

    class FakeQuery:
        data = "exec:approval-1:allow-once"
        message = SimpleNamespace(
            text="Command approval required\n\necho '<unsafe>'\n\nExpires in 60s"
        )

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            answers.append({"text": text, "show_alert": show_alert})

        async def edit_message_text(self, text: str, parse_mode: str | None = None) -> None:
            edits.append({"text": text, "parse_mode": parse_mode})

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())

    await channel._on_callback_query(
        SimpleNamespace(callback_query=FakeQuery()),
        SimpleNamespace(),
    )

    assert answers == [
        {"text": "Approval expired or already handled", "show_alert": True}
    ]
    assert edits
    assert "Approval expired or already handled" in edits[0]["text"]
    assert "echo &#x27;&lt;unsafe&gt;&#x27;" in edits[0]["text"]
    assert edits[0]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_telegram_approval_callback_resolves_pending(monkeypatch) -> None:
    decisions: list[tuple[str, str]] = []

    class FakeManager:
        def resolve(self, approval_id: str, decision: str) -> bool:
            decisions.append((approval_id, decision))
            return True

    monkeypatch.setattr(
        "flowly.exec.approval_manager.get_approval_manager",
        lambda: FakeManager(),
    )

    answers: list[dict] = []
    edits: list[dict] = []

    class FakeQuery:
        data = "exec:approval-1:deny"
        message = SimpleNamespace(text="Command approval required\n\necho hi\n\nExpires in 60s")

        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            answers.append({"text": text, "show_alert": show_alert})

        async def edit_message_text(self, text: str, parse_mode: str | None = None) -> None:
            edits.append({"text": text, "parse_mode": parse_mode})

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())

    await channel._on_callback_query(
        SimpleNamespace(callback_query=FakeQuery()),
        SimpleNamespace(),
    )

    assert decisions == [("approval-1", "deny")]
    assert answers == [{"text": "Decision recorded", "show_alert": False}]
    assert edits
    assert "Denied" in edits[0]["text"]


@pytest.mark.asyncio
async def test_iteration_events_are_not_published_to_telegram() -> None:
    bus = MessageBus()
    agent = AgentLoop.__new__(AgentLoop)
    agent.bus = bus

    await agent._emit_iteration_event(
        outbound_channel="telegram",
        outbound_chat_id="123",
        outbound_run_id="run-1",
        iteration_idx=0,
        message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
    )

    assert bus.outbound.qsize() == 0


def _approval_button_texts(sent_kwargs) -> list[str]:
    markup = sent_kwargs["reply_markup"]
    return [btn.text for row in markup.inline_keyboard for btn in row]


@pytest.mark.asyncio
async def test_telegram_approval_offers_always_when_supported() -> None:
    sent: list[dict] = []

    class FakeBot:
        async def send_message(self, **kwargs) -> None:
            sent.append(kwargs)

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())
    channel._app = SimpleNamespace(bot=FakeBot())

    await channel.send_approval_prompt(123, "a1", "git push", 60, supports_always=True)

    texts = _approval_button_texts(sent[0])
    assert "✅ Always" in texts
    assert "✅ Allow" in texts
    assert "❌ Deny" in texts


@pytest.mark.asyncio
async def test_telegram_approval_hides_always_when_not_supported() -> None:
    sent: list[dict] = []

    class FakeBot:
        async def send_message(self, **kwargs) -> None:
            sent.append(kwargs)

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())
    channel._app = SimpleNamespace(bot=FakeBot())

    await channel.send_approval_prompt(
        123, "a1", "📧 Send email to a@b.com", 60, supports_always=False
    )

    texts = _approval_button_texts(sent[0])
    assert "✅ Always" not in texts
    assert "✅ Allow" in texts
    assert "❌ Deny" in texts


@pytest.mark.asyncio
async def test_telegram_skips_empty_text_messages() -> None:
    sent: list[dict] = []

    class FakeBot:
        async def send_message(self, **kwargs) -> None:
            sent.append(kwargs)

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())
    channel._app = SimpleNamespace(bot=FakeBot())

    await channel._send_text(123, "")
    await channel._send_text(123, "   ")

    assert sent == []


# --------------------------------------------------------------------- #
# Slash command menu + passthrough
# --------------------------------------------------------------------- #


def _fake_command_update(text: str):
    """Minimal Update for a slash-command message (private chat)."""
    user = SimpleNamespace(id=42, username="alice", first_name="Alice", last_name=None)
    message = SimpleNamespace(
        text=text,
        chat_id=12345,
        message_id=777,
        chat=SimpleNamespace(type="private"),
    )
    return SimpleNamespace(message=message, effective_user=user)


def test_telegram_menu_mirrors_gateway_registry() -> None:
    """The "/" menu must equal the central registry's gateway view — so a new
    gateway command shows up automatically and no TUI-only command leaks in as
    a dead menu entry."""
    from flowly.agent.slash_commands import gateway_commands

    names = {c.command for c in TelegramChannel.NATIVE_COMMANDS}
    assert names == {c.name for c in gateway_commands()}

    # Sanity: the registry's gateway view excludes cli-only UI commands.
    tui_only = {
        "theme", "model", "provider", "sessions", "board", "kanban",
        "image", "video", "paste", "quit", "browser", "mcp", "plugins",
    }
    assert names.isdisjoint(tui_only)


def test_telegram_menu_commands_valid_for_telegram_api() -> None:
    """Telegram rejects the WHOLE set_my_commands call if any entry is
    malformed (name 1-32 chars [a-z0-9_], description 1-256), which would
    silently leave the bot with no menu at all. Guard each entry."""
    import re

    for c in TelegramChannel.NATIVE_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c.command), c.command
        assert 1 <= len(c.description) <= 256, c.command


@pytest.mark.asyncio
async def test_telegram_command_passthrough_forwards_raw_text() -> None:
    """A command with no dedicated handler (/status) must reach the bus as
    raw text so the gateway can answer — not be silently dropped."""
    bus = MessageBus()
    channel = TelegramChannel(TelegramConfig(enabled=True, dm_policy="open"), bus)

    await channel._on_command(_fake_command_update("/status"), SimpleNamespace())
    await channel._stop_typing(12345)  # tear down the typing-indicator task

    assert bus.inbound.qsize() == 1
    msg = bus.inbound.get_nowait()
    assert msg.content == "/status"
    assert msg.channel == "telegram"
    assert msg.chat_id == "12345"
    # Forwarded as raw text (no is_command shortcut) so the gateway's
    # universal parser handles it the same way it does for Web/Desktop/iOS.
    assert msg.metadata.get("is_command") is None


@pytest.mark.asyncio
async def test_telegram_command_passthrough_preserves_args() -> None:
    """``/skills python`` keeps its argument so the gateway can filter."""
    bus = MessageBus()
    channel = TelegramChannel(TelegramConfig(enabled=True, dm_policy="open"), bus)

    await channel._on_command(_fake_command_update("/skills python"), SimpleNamespace())
    await channel._stop_typing(12345)

    msg = bus.inbound.get_nowait()
    assert msg.content == "/skills python"


@pytest.mark.asyncio
async def test_telegram_command_passthrough_blocked_for_unauthorized() -> None:
    """Unpaired users in allowlist mode are blocked — a command must not slip
    past the authorization gate that normal messages go through."""
    bus = MessageBus()
    channel = TelegramChannel(TelegramConfig(enabled=True, dm_policy="allowlist"), bus)

    await channel._on_command(_fake_command_update("/status"), SimpleNamespace())

    assert bus.inbound.qsize() == 0


@pytest.mark.asyncio
async def test_telegram_help_lists_every_menu_command() -> None:
    """/help is generated from NATIVE_COMMANDS, so it must mention every
    command in the menu — keeping help and the "/" menu in sync."""
    sent: list[dict] = []

    class FakeMessage:
        async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
            sent.append({"text": text, "parse_mode": parse_mode})

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())
    await channel._on_help(
        SimpleNamespace(message=FakeMessage()), SimpleNamespace()
    )

    assert sent
    body = sent[0]["text"]
    for cmd in TelegramChannel.NATIVE_COMMANDS:
        assert f"/{cmd.command}" in body
    assert sent[0]["parse_mode"] == "HTML"


# --------------------------------------------------------------------- #
# Long-message splitting (Telegram's 4096-char cap)
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_telegram_splits_long_messages() -> None:
    """A reply over Telegram's 4096 cap (e.g. /skills) must be split across
    several sends instead of failing with "Message is too long" and dropping
    the whole reply."""
    sent: list[dict] = []

    class FakeBot:
        async def send_message(self, **kwargs) -> None:
            sent.append(kwargs)

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())
    channel._app = SimpleNamespace(bot=FakeBot())

    long_content = "\n".join(f"line {i} " + "x" * 40 for i in range(200))
    assert len(long_content) > 4096
    await channel._send_text(123, long_content)

    assert len(sent) > 1  # split into multiple messages
    for kw in sent:
        assert len(kw["text"]) <= 4096  # every chunk fits the cap
        assert kw["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_telegram_short_message_not_split() -> None:
    """A normal-sized reply is still a single send, markdown rendered."""
    sent: list[dict] = []

    class FakeBot:
        async def send_message(self, **kwargs) -> None:
            sent.append(kwargs)

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())
    channel._app = SimpleNamespace(bot=FakeBot())

    await channel._send_text(123, "hello **world**")

    assert len(sent) == 1
    assert "<b>world</b>" in sent[0]["text"]


# --------------------------------------------------------------------- #
# Polling error handling (no traceback spam on network hiccups)
# --------------------------------------------------------------------- #


def test_telegram_polling_error_callback_is_sync() -> None:
    """PTB rejects a coroutine error_callback — it must be a plain function."""
    import asyncio

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())
    assert not asyncio.iscoroutinefunction(channel._on_polling_error)


def test_telegram_polling_network_error_is_warning_not_traceback() -> None:
    """A transient NetworkError logs a single warning; any other error keeps
    an ERROR-level entry."""
    from loguru import logger
    from telegram.error import NetworkError, TelegramError

    channel = TelegramChannel(TelegramConfig(enabled=True), MessageBus())

    records: list = []
    sink_id = logger.add(lambda m: records.append(m.record), level="DEBUG")
    try:
        channel._on_polling_error(
            NetworkError("[Errno 8] nodename nor servname provided, or not known")
        )
        channel._on_polling_error(TelegramError("unexpected failure"))
    finally:
        logger.remove(sink_id)

    levels = [r["level"].name for r in records]
    assert levels == ["WARNING", "ERROR"]
