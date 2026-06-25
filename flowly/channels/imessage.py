"""iMessage channel — two delivery modes.

**BlueBubbles mode** (when ``bluebubbles_url`` is set) — recommended.
A BlueBubbles server (separate signed macOS app) holds the Automation +
Full Disk Access grants. Inbound arrives as new-message webhooks it
POSTs to a local listener; outbound goes through its REST API. Flowly
needs NO macOS permissions of its own and avoids the TCC ``-10004`` wall
a background process hits when scripting Messages.app directly.

**Direct mode** (default) — no extra app, but needs Full Disk Access.
Inbound tails ``~/Library/Messages/chat.db`` (SQLite WAL) by
``message.ROWID`` checkpoint, decoding ``attributedBody`` typedstream
blobs. Outbound drives Messages.app via the signed helper / osascript.
Reliable for interactive/terminal use; a launchd/gateway process can hit
TCC limits on send (hence BlueBubbles mode).

Identity model (both modes):
- ``chat_id``   = ``chat_identifier`` (DM: the handle, e.g.
  ``+15551234567`` / ``user@icloud.com``; group: ``chatNNN…``). Stable,
  allowlist-friendly, survives the session-file ``:`` ↔ ``_`` round-trip.
- ``sender_id`` = the sender handle (phone/email) — what users put in
  ``allow_from``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels._imessage_bluebubbles import (
    BlueBubblesError,
)
from flowly.channels._imessage_bluebubbles import (
    download_attachment as bb_download_attachment,
)
from flowly.channels._imessage_bluebubbles import (
    register_webhook as bb_register_webhook,
)
from flowly.channels._imessage_bluebubbles import (
    send_file as bb_send_file,
)
from flowly.channels._imessage_bluebubbles import (
    send_text as bb_send_text,
)
from flowly.channels._imessage_send import (
    IMessageSendError,
    send_file,
    send_text,
    split_bubbles,
    strip_markdown,
)
from flowly.channels._typedstream import decode_attributed_body
from flowly.channels.base import BaseChannel
from flowly.config.schema import IMessageConfig
from flowly.pairing import read_allow_from_store, upsert_pairing_request
from flowly.profile import get_flowly_home

# Apple epoch (2001-01-01) → Unix epoch offset, seconds.
APPLE_EPOCH_OFFSET = 978_307_200
# ``message.date`` switched to nanoseconds in High Sierra; older DBs
# store seconds. Anything above this is clearly nanoseconds.
_NANOSECOND_THRESHOLD = 10**12

# ``chat.style`` values in chat.db.
_CHAT_STYLE_GROUP = 43

# BlueBubbles webhook event types that carry a message.
_BB_MESSAGE_EVENTS = {"new-message", "message", "updated-message"}


def _parse_webhook_body(raw: bytes) -> dict:
    """Parse a BlueBubbles webhook body — JSON or form-encoded.

    BlueBubbles POSTs JSON in most versions but some send a form-encoded
    body with the JSON under a ``payload`` / ``data`` field; tolerate both
    so inbound never silently drops on Content-Type quirks.
    """
    body = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except (ValueError, TypeError):
        pass
    from urllib.parse import parse_qs

    form = parse_qs(body)
    for key in ("payload", "data", "message"):
        if form.get(key):
            try:
                inner = json.loads(form[key][0])
                return inner if isinstance(inner, dict) else {"data": inner}
            except (ValueError, TypeError):
                continue
    return {}

_DEFAULT_DB_PATH = "~/Library/Messages/chat.db"
_DEFAULT_MENTION_PATTERNS = (r"@?flowly\b",)

# How long to wait before re-probing the DB after an authorization
# failure — there is no point hammering a TCC denial every 2 seconds.
_FDA_RETRY_SECONDS = 30.0

_TAIL_QUERY = """
SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.is_from_me,
       m.date, m.cache_has_attachments, COALESCE(m.item_type, 0),
       COALESCE(h.id, ''), c.chat_identifier,
       COALESCE(c.display_name, ''), c.style, COALESCE(c.service_name, '')
FROM message m
JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
JOIN chat c                ON c.ROWID = cmj.chat_id
LEFT JOIN handle h         ON h.ROWID = m.handle_id
WHERE m.ROWID > ?
ORDER BY m.ROWID ASC
LIMIT 200
"""

_ATTACHMENT_QUERY = """
SELECT COALESCE(a.filename, ''), COALESCE(a.mime_type, ''),
       COALESCE(a.transfer_name, '')
FROM message_attachment_join maj
JOIN attachment a ON a.ROWID = maj.attachment_id
WHERE maj.message_id = ?
"""


def apple_time_to_datetime(raw: int | float | None) -> datetime:
    """Convert chat.db's Apple-epoch timestamp (ns or s) to aware UTC."""
    if not raw:
        return datetime.now(timezone.utc)
    seconds = raw / 1_000_000_000 if raw > _NANOSECOND_THRESHOLD else raw
    return datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET, tz=timezone.utc)


@dataclass
class _Row:
    """One tail-query result row, named for readability."""

    rowid: int
    guid: str
    text: str | None
    attributed_body: bytes | None
    is_from_me: int
    date: int
    has_attachments: int
    item_type: int
    sender_handle: str
    chat_identifier: str
    chat_name: str
    chat_style: int
    service: str

    @property
    def is_group(self) -> bool:
        return self.chat_style == _CHAT_STYLE_GROUP


@dataclass
class _PendingAttachment:
    """An attachment whose file hasn't landed on disk yet (iCloud lag)."""

    row: _Row
    retries_left: int = 1


class IMessageChannel(BaseChannel):
    """iMessage channel for macOS — chat.db tail in, AppleScript out."""

    name = "imessage"

    def __init__(
        self,
        config: IMessageConfig,
        bus: MessageBus,
        groq_api_key: str | None = None,
    ):
        super().__init__(config, bus)
        self.config: IMessageConfig = config
        self._groq_api_key = groq_api_key
        self._last_rowid: int | None = None
        self._status: str = "starting"
        self._pending_attachments: dict[int, _PendingAttachment] = {}
        self._mention_regexes = self._compile_mention_patterns()
        # BlueBubbles webhook mode state.
        self._bb_runner: Any = None  # aiohttp AppRunner
        self._bb_seen: OrderedDict[str, None] = OrderedDict()  # dedupe by msg guid

    # ── paths & state ──────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        configured = (self.config.db_path or "").strip()
        return Path(configured or _DEFAULT_DB_PATH).expanduser()

    def _state_path(self) -> Path:
        return get_flowly_home() / "imessage-state.json"

    def _load_checkpoint(self) -> int | None:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            value = data.get("lastRowId")
            return int(value) if value is not None else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _save_checkpoint(self, rowid: int) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"lastRowId": rowid}) + "\n", encoding="utf-8"
            )
        except OSError as e:
            logger.warning(f"iMessage checkpoint write failed: {e}")

    # ── status (surfaced via channel manager / probes) ─────────────

    @property
    def status_detail(self) -> str:
        return self._status

    # ── lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        # BlueBubbles mode needs no macOS permissions, so it can run even
        # off-darwin in principle — but the BlueBubbles server itself is
        # macOS. The direct chat.db path is darwin-only.
        if self._bluebubbles is not None:
            await self._start_bluebubbles()
            return

        if sys.platform != "darwin":
            self._status = "iMessage requires macOS"
            logger.error("iMessage channel requires macOS — not starting")
            return

        self._last_rowid = self._load_checkpoint()
        logger.info(f"iMessage channel watching {self.db_path}")

        while self._running:
            try:
                rows = await asyncio.to_thread(self._poll_once)
            except sqlite3.OperationalError as e:
                if "authorization denied" in str(e) or "unable to open" in str(e):
                    self._status = (
                        "needs Full Disk Access — grant it to Flowly in "
                        "System Settings → Privacy & Security → Full Disk Access"
                    )
                    logger.warning(f"iMessage: {self._status}")
                    await self._sleep(_FDA_RETRY_SECONDS)
                else:
                    # Transient lock while Messages.app writes — skip the tick.
                    logger.debug(f"iMessage poll skipped: {e}")
                    await self._sleep(self.config.poll_interval_seconds)
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — poller must survive
                logger.error(f"iMessage poll error: {e}")
                await self._sleep(self.config.poll_interval_seconds)
                continue

            self._status = "running"
            for row in rows:
                try:
                    await self._process_row(row)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"iMessage row {row.rowid} failed: {e}")
            await self._retry_pending_attachments()
            await self._sleep(self.config.poll_interval_seconds)

    async def stop(self) -> None:
        self._running = False
        if self._bb_runner is not None:
            try:
                await self._bb_runner.cleanup()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"iMessage webhook cleanup: {e}")
            self._bb_runner = None

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            self._running = False

    # ── inbound: BlueBubbles webhook mode ──────────────────────────

    async def _start_bluebubbles(self) -> None:
        """Listen for BlueBubbles new-message webhooks (no chat.db / FDA)."""
        from aiohttp import web

        url, password = self._bluebubbles  # type: ignore[misc]
        host = self.config.bluebubbles_webhook_host or "127.0.0.1"
        port = int(self.config.bluebubbles_webhook_port or 8642)

        async def _handle(request: "web.Request") -> "web.Response":
            raw = await request.read()
            payload = _parse_webhook_body(raw)
            logger.debug(
                "iMessage webhook received: "
                f"type={payload.get('type') or payload.get('event')!r} "
                f"keys={list(payload.keys())}"
            )
            try:
                await self._on_bb_event(payload)
            except Exception as e:  # noqa: BLE001 — never 500 back to BB
                logger.error(f"iMessage webhook handler error: {e}")
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_post("/bluebubbles-webhook", _handle)
        self._bb_runner = web.AppRunner(app)
        await self._bb_runner.setup()
        site = web.TCPSite(self._bb_runner, host, port)
        try:
            await site.start()
        except OSError as e:
            self._status = f"webhook port {port} unavailable: {e}"
            logger.error(f"iMessage: {self._status}")
            return

        webhook_url = f"http://{host}:{port}/bluebubbles-webhook"
        registered = await bb_register_webhook(url, password, webhook_url)
        self._status = "running (BlueBubbles)"
        logger.info(
            f"iMessage channel via BlueBubbles — webhook {webhook_url} "
            f"(auto-registered: {registered})"
        )
        # Hold the task open until stop().
        while self._running:
            await self._sleep(3600)

    async def _on_bb_event(self, payload: dict) -> None:
        # Accept the message-event family (payload "type" or "event").
        event = payload.get("type") or payload.get("event") or ""
        if event and event not in _BB_MESSAGE_EVENTS:
            return

        # ``data`` may be a dict, a list of dicts, or the fields may sit at
        # the top level — BlueBubbles versions differ. Normalize.
        data = payload.get("data")
        if isinstance(data, list):
            data = next((x for x in data if isinstance(x, dict)), {})
        elif not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}

        guid = data.get("guid") or ""
        if guid:
            if guid in self._bb_seen:
                return  # at-least-once webhook → dedupe
            self._bb_seen[guid] = None
            if len(self._bb_seen) > 4000:
                self._bb_seen.popitem(last=False)

        if data.get("isFromMe") or data.get("fromMe") or data.get("is_from_me"):
            return
        if int(data.get("itemType") or 0) != 0:
            return
        # Tapback reactions arrive as messages — skip.
        if data.get("associatedMessageType"):
            return

        chats = data.get("chats") or []
        chat = chats[0] if chats and isinstance(chats[0], dict) else {}
        chat_guid = (
            data.get("chatGuid") or data.get("chat_guid")
            or chat.get("guid") or chat.get("chatGuid") or ""
        )
        chat_identifier = (
            data.get("chatIdentifier") or data.get("identifier")
            or chat.get("chatIdentifier") or ""
        )
        handle = data.get("handle")
        sender = (
            (handle.get("address") if isinstance(handle, dict) else None)
            or data.get("sender") or data.get("address")
            or chat_identifier or chat_guid
        )
        # Session id: prefer the readable identifier, fall back to the GUID.
        session_chat_id = chat_identifier or chat_guid
        is_group = bool(data.get("isGroup")) or (
            int(chat.get("style") or 0) == _CHAT_STYLE_GROUP
        ) or (";+;" in chat_guid)

        if not session_chat_id or not sender:
            logger.debug(
                f"iMessage webhook dropped: no chat/sender (guid={guid!r})"
            )
            return

        body = data.get("text") or data.get("message") or data.get("body") or ""
        media_paths, transcripts = await self._bb_attachments(data)

        if not body and not media_paths:
            logger.debug(f"iMessage webhook dropped: empty (guid={guid!r})")
            return
        if is_group and not self._bb_group_allowed(session_chat_id, body):
            return
        if is_group:
            body = self._strip_mention(body)

        if not await self._check_dm_access_simple(
            sender, session_chat_id, is_group
        ):
            return

        parts = [body] if body else []
        parts.extend(transcripts)
        if not parts and media_paths:
            parts.append("(attachment)")
        content = "\n".join(parts)
        metadata: dict[str, Any] = {
            "message_guid": guid,
            "is_group": is_group,
            "service": chat.get("service") or "iMessage",
        }
        if chat.get("displayName"):
            metadata["chat_name"] = chat["displayName"]
        if is_group:
            metadata["group_sender"] = sender
            content = f"[from: {sender}] {content}"

        logger.info(f"iMessage inbound via BlueBubbles from {sender}")
        await self._handle_message(
            sender_id=sender,
            chat_id=session_chat_id,
            content=content,
            media=media_paths,
            metadata=metadata,
        )

    def _bb_group_allowed(self, chat_identifier: str, body: str) -> bool:
        policy = self.config.group_policy
        if policy == "open":
            return True
        if policy == "allowlist":
            return chat_identifier in (self.config.group_allow_from or ())
        return any(rx.search(body) for rx in self._mention_regexes)

    async def _check_dm_access_simple(
        self, sender: str, chat_identifier: str, is_group: bool
    ) -> bool:
        """Allowlist/pairing gate for BlueBubbles inbound."""
        if self.is_allowed(sender):
            return True
        if self.config.dm_policy == "allowlist" or is_group:
            logger.debug(f"iMessage: blocked unauthorized sender {sender}")
            return False
        code, created = upsert_pairing_request(
            "imessage", sender, meta={"chat": chat_identifier}
        )
        if created and code:
            try:
                await self._deliver_text(
                    chat_identifier,
                    "Flowly: access required.\n\n"
                    f"Your iMessage handle: {sender}\n"
                    f"Pairing code: {code}\n\n"
                    "Ask the owner to approve:\n"
                    f"flowly pairing approve imessage {code}",
                )
            except (IMessageSendError, BlueBubblesError) as e:
                logger.warning(f"iMessage pairing reply failed: {e}")
            logger.info(f"iMessage pairing request for {sender}: {code}")
        return False

    async def _bb_attachments(
        self, data: dict
    ) -> tuple[list[str], list[str]]:
        """Download BlueBubbles attachments → (media_paths, transcripts)."""
        records = data.get("attachments") or []
        if not records:
            return [], []
        url, password = self._bluebubbles  # type: ignore[misc]
        media_dir = get_flowly_home() / "media"
        media_paths: list[str] = []
        transcripts: list[str] = []
        for att in records:
            att_guid = att.get("guid")
            if not att_guid:
                continue
            mime = att.get("mimeType") or ""
            name = att.get("transferName") or att_guid
            suffix = Path(name).suffix
            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                dest = media_dir / f"imsg-{uuid.uuid4().hex[:12]}{suffix}"
            except OSError as e:
                logger.warning(f"iMessage media dir failed: {e}")
                continue
            if not await bb_download_attachment(url, password, att_guid, dest):
                continue
            if dest.suffix.lower() == ".heic":
                dest = await self._convert_heic(dest)
            media_paths.append(str(dest))
            if mime.startswith("audio/") or dest.suffix.lower() in (".caf", ".amr"):
                transcript = await self._transcribe(dest)
                if transcript:
                    transcripts.append(f"[transcription: {transcript}]")
        return media_paths, transcripts

    # ── inbound: polling ───────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        # mode=ro (NOT immutable) — the DB is WAL and actively written;
        # immutable would hide fresh frames.
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout = 250")
        return conn

    def _poll_once(self) -> list[_Row]:
        """One synchronous poll tick. Runs in a worker thread."""
        conn = self._connect()
        try:
            if self._last_rowid is None:
                # First run: start at the tip — never replay history.
                cur = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message")
                self._last_rowid = int(cur.fetchone()[0])
                self._save_checkpoint(self._last_rowid)
                logger.info(
                    f"iMessage checkpoint initialized at ROWID {self._last_rowid}"
                )
                return []

            rows = [
                _Row(*raw)
                for raw in conn.execute(_TAIL_QUERY, (self._last_rowid,))
            ]
        finally:
            conn.close()

        if rows:
            self._last_rowid = rows[-1].rowid
            self._save_checkpoint(self._last_rowid)
        return rows

    def _fetch_attachments(self, rowid: int) -> list[tuple[str, str, str]]:
        conn = self._connect()
        try:
            return list(conn.execute(_ATTACHMENT_QUERY, (rowid,)))
        finally:
            conn.close()

    # ── inbound: row processing ────────────────────────────────────

    async def _process_row(self, row: _Row) -> None:
        if row.is_from_me:
            return  # own sends (including the agent's replies) — no echo
        if row.item_type != 0:
            return  # group renames, member joins, etc.
        if not row.chat_identifier:
            return

        body = row.text or decode_attributed_body(row.attributed_body) or ""
        media_paths: list[str] = []
        transcripts: list[str] = []

        if row.has_attachments:
            media_paths, transcripts, missing = await self._collect_attachments(row)
            if missing and not body and not media_paths:
                # File not on disk yet (iCloud download lag) — retry next tick.
                self._pending_attachments.setdefault(
                    row.rowid, _PendingAttachment(row=row)
                )
                return

        if not body and not media_paths:
            return

        if row.is_group and not self._group_message_allowed(row, body):
            return
        body = self._strip_mention(body) if row.is_group else body

        sender = row.sender_handle or row.chat_identifier
        if not await self._check_dm_access(row, sender):
            return

        content_parts = [body] if body else []
        content_parts.extend(transcripts)
        if not content_parts and media_paths:
            content_parts.append("(attachment)")

        metadata: dict[str, Any] = {
            "message_guid": row.guid,
            "timestamp": apple_time_to_datetime(row.date).isoformat(),
            "is_group": row.is_group,
            "service": row.service,
        }
        if row.chat_name:
            metadata["chat_name"] = row.chat_name
        if row.is_group:
            metadata["group_sender"] = sender
            content = "\n".join(content_parts)
            content = f"[from: {sender}] {content}"
        else:
            content = "\n".join(content_parts)

        await self._handle_message(
            sender_id=sender,
            chat_id=row.chat_identifier,
            content=content,
            media=media_paths,
            metadata=metadata,
        )

    # ── inbound: access policy ─────────────────────────────────────

    def is_allowed(self, sender_id: str) -> bool:
        """Config allow_from + pairing-store allowlist (open mode allows all)."""
        if self.config.dm_policy == "open":
            return True
        allowed = set(self.config.allow_from or ()) | set(
            read_allow_from_store("imessage")
        )
        if not allowed:
            return False
        sender = str(sender_id)
        return sender in allowed or f"imessage:{sender}" in allowed

    async def _check_dm_access(self, row: _Row, sender: str) -> bool:
        """Apply dm_policy. Returns True when the message may proceed."""
        if self.is_allowed(sender):
            return True
        if self.config.dm_policy == "allowlist" or row.is_group:
            # Group senders never get pairing spam — silent block.
            logger.debug(f"iMessage: blocked unauthorized sender {sender}")
            return False

        code, created = upsert_pairing_request(
            "imessage", sender, meta={"chat": row.chat_identifier}
        )
        if created and code:
            try:
                await self._deliver_text(
                    row.chat_identifier,
                    "Flowly: access required.\n\n"
                    f"Your iMessage handle: {sender}\n"
                    f"Pairing code: {code}\n\n"
                    "Ask the owner to approve:\n"
                    f"flowly pairing approve imessage {code}",
                )
            except IMessageSendError as e:
                logger.warning(
                    f"iMessage pairing code created for {sender} but reply "
                    f"failed ({e}); code visible via `flowly pairing list imessage`"
                )
            logger.info(f"iMessage pairing request for {sender}: {code}")
        return False

    def _compile_mention_patterns(self) -> list[Any]:
        import re

        patterns = list(self.config.mention_patterns or ()) or list(
            _DEFAULT_MENTION_PATTERNS
        )
        compiled = []
        for pat in patterns:
            try:
                compiled.append(re.compile(pat, re.IGNORECASE))
            except re.error as e:
                logger.warning(f"iMessage: invalid mention pattern {pat!r}: {e}")
        return compiled

    def _group_message_allowed(self, row: _Row, body: str) -> bool:
        policy = self.config.group_policy
        if policy == "open":
            return True
        if policy == "allowlist":
            return row.chat_identifier in (self.config.group_allow_from or ())
        # mention policy
        return any(rx.search(body) for rx in self._mention_regexes)

    def _strip_mention(self, body: str) -> str:
        if self.config.group_policy != "mention":
            return body
        for rx in self._mention_regexes:
            stripped, n = rx.subn("", body, count=1)
            if n:
                return stripped.strip(" ,:;—-").strip()
        return body

    # ── inbound: attachments ───────────────────────────────────────

    async def _collect_attachments(
        self, row: _Row
    ) -> tuple[list[str], list[str], bool]:
        """Copy a row's attachments into ~/.flowly/media.

        Returns (media_paths, transcripts, any_missing_on_disk).
        """
        try:
            records = await asyncio.to_thread(self._fetch_attachments, row.rowid)
        except sqlite3.Error as e:
            logger.warning(f"iMessage attachment query failed: {e}")
            return [], [], False

        media_dir = get_flowly_home() / "media"
        media_paths: list[str] = []
        transcripts: list[str] = []
        missing = False

        for filename, mime_type, transfer_name in records:
            if not filename:
                missing = True
                continue
            source = Path(filename).expanduser()
            if not source.exists():
                missing = True
                continue

            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                suffix = source.suffix or Path(transfer_name).suffix
                dest = media_dir / f"imsg-{uuid.uuid4().hex[:12]}{suffix}"
                await asyncio.to_thread(shutil.copy2, source, dest)
            except OSError as e:
                logger.warning(f"iMessage attachment copy failed: {e}")
                continue

            if dest.suffix.lower() == ".heic":
                dest = await self._convert_heic(dest)
            media_paths.append(str(dest))

            if mime_type.startswith("audio/") or dest.suffix.lower() in (
                ".caf",
                ".amr",
            ):
                transcript = await self._transcribe(dest)
                if transcript:
                    transcripts.append(f"[transcription: {transcript}]")

        return media_paths, transcripts, missing

    async def _retry_pending_attachments(self) -> None:
        if not self._pending_attachments:
            return
        for rowid, pending in list(self._pending_attachments.items()):
            del self._pending_attachments[rowid]
            if pending.retries_left <= 0:
                continue
            pending.retries_left -= 1
            try:
                await self._process_row(pending.row)
            except Exception as e:  # noqa: BLE001
                logger.error(f"iMessage attachment retry failed: {e}")

    async def _convert_heic(self, path: Path) -> Path:
        """HEIC → JPEG via sips (ships with macOS). Falls back to the original."""
        target = path.with_suffix(".jpg")
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["sips", "-s", "format", "jpeg", str(path), "--out", str(target)],
                capture_output=True,
                timeout=30,
            )
            if proc.returncode == 0 and target.exists():
                path.unlink(missing_ok=True)
                return target
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.debug(f"sips HEIC conversion failed: {e}")
        return path

    async def _transcribe(self, path: Path) -> str | None:
        if not self._groq_api_key:
            return None
        try:
            from flowly.providers.transcription import GroqTranscriptionProvider

            transcriber = GroqTranscriptionProvider(self._groq_api_key)
            return await transcriber.transcribe(path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"iMessage transcription failed: {e}")
            return None

    # ── outbound ───────────────────────────────────────────────────

    async def send(self, msg: OutboundMessage) -> None:
        """Send a reply/proactive message. Never raises into the dispatcher."""
        target = (msg.chat_id or "").strip()
        if not target:
            logger.error("iMessage send: empty chat_id")
            return

        try:
            content, tagged_media = self._extract_media_tags(msg.content or "")
            text = strip_markdown(content)
            for bubble in split_bubbles(text):
                await self._deliver_text(target, bubble)

            for media_path in [*(msg.media or ()), *tagged_media]:
                path = Path(media_path).expanduser()
                if not path.is_file():
                    logger.warning(f"iMessage media not found: {media_path}")
                    continue
                try:
                    await self._deliver_file(target, path)
                except (IMessageSendError, BlueBubblesError) as e:
                    logger.warning(f"iMessage attachment send failed: {e}")
                    await self._deliver_text(
                        target,
                        f"(attachment couldn't be delivered: {path.name})",
                    )
        except (IMessageSendError, BlueBubblesError) as e:
            logger.error(f"iMessage send to {target} failed: {e}")

    @staticmethod
    def _extract_media_tags(content: str) -> tuple[str, list[str]]:
        """Pull ``MEDIA:/absolute/path`` tokens out of a reply.

        The platform hint tells the agent it may embed these; the
        adapter converts them to native attachments and removes the
        token from the visible text.
        """
        import re

        paths: list[str] = []

        def _grab(m: re.Match) -> str:
            paths.append(m.group(1).strip())
            return ""

        cleaned = re.sub(r"MEDIA:(/[^\s\"']+)", _grab, content)
        return cleaned.strip(), paths

    @property
    def _bluebubbles(self) -> tuple[str, str] | None:
        """(url, password) when outbound should go through BlueBubbles."""
        url = (self.config.bluebubbles_url or "").strip()
        if not url:
            return None
        return url, (self.config.bluebubbles_password or "")

    async def _deliver_text(self, target: str, text: str) -> None:
        if not text.strip():
            return
        bb = self._bluebubbles
        if bb is not None:
            await bb_send_text(bb[0], bb[1], target, text)
            return
        await send_text(target, text, db_path=self.db_path)

    async def _deliver_file(self, target: str, path: Path) -> None:
        bb = self._bluebubbles
        if bb is not None:
            await bb_send_file(bb[0], bb[1], target, path)
            return
        await send_file(target, path, db_path=self.db_path)
