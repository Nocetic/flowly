"""iMessage channel tests.

Isolation: FLOWLY_HOME is redirected to tmp (pairing store, media dir,
checkpoint file), the Messages DB is a fixture-built fake chat.db, and
the outbound transport (Electron bridge / osascript) is monkeypatched —
no test touches the real ``~/Library/Messages`` or sends Apple Events.

The ``attributedBody_*.bin`` fixtures are REAL typedstream blobs
produced by Foundation's ``NSArchiver`` over ``NSAttributedString`` —
the exact serializer Messages uses for the ``attributedBody`` column.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels._imessage_send import split_bubbles, strip_markdown
from flowly.channels._typedstream import decode_attributed_body
from flowly.channels.imessage import (
    APPLE_EPOCH_OFFSET,
    IMessageChannel,
    apple_time_to_datetime,
)
from flowly.config.loader import convert_keys
from flowly.config.schema import Config, IMessageConfig

FIXTURES = Path(__file__).parent / "fixtures" / "imessage"

# 2026-06-11 12:00:00 UTC in Apple-epoch nanoseconds.
_APPLE_NS = (1_781_179_200 - APPLE_EPOCH_OFFSET) * 1_000_000_000


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


# ── fake chat.db ────────────────────────────────────────────────────


class FakeChatDb:
    """Builds the minimal chat.db schema the channel queries."""

    def __init__(self, path: Path):
        self.path = path
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT, text TEXT, attributedBody BLOB,
                is_from_me INTEGER DEFAULT 0, date INTEGER DEFAULT 0,
                cache_has_attachments INTEGER DEFAULT 0,
                item_type INTEGER DEFAULT 0, handle_id INTEGER
            );
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY, guid TEXT,
                chat_identifier TEXT, display_name TEXT,
                style INTEGER DEFAULT 45, service_name TEXT DEFAULT 'iMessage'
            );
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE attachment (
                ROWID INTEGER PRIMARY KEY, filename TEXT,
                mime_type TEXT, transfer_name TEXT
            );
            CREATE TABLE message_attachment_join (
                message_id INTEGER, attachment_id INTEGER
            );
            """
        )
        conn.commit()
        conn.close()

    def _exec(self, sql: str, params: tuple) -> int:
        conn = sqlite3.connect(self.path)
        cur = conn.execute(sql, params)
        conn.commit()
        rowid = cur.lastrowid
        conn.close()
        return rowid

    def add_chat(
        self,
        identifier: str,
        *,
        guid: str | None = None,
        style: int = 45,
        display_name: str = "",
        service: str = "iMessage",
    ) -> int:
        return self._exec(
            "INSERT INTO chat (guid, chat_identifier, display_name, style, service_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (guid or f"iMessage;-;{identifier}", identifier, display_name, style, service),
        )

    def add_handle(self, handle: str) -> int:
        return self._exec("INSERT INTO handle (id) VALUES (?)", (handle,))

    def add_message(
        self,
        chat_rowid: int,
        *,
        text: str | None = None,
        attributed_body: bytes | None = None,
        handle_rowid: int | None = None,
        is_from_me: int = 0,
        item_type: int = 0,
        has_attachments: int = 0,
        date: int = _APPLE_NS,
    ) -> int:
        rowid = self._exec(
            "INSERT INTO message (guid, text, attributedBody, is_from_me, date, "
            "cache_has_attachments, item_type, handle_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"guid-{chat_rowid}-{text or 'blob'}-{is_from_me}",
                text,
                attributed_body,
                is_from_me,
                date,
                has_attachments,
                item_type,
                handle_rowid,
            ),
        )
        self._exec(
            "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
            (chat_rowid, rowid),
        )
        return rowid

    def add_attachment(self, message_rowid: int, filename: str, mime_type: str) -> int:
        att = self._exec(
            "INSERT INTO attachment (filename, mime_type, transfer_name) VALUES (?, ?, ?)",
            (filename, mime_type, Path(filename).name),
        )
        self._exec(
            "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)",
            (message_rowid, att),
        )
        return att


@pytest.fixture
def fake_db(tmp_path) -> FakeChatDb:
    return FakeChatDb(tmp_path / "chat.db")


def make_channel(
    fake_db: FakeChatDb,
    bus: MessageBus | None = None,
    **config_kwargs,
) -> IMessageChannel:
    config_kwargs.setdefault("enabled", True)
    config_kwargs.setdefault("db_path", str(fake_db.path))
    config_kwargs.setdefault("dm_policy", "open")
    config = IMessageConfig(**config_kwargs)
    return IMessageChannel(config, bus or MessageBus())


async def poll_and_process(channel: IMessageChannel) -> None:
    for row in channel._poll_once():
        await channel._process_row(row)


async def next_inbound(bus: MessageBus, timeout: float = 1.0):
    return await asyncio.wait_for(bus.consume_inbound(), timeout=timeout)


def inbound_empty(bus: MessageBus) -> bool:
    return bus.inbound.empty() if hasattr(bus, "inbound") else True


# ── typedstream decoding ────────────────────────────────────────────


FIXTURE_NAMES = ["simple", "turkish", "emoji", "long", "multiline", "url"]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_decode_real_typedstream_fixture(name):
    blob = (FIXTURES / f"attributedBody_{name}.bin").read_bytes()
    expected = (FIXTURES / f"attributedBody_{name}.txt").read_text(encoding="utf-8")
    assert decode_attributed_body(blob) == expected


def test_decode_empty_and_none():
    assert decode_attributed_body(None) is None
    assert decode_attributed_body(b"") is None


def test_decode_garbage_returns_none_or_text():
    # Pure binary garbage without any text run → None
    assert decode_attributed_body(bytes(range(0, 32)) * 4) is None


def test_decode_fallback_extracts_longest_run():
    # No typedstream header — falls back to the longest UTF-8 run.
    blob = b"\x00\x01junk\x02" + "merhaba dünya bu bir test".encode() + b"\x00\x05ab"
    assert decode_attributed_body(blob) == "merhaba dünya bu bir test"


# ── time conversion ─────────────────────────────────────────────────


def test_apple_time_nanoseconds():
    dt = apple_time_to_datetime(_APPLE_NS)
    assert dt.isoformat().startswith("2026-06-11T12:00:00")


def test_apple_time_legacy_seconds():
    dt = apple_time_to_datetime(_APPLE_NS // 1_000_000_000)
    assert dt.isoformat().startswith("2026-06-11T12:00:00")


# ── checkpoint / poller ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_run_skips_history(fake_db):
    """Messages existing before first start are never replayed."""
    chat = fake_db.add_chat("+15551234567")
    handle = fake_db.add_handle("+15551234567")
    fake_db.add_message(chat, text="old history", handle_rowid=handle)

    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)

    assert inbound_empty(bus)
    # Checkpoint was persisted at the tip.
    state = json.loads(
        (Path(channel._state_path())).read_text(encoding="utf-8")
    )
    assert state["lastRowId"] >= 1


@pytest.mark.asyncio
async def test_new_message_reaches_bus(fake_db):
    chat = fake_db.add_chat("+15551234567")
    handle = fake_db.add_handle("+15551234567")

    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)  # initialize checkpoint

    fake_db.add_message(chat, text="hello flowly", handle_rowid=handle)
    await poll_and_process(channel)

    msg = await next_inbound(bus)
    assert msg.channel == "imessage"
    assert msg.chat_id == "+15551234567"
    assert msg.sender_id == "+15551234567"
    assert msg.content == "hello flowly"
    assert msg.session_key == "imessage:+15551234567"
    assert msg.metadata["message_guid"]
    assert msg.metadata["is_group"] is False


@pytest.mark.asyncio
async def test_checkpoint_advances_and_no_redelivery(fake_db):
    chat = fake_db.add_chat("+15551234567")
    handle = fake_db.add_handle("+15551234567")

    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)

    fake_db.add_message(chat, text="one", handle_rowid=handle)
    await poll_and_process(channel)
    await next_inbound(bus)

    # Second poll with no new rows → nothing delivered again.
    await poll_and_process(channel)
    assert inbound_empty(bus)


@pytest.mark.asyncio
async def test_checkpoint_survives_restart(fake_db):
    chat = fake_db.add_chat("+15551234567")
    handle = fake_db.add_handle("+15551234567")

    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)
    fake_db.add_message(chat, text="before restart", handle_rowid=handle)
    await poll_and_process(channel)
    await next_inbound(bus)

    # New channel instance (restart) — loads checkpoint, no replay.
    bus2 = MessageBus()
    channel2 = make_channel(fake_db, bus2)
    channel2._last_rowid = channel2._load_checkpoint()
    await poll_and_process(channel2)
    assert inbound_empty(bus2)


@pytest.mark.asyncio
async def test_own_messages_skipped(fake_db):
    chat = fake_db.add_chat("+15551234567")
    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)

    fake_db.add_message(chat, text="my own reply", is_from_me=1)
    await poll_and_process(channel)
    assert inbound_empty(bus)


@pytest.mark.asyncio
async def test_system_rows_skipped(fake_db):
    chat = fake_db.add_chat("+15551234567")
    handle = fake_db.add_handle("+15551234567")
    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)

    # item_type != 0 → group rename / member join etc.
    fake_db.add_message(chat, text="renamed", handle_rowid=handle, item_type=2)
    await poll_and_process(channel)
    assert inbound_empty(bus)


@pytest.mark.asyncio
async def test_attributed_body_used_when_text_null(fake_db):
    blob = (FIXTURES / "attributedBody_turkish.bin").read_bytes()
    expected = (FIXTURES / "attributedBody_turkish.txt").read_text(encoding="utf-8")

    chat = fake_db.add_chat("+15551234567")
    handle = fake_db.add_handle("+15551234567")
    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)

    fake_db.add_message(chat, text=None, attributed_body=blob, handle_rowid=handle)
    await poll_and_process(channel)

    msg = await next_inbound(bus)
    assert msg.content == expected


# ── DM access policy ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlist_mode_blocks_silently(fake_db, monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send_text(target, text, **kwargs):
        sent.append((target, text))

    monkeypatch.setattr("flowly.channels.imessage.send_text", fake_send_text)

    chat = fake_db.add_chat("+19998887777")
    handle = fake_db.add_handle("+19998887777")
    bus = MessageBus()
    channel = make_channel(fake_db, bus, dm_policy="allowlist", allow_from=["+15550000000"])
    await poll_and_process(channel)

    fake_db.add_message(chat, text="let me in", handle_rowid=handle)
    await poll_and_process(channel)

    assert inbound_empty(bus)
    assert sent == []  # no pairing reply in allowlist mode


@pytest.mark.asyncio
async def test_allow_from_config_admits_sender(fake_db):
    chat = fake_db.add_chat("+15550000000")
    handle = fake_db.add_handle("+15550000000")
    bus = MessageBus()
    channel = make_channel(
        fake_db, bus, dm_policy="allowlist", allow_from=["+15550000000"]
    )
    await poll_and_process(channel)

    fake_db.add_message(chat, text="hi", handle_rowid=handle)
    await poll_and_process(channel)
    msg = await next_inbound(bus)
    assert msg.sender_id == "+15550000000"


@pytest.mark.asyncio
async def test_pairing_mode_sends_code_and_blocks(fake_db, monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send_text(target, text, **kwargs):
        sent.append((target, text))

    monkeypatch.setattr("flowly.channels.imessage.send_text", fake_send_text)

    chat = fake_db.add_chat("+19998887777")
    handle = fake_db.add_handle("+19998887777")
    bus = MessageBus()
    channel = make_channel(fake_db, bus, dm_policy="pairing")
    await poll_and_process(channel)

    fake_db.add_message(chat, text="hello?", handle_rowid=handle)
    await poll_and_process(channel)

    assert inbound_empty(bus)
    assert len(sent) == 1
    target, text = sent[0]
    assert target == "+19998887777"
    assert "flowly pairing approve imessage" in text

    from flowly.pairing import list_pairing_requests

    reqs = list_pairing_requests("imessage")
    assert len(reqs) == 1
    assert reqs[0].id == "+19998887777"
    assert reqs[0].code in text


@pytest.mark.asyncio
async def test_pairing_approval_admits_sender(fake_db, monkeypatch):
    async def fake_send_text(target, text, **kwargs):
        pass

    monkeypatch.setattr("flowly.channels.imessage.send_text", fake_send_text)

    chat = fake_db.add_chat("+19998887777")
    handle = fake_db.add_handle("+19998887777")
    bus = MessageBus()
    channel = make_channel(fake_db, bus, dm_policy="pairing")
    await poll_and_process(channel)

    fake_db.add_message(chat, text="knock knock", handle_rowid=handle)
    await poll_and_process(channel)
    assert inbound_empty(bus)

    from flowly.pairing import approve_pairing_code, list_pairing_requests

    code = list_pairing_requests("imessage")[0].code
    assert approve_pairing_code("imessage", code) is not None

    fake_db.add_message(chat, text="now allowed", handle_rowid=handle)
    await poll_and_process(channel)
    msg = await next_inbound(bus)
    assert msg.content == "now allowed"


# ── group chats ─────────────────────────────────────────────────────


def _group(fake_db: FakeChatDb) -> tuple[int, int]:
    chat = fake_db.add_chat(
        "chat831290", guid="iMessage;+;chat831290", style=43, display_name="Ev grubu"
    )
    handle = fake_db.add_handle("+15551112222")
    return chat, handle


@pytest.mark.asyncio
async def test_group_requires_mention_by_default(fake_db):
    chat, handle = _group(fake_db)
    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)

    fake_db.add_message(chat, text="random chatter", handle_rowid=handle)
    await poll_and_process(channel)
    assert inbound_empty(bus)


@pytest.mark.asyncio
async def test_group_mention_passes_and_is_stripped(fake_db):
    chat, handle = _group(fake_db)
    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)

    fake_db.add_message(chat, text="@flowly what's the weather", handle_rowid=handle)
    await poll_and_process(channel)

    msg = await next_inbound(bus)
    assert msg.chat_id == "chat831290"
    assert msg.metadata["is_group"] is True
    assert msg.metadata["chat_name"] == "Ev grubu"
    # Wake word stripped, sender attributed.
    assert "@flowly" not in msg.content
    assert "[from: +15551112222]" in msg.content
    assert "what's the weather" in msg.content


@pytest.mark.asyncio
async def test_group_open_policy_forwards_everything(fake_db):
    chat, handle = _group(fake_db)
    bus = MessageBus()
    channel = make_channel(fake_db, bus, group_policy="open")
    await poll_and_process(channel)

    fake_db.add_message(chat, text="no mention here", handle_rowid=handle)
    await poll_and_process(channel)
    msg = await next_inbound(bus)
    assert "no mention here" in msg.content


@pytest.mark.asyncio
async def test_group_allowlist_policy(fake_db):
    chat, handle = _group(fake_db)
    bus = MessageBus()
    channel = make_channel(
        fake_db, bus, group_policy="allowlist", group_allow_from=["other-chat"]
    )
    await poll_and_process(channel)

    fake_db.add_message(chat, text="@flowly hello", handle_rowid=handle)
    await poll_and_process(channel)
    assert inbound_empty(bus)

    channel2 = make_channel(
        fake_db,
        MessageBus(),
        group_policy="allowlist",
        group_allow_from=["chat831290"],
    )
    channel2._last_rowid = 0
    fake_db.add_message(chat, text="in the allowed group", handle_rowid=handle)
    rows = channel2._poll_once()
    delivered = [r for r in rows if channel2._group_message_allowed(r, r.text or "")]
    assert any(r.text == "in the allowed group" for r in delivered)


@pytest.mark.asyncio
async def test_group_unauthorized_sender_gets_no_pairing_spam(fake_db, monkeypatch):
    sent = []

    async def fake_send_text(target, text, **kwargs):
        sent.append((target, text))

    monkeypatch.setattr("flowly.channels.imessage.send_text", fake_send_text)

    chat, handle = _group(fake_db)
    bus = MessageBus()
    channel = make_channel(fake_db, bus, dm_policy="pairing", group_policy="open")
    await poll_and_process(channel)

    fake_db.add_message(chat, text="group msg from stranger", handle_rowid=handle)
    await poll_and_process(channel)

    assert inbound_empty(bus)
    assert sent == []  # never pairing-reply into a group


# ── attachments ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attachment_copied_to_media_dir(fake_db, tmp_path):
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"\xff\xd8\xffjpegdata")

    chat = fake_db.add_chat("+15551234567")
    handle = fake_db.add_handle("+15551234567")
    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)

    rowid = fake_db.add_message(
        chat, text="look at this", handle_rowid=handle, has_attachments=1
    )
    fake_db.add_attachment(rowid, str(source), "image/jpeg")
    await poll_and_process(channel)

    msg = await next_inbound(bus)
    assert len(msg.media) == 1
    copied = Path(msg.media[0])
    assert copied.exists()
    assert copied.suffix == ".jpg"
    assert "media" in copied.parts
    assert copied.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_missing_attachment_retries_then_delivers(fake_db, tmp_path):
    source = tmp_path / "later.jpg"  # not yet on disk (iCloud lag)

    chat = fake_db.add_chat("+15551234567")
    handle = fake_db.add_handle("+15551234567")
    bus = MessageBus()
    channel = make_channel(fake_db, bus)
    await poll_and_process(channel)

    rowid = fake_db.add_message(
        chat, text=None, handle_rowid=handle, has_attachments=1
    )
    fake_db.add_attachment(rowid, str(source), "image/jpeg")
    await poll_and_process(channel)
    assert inbound_empty(bus)  # parked for retry
    assert rowid in channel._pending_attachments

    source.write_bytes(b"arrived")
    await channel._retry_pending_attachments()

    msg = await next_inbound(bus)
    assert len(msg.media) == 1
    assert Path(msg.media[0]).read_bytes() == b"arrived"


@pytest.mark.asyncio
async def test_heic_conversion_falls_back_to_original(fake_db, tmp_path):
    """Bogus HEIC bytes → sips fails → original file is kept."""
    channel = make_channel(fake_db)
    bogus = tmp_path / "img.heic"
    bogus.write_bytes(b"not really heic")
    result = await channel._convert_heic(bogus)
    assert result.exists()


@pytest.mark.asyncio
async def test_audio_attachment_without_groq_key_skips_transcription(
    fake_db, tmp_path
):
    voice = tmp_path / "memo.caf"
    voice.write_bytes(b"caf-bytes")

    chat = fake_db.add_chat("+15551234567")
    handle = fake_db.add_handle("+15551234567")
    bus = MessageBus()
    channel = make_channel(fake_db, bus)  # no groq_api_key
    await poll_and_process(channel)

    rowid = fake_db.add_message(
        chat, text=None, handle_rowid=handle, has_attachments=1
    )
    fake_db.add_attachment(rowid, str(voice), "audio/x-caf")
    await poll_and_process(channel)

    msg = await next_inbound(bus)
    assert msg.content == "(attachment)"  # no transcript, placeholder body
    assert len(msg.media) == 1


# ── outbound ────────────────────────────────────────────────────────


def test_strip_markdown():
    text = (
        "# Title\n\n**bold** and *italic* and `code`\n\n"
        "- item one\n- item two\n\n[link](https://x.dev)\n\n```py\nprint(1)\n```"
    )
    out = strip_markdown(text)
    assert "**" not in out and "`" not in out and "# " not in out
    assert "bold and italic and code" in out
    assert "• item one" in out
    assert "link (https://x.dev)" in out
    assert "print(1)" in out


def test_split_bubbles_paragraphs():
    assert split_bubbles("one\n\ntwo") == ["one\n\ntwo"]
    bubbles = split_bubbles("a" * 3000 + "\n\n" + "b" * 3000)
    assert len(bubbles) == 2


def test_split_bubbles_hard_wrap_oversize_paragraph():
    bubbles = split_bubbles("x" * 9000)
    assert len(bubbles) == 3
    assert all(len(b) <= 4000 for b in bubbles)
    assert "".join(bubbles) == "x" * 9000


@pytest.mark.asyncio
async def test_send_strips_markdown_and_chunks(fake_db, monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send_text(target, text, **kwargs):
        sent.append((target, text))

    monkeypatch.setattr("flowly.channels.imessage.send_text", fake_send_text)

    channel = make_channel(fake_db)
    await channel.send(
        OutboundMessage(
            channel="imessage",
            chat_id="+15551234567",
            content="**Done!** Check `result.txt`",
        )
    )
    assert sent == [("+15551234567", "Done! Check result.txt")]


@pytest.mark.asyncio
async def test_send_extracts_media_tags(fake_db, monkeypatch, tmp_path):
    texts: list[str] = []
    files: list[str] = []

    async def fake_send_text(target, text, **kwargs):
        texts.append(text)

    async def fake_send_file(target, file_path, **kwargs):
        files.append(str(file_path))

    monkeypatch.setattr("flowly.channels.imessage.send_text", fake_send_text)
    monkeypatch.setattr("flowly.channels.imessage.send_file", fake_send_file)

    media = tmp_path / "chart.png"
    media.write_bytes(b"png")

    channel = make_channel(fake_db)
    await channel.send(
        OutboundMessage(
            channel="imessage",
            chat_id="+15551234567",
            content=f"Here you go MEDIA:{media}",
        )
    )
    assert texts == ["Here you go"]
    assert files == [str(media)]


@pytest.mark.asyncio
async def test_send_failed_attachment_falls_back_to_notice(
    fake_db, monkeypatch, tmp_path
):
    from flowly.channels._imessage_send import IMessageSendError

    texts: list[str] = []

    async def fake_send_text(target, text, **kwargs):
        texts.append(text)

    async def fake_send_file(target, file_path, **kwargs):
        raise IMessageSendError("nope")

    monkeypatch.setattr("flowly.channels.imessage.send_text", fake_send_text)
    monkeypatch.setattr("flowly.channels.imessage.send_file", fake_send_file)

    media = tmp_path / "doc.pdf"
    media.write_bytes(b"pdf")

    channel = make_channel(fake_db)
    await channel.send(
        OutboundMessage(
            channel="imessage", chat_id="+1555", content="sending file",
            media=[str(media)],
        )
    )
    assert texts[0] == "sending file"
    assert "doc.pdf" in texts[1]


@pytest.mark.asyncio
async def test_send_never_raises_into_dispatcher(fake_db, monkeypatch):
    from flowly.channels._imessage_send import IMessageSendError

    async def fake_send_text(target, text, **kwargs):
        raise IMessageSendError("Automation permission denied")

    monkeypatch.setattr("flowly.channels.imessage.send_text", fake_send_text)
    channel = make_channel(fake_db)
    # Must not raise.
    await channel.send(
        OutboundMessage(channel="imessage", chat_id="+1555", content="hi")
    )


# ── transport: electron → signed helper → osascript ─────────────────


@pytest.fixture
def no_helper(monkeypatch):
    """Pretend the signed helper isn't built so tests exercise osascript.

    The helper binary may actually exist in the worktree (built by
    helpers/build.sh); without this, transport tests would shell out to
    it for real.
    """
    from flowly.channels import _imessage_send as transport

    monkeypatch.setattr(transport, "_helper_available", lambda: False)


@pytest.mark.asyncio
async def test_transport_prefers_electron(monkeypatch, fake_db, no_helper):
    from flowly.channels import _imessage_send as transport

    calls: list[str] = []

    def fake_electron(payload):
        calls.append("electron")
        return None  # success

    def fake_osascript(script, args):
        calls.append("osascript")
        return None

    monkeypatch.setattr(transport, "_send_via_electron_sync", fake_electron)
    monkeypatch.setattr(transport, "_run_osascript_sync", fake_osascript)

    await transport.send_text("+15551234567", "hi", db_path=fake_db.path)
    assert calls == ["electron"]


@pytest.mark.asyncio
async def test_transport_prefers_helper_over_osascript(monkeypatch, fake_db):
    """When the signed helper is available it wins over bare osascript."""
    from flowly.channels import _imessage_send as transport

    calls: list[str] = []

    monkeypatch.setattr(transport, "_send_via_electron_sync", lambda p: "UNAVAILABLE")
    monkeypatch.setattr(transport, "_helper_available", lambda: True)
    monkeypatch.setattr(transport, "_working_chat_id", {})

    def fake_helper(text, chat_id):
        calls.append(("helper", text, chat_id))
        return None

    def fake_osascript(script, args):
        calls.append(("osascript", args))
        return None

    monkeypatch.setattr(transport, "_run_helper_sync", fake_helper)
    monkeypatch.setattr(transport, "_run_osascript_sync", fake_osascript)

    await transport.send_text("+15551234567", "hi", db_path=fake_db.path)
    # DM buddy form (bare handle) tried first — it's the one that delivers.
    assert calls == [("helper", "hi", "+15551234567")]


@pytest.mark.asyncio
async def test_transport_helper_unavailable_falls_to_osascript(monkeypatch, fake_db):
    """Missing helper binary → per-candidate osascript fallback."""
    from flowly.channels import _imessage_send as transport

    calls: list[str] = []

    monkeypatch.setattr(transport, "_send_via_electron_sync", lambda p: "UNAVAILABLE")
    monkeypatch.setattr(transport, "_helper_available", lambda: True)
    monkeypatch.setattr(transport, "_working_chat_id", {})
    monkeypatch.setattr(transport, "_run_helper_sync", lambda t, c: "UNAVAILABLE")

    def fake_osascript(script, args):
        calls.append(args[1])
        return None

    monkeypatch.setattr(transport, "_run_osascript_sync", fake_osascript)
    await transport.send_text("+15551234567", "hi", db_path=fake_db.path)
    assert calls == ["+15551234567"]


@pytest.mark.asyncio
async def test_transport_buddy_form_first_for_dm(monkeypatch, fake_db, no_helper):
    from flowly.channels import _imessage_send as transport

    fake_db.add_chat("+15551234567", guid="iMessage;-;+15551234567")
    captured: dict = {}

    def fake_osascript(script, args):
        captured["script"] = script
        captured["args"] = args
        return None

    monkeypatch.setattr(transport, "_send_via_electron_sync", lambda p: "UNAVAILABLE")
    monkeypatch.setattr(transport, "_run_osascript_sync", fake_osascript)
    monkeypatch.setattr(transport, "_working_chat_id", {})

    await transport.send_text("+15551234567", "hello there", db_path=fake_db.path)
    # Bare handle + buddy-of-account script tried first — text is argv item 1.
    assert captured["args"] == ["hello there", "+15551234567"]
    assert "to buddy theHandle of theAccount" in captured["script"]


@pytest.mark.asyncio
async def test_transport_falls_through_to_chat_id_forms(
    monkeypatch, fake_db, no_helper
):
    """Buddy form rejected → chat-id forms tried in order."""
    from flowly.channels import _imessage_send as transport

    fake_db.add_chat("+15551234567", guid="iMessage;-;+15551234567")
    attempts: list[str] = []

    def fake_osascript(script, args):
        attempts.append(args[1])
        # Reject the buddy form and any;-;; accept the iMessage;-; GUID.
        if ";" not in args[1] or args[1].startswith("any;"):
            return "execution error: Messages got an error: ... (-10004)"
        return None

    monkeypatch.setattr(transport, "_send_via_electron_sync", lambda p: "UNAVAILABLE")
    monkeypatch.setattr(transport, "_run_osascript_sync", fake_osascript)
    monkeypatch.setattr(transport, "_working_chat_id", {})

    await transport.send_text("+15551234567", "hi", db_path=fake_db.path)
    assert attempts == ["+15551234567", "any;-;+15551234567", "iMessage;-;+15551234567"]

    # The working form is cached — the next send goes straight to it.
    attempts.clear()
    await transport.send_text("+15551234567", "again", db_path=fake_db.path)
    assert attempts == ["iMessage;-;+15551234567"]


def test_send_candidates_dm_handle_buddy_first():
    from flowly.channels._imessage_send import send_candidates

    # Bare DM handle: buddy form (no ';') first, then chat-id fallbacks.
    cands = send_candidates("+15551234567", None)
    assert cands[0] == "+15551234567"
    assert "any;-;+15551234567" in cands
    assert "iMessage;-;+15551234567" in cands


def test_send_candidates_group_guid_chat_id_only():
    from flowly.channels._imessage_send import send_candidates

    cands = send_candidates("chat831290", "iMessage;+;chat831290")
    # Groups have no buddy form — chat-id only.
    assert all(";" in c for c in cands)
    assert cands[0] == "any;+;chat831290"
    assert "iMessage;+;chat831290" in cands


@pytest.mark.asyncio
async def test_transport_definitive_electron_error_raises(monkeypatch, fake_db):
    from flowly.channels import _imessage_send as transport

    def fake_electron(payload):
        return "Electron iMessage send failed: recipient rejected"

    monkeypatch.setattr(transport, "_send_via_electron_sync", fake_electron)

    with pytest.raises(transport.IMessageSendError):
        await transport.send_text("+1555", "hi", db_path=fake_db.path)


def test_resolve_chat_guid_prefers_imessage_service(fake_db):
    from flowly.channels._imessage_send import resolve_chat_guid

    fake_db.add_chat("+15551234567", guid="SMS;-;+15551234567", service="SMS")
    fake_db.add_chat("+15551234567", guid="iMessage;-;+15551234567")
    assert (
        resolve_chat_guid(fake_db.path, "+15551234567")
        == "iMessage;-;+15551234567"
    )


# ── BlueBubbles outbound backend ────────────────────────────────────


def test_bb_chat_guid_for():
    from flowly.channels._imessage_bluebubbles import chat_guid_for

    assert chat_guid_for("+15551234567") == "iMessage;-;+15551234567"
    assert chat_guid_for("user@icloud.com") == "iMessage;-;user@icloud.com"
    # Raw GUID passes through unchanged.
    assert chat_guid_for("iMessage;+;chat99") == "iMessage;+;chat99"


@pytest.mark.asyncio
async def test_send_routes_through_bluebubbles_when_configured(
    fake_db, monkeypatch
):
    calls: list[tuple] = []

    async def fake_bb_send_text(url, password, target, text):
        calls.append((url, password, target, text))

    async def fail_direct(*a, **k):
        raise AssertionError("direct osascript path must not be used when BB set")

    monkeypatch.setattr(
        "flowly.channels.imessage.bb_send_text", fake_bb_send_text
    )
    monkeypatch.setattr("flowly.channels.imessage.send_text", fail_direct)

    channel = make_channel(
        fake_db,
        bluebubbles_url="http://127.0.0.1:1234",
        bluebubbles_password="secret",
    )
    await channel.send(
        OutboundMessage(channel="imessage", chat_id="+15551234567", content="**hi**")
    )
    assert calls == [("http://127.0.0.1:1234", "secret", "+15551234567", "hi")]


@pytest.mark.asyncio
async def test_send_uses_direct_path_without_bluebubbles(fake_db, monkeypatch):
    calls: list = []

    async def fake_direct(target, text, **kwargs):
        calls.append(text)

    def boom(*a, **k):
        raise AssertionError("BB path must not run when unconfigured")

    monkeypatch.setattr("flowly.channels.imessage.send_text", fake_direct)
    monkeypatch.setattr("flowly.channels.imessage.bb_send_text", boom)

    channel = make_channel(fake_db)  # no bluebubbles_url
    await channel.send(
        OutboundMessage(channel="imessage", chat_id="+15551234567", content="hi")
    )
    assert calls == ["hi"]


@pytest.mark.asyncio
async def test_bb_send_text_posts_expected_payload(monkeypatch):
    import flowly.channels._imessage_bluebubbles as bb

    captured: dict = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"status": 200, "message": "sent"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **k):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr(bb.httpx, "AsyncClient", FakeClient)
    await bb.send_text("http://127.0.0.1:1234", "pw", "+15551234567", "hello")

    assert "/api/v1/message/text" in captured["url"]
    assert "password=pw" in captured["url"]
    assert captured["json"]["chatGuid"] == "iMessage;-;+15551234567"
    assert captured["json"]["message"] == "hello"
    assert captured["json"]["method"] == "apple-script"
    assert captured["json"]["tempGuid"]


@pytest.mark.asyncio
async def test_bb_send_text_raises_on_bad_password(monkeypatch):
    import flowly.channels._imessage_bluebubbles as bb

    class FakeResp:
        status_code = 401

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(bb.httpx, "AsyncClient", FakeClient)
    with pytest.raises(bb.BlueBubblesError):
        await bb.send_text("http://127.0.0.1:1234", "wrong", "+1555", "hi")


def _bb_event(text="hello", sender="+15551234567", *, guid="m1",
              is_from_me=False, item_type=0, style=45, chat_id=None,
              attachments=None, display_name=""):
    return {
        "type": "new-message",
        "data": {
            "guid": guid,
            "text": text,
            "isFromMe": is_from_me,
            "itemType": item_type,
            "handle": {"address": sender},
            "attachments": attachments or [],
            "chats": [{
                "chatIdentifier": chat_id or sender,
                "displayName": display_name,
                "style": style,
                "service": "iMessage",
            }],
        },
    }


def make_bb_channel(bus=None, **cfg):
    cfg.setdefault("enabled", True)
    cfg.setdefault("dm_policy", "open")
    cfg.setdefault("bluebubbles_url", "http://127.0.0.1:1234")
    cfg.setdefault("bluebubbles_password", "pw")
    config = IMessageConfig(**cfg)
    return IMessageChannel(config, bus or MessageBus())


@pytest.mark.asyncio
async def test_bb_webhook_inbound_reaches_bus():
    bus = MessageBus()
    channel = make_bb_channel(bus)
    await channel._on_bb_event(_bb_event(text="merhaba flowly"))
    msg = await next_inbound(bus)
    assert msg.channel == "imessage"
    assert msg.chat_id == "+15551234567"
    assert msg.sender_id == "+15551234567"
    assert msg.content == "merhaba flowly"
    assert msg.metadata["message_guid"] == "m1"
    assert msg.metadata["is_group"] is False


def test_parse_webhook_body_json_and_form():
    from flowly.channels.imessage import _parse_webhook_body

    # Plain JSON.
    assert _parse_webhook_body(b'{"type":"new-message","data":{"guid":"a"}}')["type"] == "new-message"
    # Form-encoded with JSON under "payload".
    import urllib.parse
    form = urllib.parse.urlencode({"payload": '{"type":"new-message","data":{"guid":"b"}}'})
    assert _parse_webhook_body(form.encode())["data"]["guid"] == "b"
    # Garbage → empty dict, never raises.
    assert _parse_webhook_body(b"not json at all") == {}


@pytest.mark.asyncio
async def test_bb_webhook_data_as_list():
    """Some BlueBubbles versions wrap the message in a list under data."""
    bus = MessageBus()
    channel = make_bb_channel(bus)
    payload = _bb_event(text="hi list")
    payload["data"] = [payload["data"]]  # wrap in list
    await channel._on_bb_event(payload)
    msg = await next_inbound(bus)
    assert msg.content == "hi list"


@pytest.mark.asyncio
async def test_bb_webhook_chat_guid_only_no_identifier():
    """v1.9+ payloads can carry only chats[0].guid, no chatIdentifier."""
    bus = MessageBus()
    channel = make_bb_channel(bus)
    await channel._on_bb_event({
        "type": "new-message",
        "data": {
            "guid": "x1",
            "text": "guid only",
            "isFromMe": False,
            "handle": {"address": "+15551234567"},
            "chats": [{"guid": "iMessage;-;+15551234567", "style": 45}],
        },
    })
    msg = await next_inbound(bus)
    assert msg.sender_id == "+15551234567"
    assert msg.chat_id == "iMessage;-;+15551234567"


@pytest.mark.asyncio
async def test_bb_webhook_accepts_message_event_type():
    bus = MessageBus()
    channel = make_bb_channel(bus)
    ev = _bb_event(text="typed message", guid="t1")
    ev["type"] = "message"  # not "new-message"
    await channel._on_bb_event(ev)
    msg = await next_inbound(bus)
    assert msg.content == "typed message"


@pytest.mark.asyncio
async def test_bb_webhook_dedupes_by_guid():
    bus = MessageBus()
    channel = make_bb_channel(bus)
    await channel._on_bb_event(_bb_event(guid="dup"))
    await channel._on_bb_event(_bb_event(guid="dup"))
    await next_inbound(bus)
    assert inbound_empty(bus)


@pytest.mark.asyncio
async def test_bb_webhook_skips_from_me_and_system():
    bus = MessageBus()
    channel = make_bb_channel(bus)
    await channel._on_bb_event(_bb_event(guid="a", is_from_me=True))
    await channel._on_bb_event(_bb_event(guid="b", item_type=2))
    assert inbound_empty(bus)


@pytest.mark.asyncio
async def test_bb_webhook_group_mention_gating_and_strip():
    bus = MessageBus()
    channel = make_bb_channel(bus)  # group_policy defaults to mention
    # No mention → ignored.
    await channel._on_bb_event(
        _bb_event(guid="g1", text="random", style=43, chat_id="chat99")
    )
    assert inbound_empty(bus)
    # Mention → forwarded, wake word stripped, sender attributed.
    await channel._on_bb_event(
        _bb_event(guid="g2", text="@flowly hi", style=43, chat_id="chat99",
                  display_name="Grup")
    )
    msg = await next_inbound(bus)
    assert msg.chat_id == "chat99"
    assert msg.metadata["is_group"] is True
    assert "@flowly" not in msg.content
    assert "[from: +15551234567]" in msg.content


@pytest.mark.asyncio
async def test_bb_webhook_allowlist_blocks(monkeypatch):
    async def no_send(*a, **k):
        pass

    monkeypatch.setattr("flowly.channels.imessage.bb_send_text", no_send)
    bus = MessageBus()
    channel = make_bb_channel(bus, dm_policy="allowlist", allow_from=["+1999"])
    await channel._on_bb_event(_bb_event(sender="+15550000000"))
    assert inbound_empty(bus)


@pytest.mark.asyncio
async def test_bb_webhook_downloads_attachment(monkeypatch, tmp_path):
    async def fake_download(url, password, att_guid, dest):
        Path(dest).write_bytes(b"img-bytes")
        return True

    monkeypatch.setattr(
        "flowly.channels.imessage.bb_download_attachment", fake_download
    )
    bus = MessageBus()
    channel = make_bb_channel(bus)
    await channel._on_bb_event(_bb_event(
        text="look", attachments=[
            {"guid": "att1", "mimeType": "image/jpeg", "transferName": "p.jpg"}
        ],
    ))
    msg = await next_inbound(bus)
    assert len(msg.media) == 1
    assert Path(msg.media[0]).read_bytes() == b"img-bytes"


@pytest.mark.asyncio
async def test_bb_register_webhook_skips_duplicate(monkeypatch):
    import flowly.channels._imessage_bluebubbles as bb

    class Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._p = payload

        def json(self):
            return self._p

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            return Resp(200, {"data": [{"url": "http://127.0.0.1:8642/bluebubbles-webhook"}]})

        async def post(self, *a, **k):
            raise AssertionError("must not POST when webhook already exists")

    monkeypatch.setattr(bb.httpx, "AsyncClient", FakeClient)
    ok = await bb.register_webhook(
        "http://127.0.0.1:1234", "pw", "http://127.0.0.1:8642/bluebubbles-webhook"
    )
    assert ok is True


def test_bb_webhook_config_camel_case():
    raw = {"channels": {"imessage": {
        "enabled": True,
        "bluebubblesUrl": "http://127.0.0.1:1234",
        "bluebubblesWebhookHost": "0.0.0.0",
        "bluebubblesWebhookPort": 9000,
    }}}
    config = Config.model_validate(convert_keys(raw))
    assert config.channels.imessage.bluebubbles_webhook_host == "0.0.0.0"
    assert config.channels.imessage.bluebubbles_webhook_port == 9000


def test_bb_config_camel_case_round_trip():
    raw = {
        "channels": {
            "imessage": {
                "enabled": True,
                "bluebubblesUrl": "http://127.0.0.1:1234",
                "bluebubblesPassword": "secret",
            }
        }
    }
    config = Config.model_validate(convert_keys(raw))
    assert config.channels.imessage.bluebubbles_url == "http://127.0.0.1:1234"
    assert config.channels.imessage.bluebubbles_password == "secret"


# ── config & registration ───────────────────────────────────────────


def test_config_camel_case_round_trip():
    raw = {
        "channels": {
            "imessage": {
                "enabled": True,
                "pollIntervalSeconds": 1.5,
                "dmPolicy": "open",
                "groupPolicy": "allowlist",
                "allowFrom": ["+15551234567"],
                "groupAllowFrom": ["chat123"],
                "mentionPatterns": ["@bot"],
            }
        }
    }
    config = Config.model_validate(convert_keys(raw))
    im = config.channels.imessage
    assert im.enabled is True
    assert im.poll_interval_seconds == 1.5
    assert im.dm_policy == "open"
    assert im.group_policy == "allowlist"
    assert im.allow_from == ["+15551234567"]
    assert im.group_allow_from == ["chat123"]
    assert im.mention_patterns == ["@bot"]


def test_config_defaults_off():
    config = Config()
    assert config.channels.imessage.enabled is False
    assert config.channels.imessage.dm_policy == "pairing"
    assert config.channels.imessage.group_policy == "mention"


def test_manager_registers_imessage_on_darwin(monkeypatch):
    import sys

    from flowly.channels.manager import ChannelManager

    if sys.platform != "darwin":
        pytest.skip("registration path is darwin-only")

    config = Config()
    config.channels.imessage.enabled = True
    manager = ChannelManager(config, MessageBus())
    assert "imessage" in manager.channels
    assert manager.channels["imessage"].name == "imessage"


def test_manager_skips_imessage_when_disabled():
    from flowly.channels.manager import ChannelManager

    manager = ChannelManager(Config(), MessageBus())
    assert "imessage" not in manager.channels


def test_pairing_rpc_accepts_imessage():
    from flowly.channels.feature_rpc import _PAIRING_CHANNELS

    assert "imessage" in _PAIRING_CHANNELS


def test_integration_card_registered():
    from flowly.integrations.registry import get_card

    card = get_card("imessage")
    assert card is not None
    assert card.category == "channel"
    assert card.config_path == "channels.imessage"
    assert card.probe is not None


def test_platform_hint_registered():
    from flowly.agent.prompt_blocks import build_platform_hint

    hint = build_platform_hint("imessage")
    assert "iMessage" in hint
    assert "MEDIA:" in hint


# ── session-key round trip ──────────────────────────────────────────


def test_session_filename_round_trip(tmp_path):
    """chat_identifier survives the sessions ``:`` ↔ ``_`` filename mapping."""
    from flowly.utils.helpers import safe_filename

    for chat_id in ("+15551234567", "user@icloud.com", "chat831290"):
        key = f"imessage:{chat_id}"
        fname = safe_filename(key.replace(":", "_"))
        assert fname.replace("_", ":") == key


# ── FDA / error surface ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_reports_full_disk_access(monkeypatch, tmp_path):
    """An authorization-denied DB read surfaces an actionable status."""
    config = IMessageConfig(enabled=True, db_path=str(tmp_path / "denied.db"))
    channel = IMessageChannel(config, MessageBus())

    def raise_auth_denied():
        raise sqlite3.OperationalError(
            "unable to open database file: authorization denied"
        )

    monkeypatch.setattr(channel, "_poll_once", raise_auth_denied)

    async def fast_sleep(seconds):
        channel._running = False  # stop after first tick

    monkeypatch.setattr(channel, "_sleep", fast_sleep)
    monkeypatch.setattr("sys.platform", "darwin")

    await channel.start()
    assert "Full Disk Access" in channel.status_detail
