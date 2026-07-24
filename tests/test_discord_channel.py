"""Tests for the Discord channel's server-message gating and speaker labels.

MESSAGE_CREATE payloads are fed straight into ``_handle_message_create``;
no gateway or REST call is made. Before ``group_policy`` existed the bot
answered every message in every server channel it could read.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from flowly.channels.discord import DiscordChannel
from flowly.config.schema import DiscordConfig


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_channel(
    group_policy: str = "mention",
    group_allow_from: list[str] | None = None,
) -> tuple[DiscordChannel, list[dict]]:
    config = DiscordConfig(
        enabled=True,
        token="t",
        group_policy=group_policy,
        group_allow_from=group_allow_from or [],
    )
    channel = DiscordChannel(config, MagicMock())
    channel._bot_user_id = "BOT1"

    handled: list[dict] = []

    async def record(**kwargs):
        handled.append(kwargs)

    channel._handle_message = record  # type: ignore[method-assign]

    async def no_typing(channel_id: str) -> None:
        return None

    channel._start_typing = no_typing  # type: ignore[method-assign]
    return channel, handled


def _payload(
    content: str,
    guild_id: str | None = "G1",
    mentions: list[dict] | None = None,
    referenced_author: str | None = None,
    nick: str | None = None,
) -> dict:
    p: dict = {
        "id": "M1",
        "channel_id": "CH1",
        "content": content,
        "author": {"id": "U1", "username": "hakan", "global_name": "Hakan"},
        "mentions": mentions or [],
    }
    if guild_id:
        p["guild_id"] = guild_id
    if referenced_author:
        p["referenced_message"] = {"author": {"id": referenced_author}}
    if nick:
        p["member"] = {"nick": nick}
    return p


def _feed(channel: DiscordChannel, payload: dict) -> None:
    _run(channel._handle_message_create(payload))


def _bot_mention(uid: str = "BOT1") -> dict:
    return {"id": uid, "username": "flowly", "global_name": "Flowly"}


# ── group policy gating ─────────────────────────────────────────────


def test_mention_policy_ignores_plain_guild_message():
    channel, handled = _make_channel("mention")
    _feed(channel, _payload("günaydın ekip"))
    assert handled == []


def test_mention_policy_answers_when_bot_is_mentioned():
    channel, handled = _make_channel("mention")
    _feed(channel, _payload("<@BOT1> özet çıkar", mentions=[_bot_mention()]))
    assert len(handled) == 1
    assert "özet çıkar" in handled[0]["content"]


def test_mention_policy_answers_replies_to_the_bot():
    channel, handled = _make_channel("mention")
    _feed(channel, _payload("devamını yazar mısın", referenced_author="BOT1"))
    assert len(handled) == 1


def test_open_policy_answers_plain_guild_messages():
    channel, handled = _make_channel("open")
    _feed(channel, _payload("herkese merhaba"))
    assert len(handled) == 1


def test_allowlist_policy_gates_by_channel_id():
    channel, handled = _make_channel("allowlist", group_allow_from=["CH1"])
    _feed(channel, _payload("selam"))
    assert len(handled) == 1

    other, other_handled = _make_channel("allowlist", group_allow_from=["CH9"])
    _feed(other, _payload("selam"))
    assert other_handled == []


def test_dms_bypass_group_policy():
    channel, handled = _make_channel("mention")
    _feed(channel, _payload("selam", guild_id=None))
    assert len(handled) == 1


# ── speaker identity ────────────────────────────────────────────────


def test_guild_message_is_labeled_with_sender_name():
    channel, handled = _make_channel("open")
    _feed(channel, _payload("raporu attım"))
    assert handled[0]["content"] == "[Hakan]: raporu attım"
    assert handled[0]["metadata"]["sender_name"] == "Hakan"


def test_guild_nick_wins_over_global_name():
    channel, handled = _make_channel("open")
    _feed(channel, _payload("selam", nick="Kaptan"))
    assert handled[0]["content"].startswith("[Kaptan]:")


def test_dm_content_is_not_labeled():
    channel, handled = _make_channel("open")
    _feed(channel, _payload("selam", guild_id=None))
    assert handled[0]["content"] == "selam"


def test_bot_mention_is_stripped_and_others_humanized():
    channel, handled = _make_channel("mention")
    _feed(
        channel,
        _payload(
            "<@BOT1> <@U2> ile konuş",
            mentions=[_bot_mention(), {"id": "U2", "username": "ayse", "global_name": "Ayşe"}],
        ),
    )
    assert handled[0]["content"] == "[Hakan]: @Ayşe ile konuş"
