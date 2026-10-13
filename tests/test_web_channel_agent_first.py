"""A message the agent writes first reaches a relay conversation.

The relay routes a bot's events by client session. A conversation nobody has
open has none, and a goal check-in sent under the conversation's own id was
dropped there: never stored, never pushed. The web channel now asks the relay
to open a session for it (``conversation.open``) and sends the final under that.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels import web as web_module
from flowly.channels.web import AGENT_FIRST_CONVERSATION_KEY, WebChannel
from flowly.config.schema import WebChannelConfig


class FakeRelay:
    """The relay socket: answers conversation.open the way the relay does."""

    def __init__(self, channel: WebChannel, answer: dict | None):
        self.channel, self.answer, self.sent = channel, answer, []

    async def send(self, payload: str) -> None:
        frame = json.loads(payload)
        self.sent.append(frame)
        if frame.get("type") == "conversation.open" and self.answer is not None:
            reply = {"type": "conversation.opened", "requestId": frame["requestId"], **self.answer}
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self.channel._handle_relay_message(self, reply)))


def _channel(answer: dict | None):
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    finals: list[dict] = []

    async def capture(payload: str) -> None:
        finals.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    relay = FakeRelay(channel, answer)
    channel._ws = relay
    return channel, relay, finals


def _check_in() -> OutboundMessage:
    return OutboundMessage(channel="web", chat_id="conv1", content="20 dakikanı yaptın mı?", metadata={
        AGENT_FIRST_CONVERSATION_KEY: "conv1", "session_key": "web:conv1",
    })


@pytest.mark.asyncio
async def test_the_final_goes_out_under_the_session_the_relay_opened():
    channel, relay, finals = _channel({"ok": True, "sessionId": "opened-session"})
    await channel.send(_check_in())
    assert relay.sent == [{"type": "conversation.open", "requestId": relay.sent[0]["requestId"], "conversationId": "conv1"}]
    [final] = finals
    assert final["sessionId"] == "opened-session"
    assert final["data"]["state"] == "final" and final["data"]["sessionKey"] == "web:conv1"
    assert final["data"]["message"]["content"][0]["text"] == "20 dakikanı yaptın mı?"
    assert channel._conversation_open_waiters == {}


@pytest.mark.asyncio
async def test_a_refusal_keeps_the_message_s_own_id(monkeypatch):
    channel, _relay, finals = _channel({"ok": False, "error": "not_found"})
    await channel.send(_check_in())
    assert finals[0]["sessionId"] == "conv1"


@pytest.mark.asyncio
async def test_a_relay_that_never_answers_is_retried_then_given_up(monkeypatch):
    monkeypatch.setattr(web_module, "_AGENT_FIRST_OPEN_TIMEOUT", 0.01)
    monkeypatch.setattr(web_module, "_AGENT_FIRST_OPEN_RETRY_SECONDS", 0)
    channel, relay, finals = _channel(None)
    await channel.send(_check_in())
    assert [frame["type"] for frame in relay.sent] == ["conversation.open"] * web_module._AGENT_FIRST_OPEN_ATTEMPTS
    assert finals[0]["sessionId"] == "conv1"
    assert channel._conversation_open_waiters == {}


@pytest.mark.asyncio
async def test_an_ordinary_reply_asks_for_nothing():
    channel, relay, finals = _channel({"ok": True, "sessionId": "opened-session"})
    await channel.send(OutboundMessage(channel="web", chat_id="browser-session", content="Hi"))
    assert relay.sent == []
    assert finals[0]["sessionId"] == "browser-session"
