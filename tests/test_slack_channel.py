"""Tests for the Slack channel's group-message handling.

Socket Mode events are fed straight into ``_on_socket_request`` with a stub
web client, so no network is touched. Covers the regressions reported from a
real workspace: an app subscribed only to ``message.channels`` (no
``app_mention``) dropped every @-mention, and the agent could not tell
channel members apart because content carried no speaker.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from flowly.channels.slack import SlackChannel
from flowly.config.schema import SlackConfig


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_channel(
    group_policy: str = "mention",
    users: dict[str, str] | None = None,
    group_context: str = "listen",
) -> tuple[SlackChannel, list[dict]]:
    """SlackChannel wired to a stub web client; returns (channel, handled)."""
    config = SlackConfig(
        enabled=True, bot_token="xoxb-t", app_token="xapp-t",
        group_policy=group_policy, group_context=group_context,
    )
    channel = SlackChannel(config, MagicMock())
    channel._bot_user_id = "UBOT"

    web = AsyncMock()

    async def users_info(user: str):
        name = (users or {}).get(user)
        if name is None:
            raise RuntimeError("missing_scope")
        return {"user": {"profile": {"display_name": name}}}

    web.users_info = AsyncMock(side_effect=users_info)
    web.conversations_info = AsyncMock(return_value={"channel": {"name": "genel"}})
    web.conversations_members = AsyncMock(return_value={"members": ["U1", "U2"]})
    channel._web_client = web

    handled: list[dict] = []

    async def record(**kwargs):
        handled.append(kwargs)

    channel._handle_message = record  # type: ignore[method-assign]
    return channel, handled


def _feed(channel: SlackChannel, *events: dict) -> None:
    req = [
        SimpleNamespace(type="events_api", envelope_id=f"e{i}", payload={"event": ev})
        for i, ev in enumerate(events)
    ]

    async def go():
        client = AsyncMock()
        for r in req:
            await channel._on_socket_request(client, r)

    _run(go())


def _msg(text: str, ts: str = "100.1", **extra) -> dict:
    return {
        "type": "message",
        "user": "U1",
        "channel": "C1",
        "ts": ts,
        "channel_type": "channel",
        "text": text,
        **extra,
    }


def _app_mention(text: str, ts: str = "100.1", **extra) -> dict:
    # Real app_mention payloads carry no channel_type.
    return {"type": "app_mention", "user": "U1", "channel": "C1", "ts": ts, "text": text, **extra}


# ── mention gating ──────────────────────────────────────────────────


def test_mention_policy_does_not_reply_to_plain_channel_message():
    # Not answered (no reply turn); with listen default it's observed instead —
    # covered by test_unanswered_mention_channel_message_is_observed below.
    channel, handled = _make_channel("mention", group_context="off")
    _feed(channel, _msg("günaydın ekip"))
    assert handled == []


def test_mention_answered_when_only_message_event_arrives():
    # App subscribed to message.channels but NOT app_mention: the mention
    # arrives once, as a plain message event. It must still be answered.
    channel, handled = _make_channel("mention")
    _feed(channel, _msg("<@UBOT> özet çıkarır mısın"))
    assert len(handled) == 1
    assert "özet çıkarır mısın" in handled[0]["content"]


def test_mention_answered_once_when_both_events_arrive():
    channel, handled = _make_channel("mention")
    _feed(channel, _msg("<@UBOT> selam"), _app_mention("<@UBOT> selam"))
    assert len(handled) == 1


def test_mention_answered_once_when_app_mention_arrives_first():
    channel, handled = _make_channel("mention")
    _feed(channel, _app_mention("<@UBOT> selam"), _msg("<@UBOT> selam"))
    assert len(handled) == 1


def test_open_policy_answers_plain_messages():
    channel, handled = _make_channel("open")
    _feed(channel, _msg("herkese merhaba"))
    assert len(handled) == 1


def test_app_mention_without_channel_type_is_treated_as_channel():
    channel, handled = _make_channel("mention")
    _feed(channel, _app_mention("<@UBOT> selam"))
    assert handled[0]["metadata"]["slack"]["channel_type"] == "channel"


# ── speaker identity ────────────────────────────────────────────────


def test_group_message_is_labeled_with_sender_name():
    channel, handled = _make_channel("open", users={"U1": "Hakan"})
    _feed(channel, _msg("raporu attım"))
    assert handled[0]["content"] == "[Hakan]: raporu attım"
    assert handled[0]["metadata"]["slack"]["sender_name"] == "Hakan"


def test_dm_content_is_not_labeled():
    channel, handled = _make_channel("open", users={"U1": "Hakan"})
    _feed(channel, _msg("selam", channel_type="im"))
    assert handled[0]["content"] == "selam"


def test_unresolvable_sender_degrades_to_raw_content():
    # users:read scope missing -> users_info raises -> no label, no crash.
    channel, handled = _make_channel("open")
    _feed(channel, _msg("selam"))
    assert handled[0]["content"] == "selam"


def test_user_name_lookup_is_cached():
    channel, handled = _make_channel("open", users={"U1": "Hakan"})
    _feed(channel, _msg("bir", ts="1.1"))
    after_first = channel._web_client.users_info.await_count
    _feed(channel, _msg("iki", ts="1.2"))
    # Second message from the same user resolves no new names — sender and
    # channel-member lookups are both cached.
    assert channel._web_client.users_info.await_count == after_first


def test_other_user_mentions_are_humanized():
    channel, handled = _make_channel("open", users={"U1": "Hakan", "U2": "Ayşe"})
    _feed(channel, _msg("<@U2> bunu görmeli"))
    assert handled[0]["content"] == "[Hakan]: @Ayşe bunu görmeli"


def test_channel_refs_are_humanized():
    channel, handled = _make_channel("open", users={"U1": "Hakan"})
    _feed(channel, _msg("detaylar <#C42|genel> kanalında"))
    assert handled[0]["content"] == "[Hakan]: detaylar #genel kanalında"


# ── passive channel context (Faz B / observe path) ──────────────────


def test_unanswered_mention_channel_message_is_observed():
    # mention policy, plain message → not answered but recorded for context.
    channel, handled = _make_channel("mention", users={"U1": "Hakan"})
    _feed(channel, _msg("günaydın ekip"))
    assert len(handled) == 1
    kw = handled[0]
    assert kw["metadata"]["group_observe"] is True
    # Bare text (no [name] prefix) — the loop labels it in the context block.
    assert kw["content"] == "günaydın ekip"
    assert kw["metadata"]["slack"]["sender_name"] == "Hakan"


def test_observed_message_gets_no_reaction():
    channel, handled = _make_channel("mention")
    _feed(channel, _msg("selam ekip"))
    channel._web_client.reactions_add.assert_not_awaited()


def test_group_context_off_drops_unanswered_messages():
    channel, handled = _make_channel("mention", group_context="off")
    _feed(channel, _msg("selam ekip"))
    assert handled == []


def test_open_policy_never_observes_it_answers():
    # In open mode every message is answered, so nothing is merely observed.
    channel, handled = _make_channel("open")
    _feed(channel, _msg("selam"))
    assert len(handled) == 1
    assert "group_observe" not in handled[0]["metadata"]


def test_answered_mention_attaches_channel_awareness():
    channel, handled = _make_channel("mention", users={"U1": "Hakan", "U2": "Ayşe"})
    _feed(channel, _msg("<@UBOT> özetle"))
    slack = handled[0]["metadata"]["slack"]
    assert slack["channel_name"] == "genel"
    assert slack["members"] == ["Hakan", "Ayşe"]


def test_dm_is_never_observed():
    channel, handled = _make_channel("mention", group_context="listen")
    _feed(channel, _msg("selam", channel_type="im"))
    # DMs always answer; observe flag never set.
    assert len(handled) == 1
    assert "group_observe" not in handled[0]["metadata"]
