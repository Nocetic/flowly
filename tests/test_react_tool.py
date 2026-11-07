"""The agent's reactions: the ``react`` tool, the hub
that carries it, and the ``chat.reaction`` event the Flowly server reads."""

from __future__ import annotations

import json

import pytest

import flowly.agent.reactions as reactions
import flowly.profile as profiles
from flowly.agent.reactions import ReactionHub, is_reaction_emoji
from flowly.agent.tools.react import ReactTool
from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.routing import infer_toolset, resolve_toolset_filters
from flowly.agent_home import HOME_SESSION
from flowly.bus.queue import MessageBus
from flowly.channels.flowly_server import PROFILE_CHAT_EVENTS
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig


@pytest.fixture(autouse=True)
def hub(monkeypatch):
    fresh = ReactionHub()
    monkeypatch.setattr(reactions, "_hub", fresh)
    return fresh


def test_one_emoji_only():
    for ok in ["👍", "❤️", "👍🏽", "🇹🇷", "👨‍👩‍👧", "🙏", "😂"]:
        assert is_reaction_emoji(ok), ok
    for no in ["", "ok", "👍 ", " 👍", "a👍", "👍 nice", "1", "👍👍", "👍" * 9, "🇹", "🇹🇷🇹", None, 3]:
        assert not is_reaction_emoji(no), no


@pytest.mark.asyncio
async def test_the_tool_hands_the_reaction_to_every_surface(hub):
    carried: list[tuple[str, str]] = []

    async def surface(session_key: str, emoji: str) -> None:
        carried.append((session_key, emoji))

    hub.add_callback(surface)
    result = await ReactTool().execute(emoji="👍", session_key="web:c1")
    assert carried == [("web:c1", "👍")]
    assert result == "Reacted 👍 to the user's latest message."


@pytest.mark.asyncio
async def test_the_tool_refuses_words_and_chats_without_reactions(hub):
    tool = ReactTool()
    assert (await tool.execute(emoji="sounds good", session_key="web:c1")).startswith("Error: emoji must be")
    assert (await tool.execute(emoji="👍", session_key="web:c1")).startswith("Error: reactions are not available"), \
        "nothing carries reactions here (a CLI run)"

    async def surface(session_key: str, emoji: str) -> None:
        return None

    hub.add_callback(surface)
    assert (await tool.execute(emoji="👍")).startswith("Error: reactions are not available"), "no chat to react in"


@pytest.mark.asyncio
async def test_a_failing_surface_never_stops_another(hub):
    carried: list[str] = []

    async def broken(session_key: str, emoji: str) -> None:
        raise RuntimeError("socket gone")

    async def fine(session_key: str, emoji: str) -> None:
        carried.append(emoji)

    hub.add_callback(broken)
    hub.add_callback(fine)
    assert await hub.emit("web:c1", "❤️") is True
    assert carried == ["❤️"]


def test_only_chats_that_draw_reactions_offer_it_and_never_a_run_with_nobody_there():
    registry = ToolRegistry()
    registry.register(ReactTool())
    assert registry.is_available("react", platform="web")
    assert registry.is_available("react", platform="desktop")
    assert not registry.is_available("react", platform="telegram")
    assert infer_toolset("react") == "interactive"
    _enabled, disabled = resolve_toolset_filters("cron")
    assert "interactive" in disabled


@pytest.mark.asyncio
async def test_the_web_channel_sends_chat_reaction_to_the_chats_session():
    channel = WebChannel(config=WebChannelConfig(enabled=True), bus=MessageBus())
    sent: list[dict] = []

    async def capture(payload: str) -> None:
        sent.append(json.loads(payload))

    channel._send_or_queue = capture  # type: ignore[method-assign]
    channel._session_key_to_relay_id["web:c1"] = "srv-delivery-1"
    await channel.send_reaction_event("web:c1", "👍")
    await channel.send_reaction_event("web:unknown", "👍")
    assert sent == [{"type": "event", "sessionId": "srv-delivery-1", "event": "chat.reaction",
                     "data": {"sessionKey": "web:c1", "emoji": "👍"}}]


def test_a_named_agents_reaction_goes_to_the_server_with_its_chat():
    assert "chat.reaction" in PROFILE_CHAT_EVENTS


@pytest.fixture
def home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / ".flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    monkeypatch.setenv("FLOWLY_HOME", str(default))
    default.mkdir(parents=True)
    (default / "workspace").mkdir()
    profiles.create_profile("writer", local_runtime=True)
    return default


@pytest.mark.asyncio
async def test_a_named_agents_reaction_reaches_the_server_on_its_delivery_session(home):
    from tests.test_flowly_server_profile_chat import SESSION, Socket, deliver, make_channel

    channel, host, forwarded = make_channel()
    await deliver(channel, Socket())
    await host._emit("writer", "chat.reaction", {"sessionKey": HOME_SESSION, "emoji": "👍"})
    assert forwarded == [{"type": "event", "sessionId": SESSION, "event": "chat.reaction", "profile": "writer",
                          "data": {"sessionKey": HOME_SESSION, "emoji": "👍"}}]
