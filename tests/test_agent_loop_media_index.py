"""The agent loop's hook into the media library.

One question decides whether "Open in chat" works: which timestamp gets
recorded. It has to be the one ``chat.history`` will publish for the message
carrying the media, which is why the loop reads it back off the persisted
session instead of stamping the clock.
"""

from __future__ import annotations

import pytest

from flowly.agent.loop import AgentLoop
from flowly.media.assets import MediaAsset
from flowly.media.library import SOURCE_GENERATED, SOURCE_RECEIVED


class _Session:
    """Just enough of a Session for the indexing hook."""

    def __init__(self, key: str, messages: list[dict]):
        self.key = key
        self.messages = messages


class _Indexer:
    """The loop's indexing surface, lifted out of the (expensive) loop itself.

    ``staticmethod`` is restored explicitly: reading ``AgentLoop._turn_message_ts``
    off the class yields a plain function, and re-attaching that to a new class
    would turn it back into a bound method that eats an extra argument.
    """

    _turn_message_ts = staticmethod(AgentLoop._turn_message_ts)
    _index_turn_media = AgentLoop._index_turn_media


@pytest.fixture()
def calls(monkeypatch) -> list[dict]:
    recorded: list[dict] = []

    async def fake_record(assets, **kwargs):
        recorded.append({"assets": list(assets), **kwargs})
        return len(assets)

    async def fake_record_paths(paths, **kwargs):
        recorded.append({"paths": list(paths), **kwargs})
        return len(paths)

    monkeypatch.setattr("flowly.media.library.record_async", fake_record)
    monkeypatch.setattr("flowly.media.library.record_paths_async", fake_record_paths)
    return recorded


TURN = [
    {"role": "user", "content": "draw a red car", "timestamp": "T1", "media": ["/m/up.png"]},
    {"role": "assistant", "content": "", "timestamp": "T2", "tool_calls": [{"id": "1"}]},
    {"role": "tool", "content": "ok", "timestamp": "T3"},
    {
        "role": "assistant",
        "content": "here you go",
        "timestamp": "T4",
        "media_assets": [{"path": "/m/img-a.png"}],
    },
]


@pytest.mark.asyncio
async def test_produced_media_is_recorded_against_the_assistant_turn(calls):
    session = _Session("cli:main", TURN)
    asset = MediaAsset(path="/m/img-a.png", kind="image")

    await _Indexer()._index_turn_media(session, [asset], None, "cli")

    assert len(calls) == 1
    assert calls[0]["source"] == SOURCE_GENERATED
    assert calls[0]["session_key"] == "cli:main"
    assert calls[0]["channel"] == "cli"
    # The closing assistant message, not the mid-turn tool-call one.
    assert calls[0]["message_ts"] == "T4"


@pytest.mark.asyncio
async def test_received_media_is_recorded_against_the_user_turn(calls):
    session = _Session("cli:main", TURN)

    await _Indexer()._index_turn_media(session, [], ["/m/up.png"], "cli")

    assert len(calls) == 1
    assert calls[0]["source"] == SOURCE_RECEIVED
    assert calls[0]["message_ts"] == "T1"


@pytest.mark.asyncio
async def test_both_directions_in_one_turn(calls):
    session = _Session("cli:main", TURN)
    asset = MediaAsset(path="/m/img-a.png", kind="image")

    await _Indexer()._index_turn_media(session, [asset], ["/m/up.png"], "cli")

    assert [c["source"] for c in calls] == [SOURCE_GENERATED, SOURCE_RECEIVED]


@pytest.mark.asyncio
async def test_a_turn_with_no_media_does_nothing(calls):
    session = _Session("cli:main", TURN)

    await _Indexer()._index_turn_media(session, [], None, "cli")

    assert calls == []


@pytest.mark.asyncio
async def test_a_missing_timestamp_is_not_fatal(calls):
    """History written before timestamps, or a hand-edited transcript."""
    session = _Session("cli:main", [{"role": "assistant", "media_assets": [{}]}])

    await _Indexer()._index_turn_media(
        session, [MediaAsset(path="/m/a.png", kind="image")], None, "cli"
    )

    assert calls[0]["message_ts"] == ""


def test_the_newest_matching_message_wins():
    """A long session holds many media turns; this one is about the last."""
    session = _Session(
        "cli:main",
        [
            {"role": "assistant", "timestamp": "OLD", "media_assets": [{}]},
            {"role": "assistant", "timestamp": "NEW", "media_assets": [{}]},
        ],
    )

    assert _Indexer._turn_message_ts(session, "assistant", "media_assets") == "NEW"


def test_role_is_part_of_the_match():
    """A user turn carrying `media` must not be mistaken for the reply."""
    session = _Session(
        "cli:main",
        [
            {"role": "user", "timestamp": "T1", "media": ["/m/up.png"]},
            {"role": "assistant", "timestamp": "T2", "media": ["/m/img.png"]},
        ],
    )

    assert _Indexer._turn_message_ts(session, "user", "media") == "T1"
