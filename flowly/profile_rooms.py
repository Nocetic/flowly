"""Host-owned group conversations for isolated profiles."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import json
import mimetypes
import os
import re
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

from flowly.profile import default_home
from flowly.profile_host_contract import MAX_PROFILE_MESSAGE_CHARS, ProfileHostError
from flowly.profile_room_store import (
    RoomStoreConflictError,
    RoomStoreError,
    RoomStoreInvalidError,
    RoomStoreLimitError,
    SQLiteRoomStore,
    assign_message_sequences,
    canonical_room_json,
    ensure_message_sequences,
    next_room_sequence,
    room_fingerprints,
)

TargetRpc = Callable[[str, str, dict[str, Any], float], Awaitable[Any]]
TargetPrepare = Callable[[str], Awaitable[Any]]
ProfileDirectory = Callable[[], list[str]]
RoomEventCallback = Callable[[dict[str, Any]], Awaitable[None]]

_STORE_VERSION = 1
_MAX_ROOMS = 200
_MAX_MEMBERS = 6
_MAX_MESSAGES = 1_000
_MAX_CONTEXT_MESSAGES = 40
_DEFAULT_HISTORY_PAGE = 50
_MAX_HISTORY_PAGE = 100
_MAX_HISTORY_CURSOR_CHARS = 512
_MAX_SUMMARY_CONTENT_CHARS = 512
_MAX_LEGACY_STORE_BYTES = 16 * 1024 * 1024
_MAX_TOOL_CALLS = 8
_MAX_ATTACHMENTS = 10
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_MAX_ATTACHMENT_B64_CHARS = ((_MAX_ATTACHMENT_BYTES + 2) // 3) * 4
_MAX_THUMBNAIL_BYTES = 256 * 1024
_MEDIA_READ_CHUNK_BYTES = 1024 * 1024
_GROUP_MEDIA_PREFIX = "group-"
_MAX_COUNCIL_ROUNDS = 3
_MAX_COUNCIL_TURNS = 10
_MEMBER_TIMEOUT_SECONDS = 600.0
_SESSION_PREFIX = "desktop:profile-room:"
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MENTION_RE = re.compile(r"(^|[\s([{])@([a-z0-9][a-z0-9._-]*)", re.IGNORECASE)
_CODE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]*`")
_HISTORY_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_GROUP_NON_EXECUTOR_DISABLED_TOOLS = ["board_add", "board_update", "board_run"]
PROFILE_ROOM_METHODS = (
    "profiles.rooms.list",
    "profiles.rooms.get",
    "profiles.rooms.history",
    "profiles.rooms.import",
    "profiles.rooms.create",
    "profiles.rooms.update",
    "profiles.rooms.delete",
    "profiles.rooms.prepare",
    "profiles.rooms.send",
    "profiles.rooms.stop",
    "profiles.rooms.approval.resolve",
    "profiles.rooms.clarify.resolve",
)
_TOOL_ARGUMENT_KEYS: dict[str, tuple[str, ...]] = {
    "exec": ("command", "cmd", "cwd"),
    "read_file": ("path", "file_path", "uri"),
    "write_file": ("path", "file_path"),
    "edit_file": ("path", "file_path"),
    "list_dir": ("path", "directory", "dir", "uri"),
    "web_search": ("query", "q"),
    "web_fetch": ("url",),
    "browser_tab": ("action", "url", "query"),
    "artifact": ("action", "artifact_id", "title", "type"),
    "board_add": ("title", "assignee_profile"),
    "board_list": ("status", "assignee_profile"),
    "board_get": ("card_id",),
    "board_update": ("card_id", "status", "title", "assignee_profile"),
    "board_run": ("card_id", "goal"),
    "process": ("action", "pid", "command"),
    "cron": ("action", "name"),
    "memory_search": ("query", "key", "id"),
    "memory_get": ("key", "id"),
    "memory_recall": ("query", "key", "id"),
    "session_search": ("query", "search_query", "q", "term"),
    "skill_view": ("name", "skill"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _session_key(room_id: str) -> str:
    return f"{_SESSION_PREFIX}{room_id}"


def _room_id_from_session(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith(_SESSION_PREFIX):
        return ""
    try:
        return str(uuid.UUID(value[len(_SESSION_PREFIX):]))
    except (ValueError, AttributeError):
        return ""


def _room_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ProfileHostError("ROOM_INVALID", "The group identity is invalid.")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ProfileHostError("ROOM_INVALID", "The group identity is invalid.") from exc


def _include_messages(params: dict[str, Any]) -> bool:
    value = params.get("includeMessages", True)
    if not isinstance(value, bool):
        raise ProfileHostError("INVALID_PARAMS", "includeMessages must be a boolean.")
    return value


def _history_limit(value: Any) -> int:
    if value is None:
        return _DEFAULT_HISTORY_PAGE
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_HISTORY_PAGE:
        raise ProfileHostError(
            "INVALID_PARAMS",
            f"Group history limit must be between 1 and {_MAX_HISTORY_PAGE}.",
        )
    return value


def _encode_history_cursor(room_id: str, before_seq: int) -> str:
    """Name a boundary by sequence rather than by the message sitting on it.

    A message-id boundary stops resolving the moment that message leaves the
    window, which made every deep scroll one retention pass away from an
    unrecoverable error. A sequence is a coordinate in a space that only ever
    grows, so ``seq < before`` keeps answering the same question forever.
    """
    payload = json.dumps(
        {"v": 2, "r": room_id, "s": int(before_seq)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_history_cursor(value: Any, room_id: str) -> int | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_HISTORY_CURSOR_CHARS
        or not _HISTORY_CURSOR_RE.fullmatch(value)
    ):
        raise ProfileHostError("ROOM_CURSOR_INVALID", "The group history cursor is invalid.")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding)
        if len(decoded) > 256:
            raise ValueError("oversized cursor")
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ProfileHostError("ROOM_CURSOR_INVALID", "The group history cursor is invalid.") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 2
        or payload.get("r") != room_id
        or set(payload) != {"v", "r", "s"}
    ):
        raise ProfileHostError("ROOM_CURSOR_INVALID", "The group history cursor is invalid.")
    before_seq = payload.get("s")
    if isinstance(before_seq, bool) or not isinstance(before_seq, int) or before_seq < 0:
        raise ProfileHostError("ROOM_CURSOR_INVALID", "The group history cursor is invalid.")
    return before_seq


def _bounded_text(value: Any, *, label: str, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum:
        raise ProfileHostError("INVALID_PARAMS", f"{label} is invalid.")
    clean = value.strip()
    if not clean and not empty:
        raise ProfileHostError("INVALID_PARAMS", f"{label} is required.")
    return clean


def _final_message(payload: dict[str, Any]) -> dict[str, Any]:
    value: Any = payload.get("message", payload.get("content"))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {"content": value}
    return value if isinstance(value, dict) else {}


def _final_text(payload: dict[str, Any]) -> str:
    value = _final_message(payload)
    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(value.get("text"), str):
        return str(value["text"]).strip()
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _project_arguments(name: str, raw: Any) -> str:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    source = raw if isinstance(raw, dict) else {}
    projected: dict[str, Any] = {}
    for key in _TOOL_ARGUMENT_KEYS.get(name, ()):
        value = source.get(key)
        if isinstance(value, str):
            projected[key] = value[:512]
        elif isinstance(value, (bool, int, float)):
            projected[key] = value
    return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))[:8192]


def _parse_mentions(text: str, members: list[str]) -> tuple[bool, set[str]]:
    aliases: dict[str, str] = {}
    for member in members:
        lower = member.lower()
        aliases[lower] = member
        aliases[re.sub(r"[._-]+", "", lower)] = member
    everyone = False
    selected: set[str] = set()
    for match in _MENTION_RE.finditer(_CODE_RE.sub(" ", text)):
        handle = match.group(2).lower()
        if handle in {"everyone", "all"}:
            everyone = True
            continue
        member = aliases.get(handle) or aliases.get(re.sub(r"[._-]+", "", handle))
        if member:
            selected.add(member)
    return everyone, selected


def _sanitize_attachments(value: Any) -> list[dict[str, str]]:
    """Validate a remote group attachment payload without persisting bytes.

    Group rooms are host-owned and can be reached through the relay, so native
    client paths must never cross this boundary. Inline bytes or an HTTPS CDN
    URL are accepted, bounded, and later fanned out only to the responders for
    this turn. The durable room transcript receives metadata separately.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_ATTACHMENTS:
        raise ProfileHostError(
            "INVALID_PARAMS", f"A group message can include up to {_MAX_ATTACHMENTS} files."
        )

    clean: list[dict[str, str]] = []
    inline_bytes = 0
    for raw in value:
        if not isinstance(raw, dict):
            raise ProfileHostError("INVALID_PARAMS", "A group attachment is invalid.")
        file_name = _bounded_text(
            raw.get("fileName"), label="Attachment name", maximum=255
        )
        mime_type = _bounded_text(
            raw.get("mimeType"), label="Attachment type", maximum=128
        )
        attachment = {"fileName": file_name, "mimeType": mime_type}

        content = raw.get("content")
        cdn_url = raw.get("cdnUrl")
        if isinstance(content, str) and content:
            encoded = content
            if content.startswith("data:") and "," in content:
                encoded = content.split(",", 1)[1]
            if len(encoded) > _MAX_ATTACHMENT_B64_CHARS:
                raise ProfileHostError("INVALID_PARAMS", "A group attachment is too large.")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ProfileHostError(
                    "INVALID_PARAMS", "A group attachment contains invalid file data."
                ) from exc
            inline_bytes += len(decoded)
            if inline_bytes > _MAX_ATTACHMENT_BYTES:
                raise ProfileHostError(
                    "INVALID_PARAMS", "Group attachments exceed the 25 MB message limit."
                )
            attachment["content"] = encoded
        elif isinstance(cdn_url, str) and cdn_url:
            if len(cdn_url) > 4_096:
                raise ProfileHostError("INVALID_PARAMS", "A group attachment URL is invalid.")
            parsed = urlsplit(cdn_url)
            if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ProfileHostError("INVALID_PARAMS", "A group attachment URL is invalid.")
            attachment["cdnUrl"] = cdn_url
        else:
            raise ProfileHostError(
                "INVALID_PARAMS", "A group attachment has no remotely readable content."
            )
        thumbnail = raw.get("thumbnail")
        if isinstance(thumbnail, str) and thumbnail:
            if len(thumbnail) > ((_MAX_THUMBNAIL_BYTES + 2) // 3) * 4:
                raise ProfileHostError("INVALID_PARAMS", "A group attachment thumbnail is invalid.")
            try:
                preview = base64.b64decode(thumbnail, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ProfileHostError(
                    "INVALID_PARAMS", "A group attachment thumbnail is invalid."
                ) from exc
            if not preview or len(preview) > _MAX_THUMBNAIL_BYTES:
                raise ProfileHostError("INVALID_PARAMS", "A group attachment thumbnail is invalid.")
            attachment["thumbnail"] = base64.b64encode(preview).decode("ascii")
        clean.append(attachment)
    return clean


def _attachment_kind(mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return "file"


def _safe_attachment_suffix(file_name: str, mime_type: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = mimetypes.guess_extension(mime_type, strict=False) or ""
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""


def _image_thumbnail(path: Path) -> str | None:
    """Return a bounded JPEG preview without making room history carry originals."""
    try:
        from flowly.channels.web import _compress_image_for_transport

        compressed = _compress_image_for_transport(
            path,
            max_dimension=512,
            target_bytes=48 * 1024,
            initial_quality=78,
        )
    except Exception:
        return None
    if compressed is None:
        return None
    encoded = base64.b64encode(compressed[0]).decode("ascii")
    return encoded if len(compressed[0]) <= _MAX_THUMBNAIL_BYTES else None


def _durable_attachment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid attachment")
    file_name = value.get("fileName")
    mime_type = value.get("mimeType")
    if (
        not isinstance(file_name, str)
        or not file_name
        or len(file_name) > 255
        or "\x00" in file_name
        or not isinstance(mime_type, str)
        or not mime_type
        or len(mime_type) > 128
        or "\x00" in mime_type
    ):
        raise ValueError("invalid attachment")
    clean: dict[str, Any] = {"fileName": file_name, "mimeType": mime_type}
    media_id = value.get("mediaId")
    if media_id is not None:
        if (
            not isinstance(media_id, str)
            or not media_id.startswith(_GROUP_MEDIA_PREFIX)
            or len(media_id) > 255
            or media_id != Path(media_id).name
            or "\x00" in media_id
        ):
            raise ValueError("invalid attachment media id")
        clean["mediaId"] = media_id
    cdn_url = value.get("cdnUrl")
    if cdn_url is not None:
        if not isinstance(cdn_url, str) or len(cdn_url) > 4_096:
            raise ValueError("invalid attachment url")
        parsed = urlsplit(cdn_url)
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("invalid attachment url")
        clean["cdnUrl"] = cdn_url
    thumbnail = value.get("thumbnail")
    if thumbnail is not None:
        if not isinstance(thumbnail, str) or len(thumbnail) > ((_MAX_THUMBNAIL_BYTES + 2) // 3) * 4:
            raise ValueError("invalid attachment thumbnail")
        try:
            decoded = base64.b64decode(thumbnail, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid attachment thumbnail") from exc
        if not decoded or len(decoded) > _MAX_THUMBNAIL_BYTES:
            raise ValueError("invalid attachment thumbnail")
        clean["thumbnail"] = base64.b64encode(decoded).decode("ascii")
    kind = value.get("kind")
    if kind is not None:
        if kind not in {"image", "video", "audio", "file"}:
            raise ValueError("invalid attachment kind")
        clean["kind"] = kind
    size = value.get("size")
    if size is not None:
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= _MAX_ATTACHMENT_BYTES:
            raise ValueError("invalid attachment size")
        clean["size"] = size
    status = value.get("status")
    if status is not None:
        if status not in {"ready", "failed", "expired"}:
            raise ValueError("invalid attachment status")
        clean["status"] = status
    for key in ("durationMs", "width", "height"):
        metric = value.get(key)
        if metric is not None:
            if (
                not isinstance(metric, int)
                or isinstance(metric, bool)
                or metric < 0
                or metric > 2_147_483_647
            ):
                raise ValueError(f"invalid attachment {key}")
            clean[key] = metric
    return clean


class ProfileRoomService:
    def __init__(
        self,
        *,
        target_rpc: TargetRpc,
        target_prepare: TargetPrepare | None = None,
        profile_directory: ProfileDirectory,
        on_event: RoomEventCallback | None,
        store_path: Path | None = None,
        member_timeout: float = _MEMBER_TIMEOUT_SECONDS,
    ) -> None:
        self._target_rpc = target_rpc
        self._target_prepare = target_prepare
        self._profile_directory = profile_directory
        self._on_event = on_event
        requested_store = store_path or (default_home() / "profile-rooms.sqlite3")
        if requested_store.suffix.lower() == ".json":
            self._legacy_store_path = requested_store
            self._store_path = requested_store.with_suffix(".sqlite3")
        else:
            self._store_path = requested_store
            self._legacy_store_path = requested_store.with_suffix(".json")
        self._sqlite_store = SQLiteRoomStore(self._store_path)
        self._storage_mode = "sqlite-wal"
        self._store_revision = 0
        self._persisted_fingerprints: dict[str, str] = {}
        self._media_dir = self._store_path.parent / "media"
        self._member_timeout = member_timeout
        self._rooms: dict[str, dict[str, Any]] = {}
        self._active: dict[str, set[str]] = {}
        self._runs: dict[str, dict[str, str]] = {}
        self._waiters: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._early: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
        self._streams: dict[str, dict[str, str]] = {}
        self._activities: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        self._attentions: dict[str, dict[str, dict[str, Any]]] = {}
        self._readiness: dict[str, dict[str, dict[str, str]]] = {}
        self._prepare_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._pending_attachments: dict[str, tuple[int, list[dict[str, str]]]] = {}
        self._epochs: dict[str, int] = {}
        # Highest message sequence already published for each room. This
        # is what makes a snapshot event able to say what it added rather
        # than only that something changed.
        self._emitted_seq: dict[str, int] = {}
        # Stop is a small transaction: freeze incoming frames, checkpoint the
        # visible partials, then abort the member runtimes.  Without this gate a
        # delta can land while the durable checkpoint is being written and be
        # erased by ``_drop_live`` without ever reaching group history.
        self._stopping: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._live_publish_tasks: dict[str, asyncio.Task[None]] = {}
        self._load_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._loaded = False
        self._closed = False

    def _materialize_attachments(
        self,
        room_id: str,
        attachments: list[dict[str, str]],
    ) -> tuple[list[dict[str, Any]], list[Path]]:
        descriptors: list[dict[str, Any]] = []
        created: list[Path] = []
        try:
            for attachment in attachments:
                file_name = attachment["fileName"]
                mime_type = attachment["mimeType"]
                descriptor: dict[str, Any] = {
                    "fileName": file_name,
                    "mimeType": mime_type,
                    "kind": _attachment_kind(mime_type),
                    "status": "ready",
                }
                if attachment.get("cdnUrl"):
                    descriptor["cdnUrl"] = attachment["cdnUrl"]
                    if attachment.get("thumbnail"):
                        descriptor["thumbnail"] = attachment["thumbnail"]
                    descriptors.append(descriptor)
                    continue
                data = base64.b64decode(attachment["content"], validate=True)
                self._media_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                media_id = (
                    f"{_GROUP_MEDIA_PREFIX}{room_id[:8]}-{uuid.uuid4().hex}"
                    f"{_safe_attachment_suffix(file_name, mime_type)}"
                )
                destination = self._media_dir / media_id
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
                created.append(destination)
                descriptor["mediaId"] = media_id
                descriptor["size"] = len(data)
                if descriptor["kind"] == "image":
                    thumbnail = _image_thumbnail(destination)
                    if thumbnail:
                        descriptor["thumbnail"] = thumbnail
                elif attachment.get("thumbnail"):
                    descriptor["thumbnail"] = attachment["thumbnail"]
                descriptors.append(descriptor)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        return descriptors, created

    def _delete_room_media(self, room: dict[str, Any]) -> None:
        for message in room.get("messages", []):
            for attachment in message.get("attachments", []):
                media_id = attachment.get("mediaId") if isinstance(attachment, dict) else None
                if (
                    isinstance(media_id, str)
                    and media_id.startswith(_GROUP_MEDIA_PREFIX)
                    and media_id == Path(media_id).name
                ):
                    (self._media_dir / media_id).unlink(missing_ok=True)

    async def _read_profile_media(
        self,
        profile: str,
        media_id: str,
        remaining: int,
    ) -> tuple[bytes, str]:
        if (
            not media_id
            or len(media_id) > 255
            or media_id.startswith(".")
            or media_id != Path(media_id).name
            or "\x00" in media_id
        ):
            raise ValueError("invalid profile media id")
        offset = 0
        expected_size: int | None = None
        mime_type = ""
        chunks: list[bytes] = []
        while True:
            result = await self._target_rpc(
                profile,
                "media.read",
                {
                    "mediaId": media_id,
                    "offset": offset,
                    "length": _MEDIA_READ_CHUNK_BYTES,
                },
                30,
            )
            if not isinstance(result, dict):
                raise ValueError("invalid profile media response")
            size = result.get("size")
            result_offset = result.get("offset")
            encoded = result.get("data")
            eof = result.get("eof")
            response_mime = result.get("mimeType")
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
                or size > remaining
                or result_offset != offset
                or result.get("mediaId") != media_id
                or not isinstance(encoded, str)
                or len(encoded) > ((_MEDIA_READ_CHUNK_BYTES + 2) // 3) * 4
                or not isinstance(eof, bool)
                or not isinstance(response_mime, str)
                or not response_mime
                or len(response_mime) > 128
                or "\x00" in response_mime
            ):
                raise ValueError("invalid profile media response")
            if expected_size is None:
                expected_size = size
                mime_type = response_mime
            elif size != expected_size or response_mime != mime_type:
                raise ValueError("profile media changed while reading")
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("invalid profile media data") from exc
            if len(chunk) > _MEDIA_READ_CHUNK_BYTES or offset + len(chunk) > size:
                raise ValueError("invalid profile media data")
            chunks.append(chunk)
            offset += len(chunk)
            if eof:
                if offset != size:
                    raise ValueError("incomplete profile media data")
                return b"".join(chunks), mime_type
            if not chunk or offset >= size:
                raise ValueError("incomplete profile media data")

    async def _import_terminal_attachments(
        self,
        room_id: str,
        profile: str,
        terminal: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[Path]]:
        raw = _final_message(terminal).get("attachments")
        if raw is None:
            return [], []
        if not isinstance(raw, list) or len(raw) > _MAX_ATTACHMENTS:
            logger.warning("Ignoring invalid attachment list from group member {}", profile)
            return [], []

        descriptors: list[dict[str, Any]] = []
        created: list[Path] = []
        imported_bytes = 0
        for value in raw:
            created_before = len(created)
            try:
                if not isinstance(value, dict):
                    raise ValueError("invalid attachment")
                file_name = _bounded_text(
                    value.get("fileName"), label="Attachment name", maximum=255
                )
                mime_type = _bounded_text(
                    value.get("mimeType"), label="Attachment type", maximum=128
                )
                if isinstance(value.get("cdnUrl"), str) and value.get("cdnUrl"):
                    candidate = {
                        key: value[key]
                        for key in (
                            "fileName", "mimeType", "cdnUrl", "thumbnail", "kind",
                            "size", "status", "durationMs", "width", "height",
                        )
                        if key in value
                    }
                    descriptors.append(_durable_attachment(candidate))
                    continue

                media_id = value.get("mediaId")
                if not isinstance(media_id, str):
                    raise ValueError("attachment has no readable source")
                data, actual_mime = await self._read_profile_media(
                    profile, media_id, _MAX_ATTACHMENT_BYTES - imported_bytes
                )
                imported_bytes += len(data)
                incoming = {
                    "fileName": file_name,
                    "mimeType": actual_mime or mime_type,
                    "content": base64.b64encode(data).decode("ascii"),
                }
                if isinstance(value.get("thumbnail"), str):
                    incoming["thumbnail"] = value["thumbnail"]
                materialized, paths = self._materialize_attachments(room_id, [incoming])
                created.extend(paths)
                descriptor = materialized[0]
                for key in ("kind", "durationMs", "width", "height"):
                    if key in value:
                        descriptor[key] = value[key]
                descriptor = _durable_attachment(descriptor)
                descriptors.append(descriptor)
            except Exception as exc:
                for path in created[created_before:]:
                    path.unlink(missing_ok=True)
                del created[created_before:]
                logger.warning(
                    "Ignoring unreadable attachment from group member {}: {}",
                    profile,
                    str(exc)[:200],
                )
        return descriptors, created

    @property
    def methods(self) -> tuple[str, ...]:
        return PROFILE_ROOM_METHODS

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {
            "modes": ["panel", "council"],
            "maxMembers": _MAX_MEMBERS,
            "councilRounds": _MAX_COUNCIL_ROUNDS,
            "councilTurns": _MAX_COUNCIL_TURNS,
            "summaries": True,
            "historyPagination": {
                "defaultPageSize": _DEFAULT_HISTORY_PAGE,
                "maxPageSize": _MAX_HISTORY_PAGE,
                "cursor": "opaque-v2",
            },
            # Every bound a client would otherwise have to guess. The live
            # window is what a room carries in memory and in a snapshot
            # payload; durable history is not bounded by it, so a client must
            # not treat the window size as the number of messages that exist.
            "retention": {
                "model": "retain-beyond-window",
                "liveWindow": _MAX_MESSAGES,
                "durableHistory": True,
            },
            "roomEvents": ["full-v1", "delta-v1"],
            "storage": "sqlite-wal",
            "legacyJsonMigration": "verified-copy-preserve-source",
        }

    def accepts_event(self, profile: str, session_key: str, run_id: str = "") -> bool:
        room_id = _room_id_from_session(session_key)
        room = self._rooms.get(room_id) if room_id else None
        if room is not None and profile in room.get("members", []):
            return True
        return any(runs.get(profile) == run_id for runs in self._runs.values()) if run_id else False

    def is_profile_active(self, profile: str) -> bool:
        """Return whether a profile currently owns work in any live room turn."""
        if any(profile in active for active in self._active.values()):
            return True
        return any(
            isinstance(room.get("run"), dict)
            and room["run"].get("state") == "running"
            and isinstance(room["run"].get("members"), dict)
            and isinstance(room["run"]["members"].get(profile), dict)
            and room["run"]["members"][profile].get("state") in {
                "queued", "running", "needs_user",
            }
            for room in self._rooms.values()
        )

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "profiles.rooms.list":
            return {"rooms": await self.list(include_messages=_include_messages(params))}
        if method == "profiles.rooms.get":
            return {"room": await self.get(
                params.get("roomId"), include_messages=_include_messages(params),
            )}
        if method == "profiles.rooms.history":
            return await self.history(
                params.get("roomId"), params.get("cursor"), params.get("limit"),
            )
        if method == "profiles.rooms.import":
            return await self.import_rooms(params.get("rooms"))
        if method == "profiles.rooms.create":
            return {"room": await self.create(
                params.get("title"), params.get("members"), params.get("mode"),
                include_messages=_include_messages(params),
            )}
        if method == "profiles.rooms.update":
            return {"room": await self.update(
                params.get("roomId"), params.get("title"), params.get("members"),
                params.get("mode"), include_messages=_include_messages(params),
            )}
        if method == "profiles.rooms.delete":
            await self.delete(params.get("roomId"))
            return {"ok": True}
        if method == "profiles.rooms.prepare":
            return {"room": await self.prepare(
                params.get("roomId"), include_messages=_include_messages(params),
            )}
        if method == "profiles.rooms.send":
            return {"room": await self.send(
                params.get("roomId"), params.get("content"), params.get("attachments"),
                include_messages=_include_messages(params),
            )}
        if method == "profiles.rooms.stop":
            await self.stop(params.get("roomId"))
            return {"ok": True}
        if method == "profiles.rooms.approval.resolve":
            await self.resolve_approval(params.get("roomId"), params.get("id"), params.get("decision"))
            return {"ok": True}
        if method == "profiles.rooms.clarify.resolve":
            await self.resolve_clarify(params.get("roomId"), params.get("id"), params.get("answer"))
            return {"ok": True}
        raise ProfileHostError("METHOD_NOT_ALLOWED", "This group operation is not available.")

    async def list(self, *, include_messages: bool = True) -> list[dict[str, Any]]:
        await self._load()
        return [self._public(room, include_messages=include_messages) for room in sorted(
            self._rooms.values(), key=lambda item: item["updatedAt"], reverse=True
        )]

    async def get(self, raw_id: Any, *, include_messages: bool = True) -> dict[str, Any]:
        room_id = _room_id(raw_id)
        await self._load()
        return self._public(self._require(room_id), include_messages=include_messages)

    async def history(
        self,
        raw_id: Any,
        cursor: Any = None,
        limit: Any = None,
    ) -> dict[str, Any]:
        """Return one stable, newest-first page boundary in chronological order."""
        room_id = _room_id(raw_id)
        page_limit = _history_limit(limit)
        before_seq = _decode_history_cursor(cursor, room_id)
        await self._load()
        room = self._require(room_id)
        if self._storage_mode == "sqlite-wal" and self._sqlite_store.exists():
            # The durable store is the pager. Reading a page is an index
            # descent on (room_id, seq); it does not depend on how much of the
            # room happens to be resident, so a group keeps its whole history
            # reachable while memory stays bounded to the live window.
            try:
                page, next_seq, has_more, total = await asyncio.to_thread(
                    self._sqlite_store.history_page,
                    room_id,
                    before_seq=before_seq,
                    limit=page_limit,
                )
            except RoomStoreError as exc:
                raise ProfileHostError(
                    "ROOM_HISTORY_UNAVAILABLE",
                    "The group history could not be read.",
                    retryable=True,
                ) from exc
        else:
            page, next_seq, has_more, total = self._history_page_in_memory(
                room, before_seq=before_seq, limit=page_limit
            )
        return {
            "roomId": room_id,
            "messages": page,
            "nextCursor": (
                _encode_history_cursor(room_id, next_seq) if next_seq is not None else None
            ),
            "hasMore": has_more,
            "totalCount": total,
        }

    @staticmethod
    def _history_page_in_memory(
        room: dict[str, Any],
        *,
        before_seq: int | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int | None, bool, int]:
        """Page the resident window when no SQLite authority is available.

        Only reachable in the preserved-legacy-JSON fallback, where the whole
        room is resident anyway. Same contract as the durable pager so the
        wire response is identical either way.
        """
        messages = room["messages"]
        eligible = [
            message for message in messages
            if before_seq is None or int(message.get("seq", 0)) < before_seq
        ]
        window = eligible[-limit:] if limit < len(eligible) else eligible
        page = json.loads(json.dumps(window))
        has_more = len(window) < len(eligible)
        next_seq = int(window[0]["seq"]) if has_more and window else None
        return page, next_seq, has_more, len(messages)

    async def prepare(
        self,
        raw_id: Any,
        *,
        include_messages: bool = True,
    ) -> dict[str, Any]:
        """Warm every member runtime without making room availability all-or-nothing."""
        room_id = _room_id(raw_id)
        await self._load()
        room = self._require(room_id)
        if self._closed:
            raise ProfileHostError("HOST_STOPPED", "The profile host is shutting down.")

        tasks: list[asyncio.Task[None]] = []
        changed = False
        readiness = self._readiness.setdefault(room_id, {})
        for profile in room["members"]:
            key = (room_id, profile)
            task = self._prepare_tasks.get(key)
            if task is None:
                current = readiness.get(profile)
                if not isinstance(current, dict) or current.get("state") != "ready":
                    readiness[profile] = {
                        "state": "starting",
                        "updatedAt": _now(),
                        "error": "",
                    }
                    changed = True
                task = asyncio.create_task(
                    self._prepare_member(room_id, profile),
                    name=f"profile-room-prepare:{room_id}:{profile}",
                )
                self._prepare_tasks[key] = task
                task.add_done_callback(
                    lambda completed, member_key=key: self._prepare_tasks.pop(member_key, None)
                    if self._prepare_tasks.get(member_key) is completed else None
                )
            tasks.append(task)

        if changed:
            await self._emit({
                "roomId": room_id,
                "type": "readiness",
                "room": self._public(room),
            })
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))
        return self._public(self._require(room_id), include_messages=include_messages)

    async def _prepare_member(self, room_id: str, profile: str) -> None:
        try:
            if self._target_prepare is not None:
                await self._target_prepare(profile)
            else:
                await self._target_rpc(profile, "sessions.list", {}, 30)
            state = "ready"
            error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state = "error"
            error = (
                exc.message if isinstance(exc, ProfileHostError) else str(exc)
            )[:500] or "This group member could not start."

        room = self._rooms.get(room_id)
        if room is None or profile not in room["members"]:
            return
        self._readiness.setdefault(room_id, {})[profile] = {
            "state": state,
            "updatedAt": _now(),
            "error": error,
        }
        await self._emit({
            "roomId": room_id,
            "type": "readiness",
            "profile": profile,
            "room": self._public(room),
        })

    async def import_rooms(self, value: Any) -> dict[str, Any]:
        """Import the legacy Desktop-owned room store exactly once.

        The operation is deliberately additive and transactional. Existing
        rooms are never overwritten: a retry is accepted only when the
        destination still contains the imported transcript. A UUID collision
        with different history rejects the whole batch, so Desktop cannot
        silently switch authorities after losing part of its local history.
        """
        await self._load()
        if not isinstance(value, list) or len(value) > _MAX_ROOMS:
            raise ProfileHostError("INVALID_PARAMS", "Imported groups are invalid.")
        try:
            if (
                len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
                > _MAX_LEGACY_STORE_BYTES
            ):
                raise ValueError("oversized import")
        except (TypeError, ValueError) as exc:
            raise ProfileHostError("INVALID_PARAMS", "Imported groups are invalid.") from exc

        imported: list[dict[str, Any]] = []
        existing_ids: list[str] = []
        conflicts: list[str] = []
        seen: set[str] = set()
        for raw in value:
            room = self._import_room(raw)
            room_id = room["id"]
            if room_id in seen:
                raise ProfileHostError("INVALID_PARAMS", "Imported groups contain a duplicate identity.")
            seen.add(room_id)
            current = self._rooms.get(room_id)
            if current is None:
                imported.append(room)
            elif self._contains_import(current, room):
                existing_ids.append(room_id)
            else:
                conflicts.append(room_id)

        if conflicts:
            return {
                "ok": False,
                "imported": 0,
                "existing": len(existing_ids),
                "conflicts": conflicts,
            }
        if len(self._rooms) + len(imported) > _MAX_ROOMS:
            raise ProfileHostError("ROOM_LIMIT", "Delete a group before importing another one.")

        previous = self._rooms
        self._rooms = {**previous, **{room["id"]: room for room in imported}}
        try:
            await self._persist()
        except Exception:
            self._rooms = previous
            raise
        for room in imported:
            await self._emit({
                "roomId": room["id"], "type": "updated", "room": self._public(room),
            })
        return {
            "ok": True,
            "imported": len(imported),
            "existing": len(existing_ids),
            "conflicts": [],
        }

    async def create(
        self,
        title: Any,
        members: Any,
        mode: Any = None,
        *,
        include_messages: bool = True,
    ) -> dict[str, Any]:
        await self._load()
        if len(self._rooms) >= _MAX_ROOMS:
            raise ProfileHostError("ROOM_LIMIT", "Delete a group before creating another one.")
        clean_title, clean_members = self._definition(title, members)
        clean_mode = self._room_mode(mode)
        timestamp = _now()
        room_id = str(uuid.uuid4())
        room = {
            "id": room_id, "title": clean_title, "members": clean_members,
            "mode": clean_mode,
            "messages": [], "watermarks": {member: 0 for member in clean_members},
            "createdAt": timestamp, "updatedAt": timestamp,
            "nextSeq": 0, "trimmedCount": 0,
        }
        self._rooms[room_id] = room
        try:
            await self._persist()
        except Exception:
            self._rooms.pop(room_id, None)
            raise
        public = self._public(room)
        await self._emit({"roomId": room_id, "type": "updated", "room": public})
        return self._public(room, include_messages=include_messages)

    async def update(
        self,
        raw_id: Any,
        title: Any,
        members: Any,
        mode: Any = None,
        *,
        include_messages: bool = True,
    ) -> dict[str, Any]:
        room_id = _room_id(raw_id)
        await self._load()
        room = self._require(room_id)
        if self._room_is_running(room):
            raise ProfileHostError("ROOM_BUSY", "Stop the active group response before changing its members.")
        clean_title, clean_members = self._definition(title, members)
        clean_mode = self._room_mode(mode, default=str(room.get("mode") or "panel"))
        previous = {
            "title": room["title"],
            "members": list(room["members"]),
            "watermarks": dict(room["watermarks"]),
            "mode": room.get("mode", "panel"),
            "updatedAt": room["updatedAt"],
        }
        previous_readiness = {
            member: dict(value)
            for member, value in self._readiness.get(room_id, {}).items()
        }
        old_members = set(room["members"])
        room["title"] = clean_title
        room["members"] = clean_members
        room["mode"] = clean_mode
        room["watermarks"] = {
            member: min(int(room["watermarks"].get(member, 0)), len(room["messages"]))
            if member in old_members else 0 for member in clean_members
        }
        readiness = self._readiness.get(room_id)
        if readiness is not None:
            self._readiness[room_id] = {
                member: value for member, value in readiness.items()
                if member in clean_members
            }
        room["updatedAt"] = _now()
        try:
            await self._persist()
        except Exception:
            room.update(previous)
            if previous_readiness:
                self._readiness[room_id] = previous_readiness
            else:
                self._readiness.pop(room_id, None)
            raise
        public = self._public(room)
        await self._emit({"roomId": room_id, "type": "updated", "room": public})
        return self._public(room, include_messages=include_messages)

    async def delete(self, raw_id: Any) -> None:
        room_id = _room_id(raw_id)
        await self._load()
        room = self._require(room_id)
        if self._room_is_running(room):
            raise ProfileHostError("ROOM_BUSY", "Stop the active group response before deleting it.")
        previous_epoch = self._epochs.get(room_id)
        previous_readiness = self._readiness.get(room_id)
        self._rooms.pop(room_id)
        self._emitted_seq.pop(room_id, None)
        self._readiness.pop(room_id, None)
        self._epochs[room_id] = self._epochs.get(room_id, 0) + 1
        self._pending_attachments.pop(room_id, None)
        self._drop_live(room_id)
        try:
            await self._persist()
        except Exception:
            self._rooms[room_id] = room
            if previous_readiness is not None:
                self._readiness[room_id] = previous_readiness
            if previous_epoch is None:
                self._epochs.pop(room_id, None)
            else:
                self._epochs[room_id] = previous_epoch
            raise
        self._delete_room_media(room)
        await self._emit({"roomId": room_id, "type": "deleted"})
        await asyncio.gather(*(
            self._target_rpc(member, "sessions.delete", {"sessionKey": _session_key(room_id)}, 30)
            for member in room["members"]
        ), return_exceptions=True)

    async def send(
        self,
        raw_id: Any,
        content: Any,
        attachments: Any = None,
        *,
        include_messages: bool = True,
    ) -> dict[str, Any]:
        room_id = _room_id(raw_id)
        clean_attachments = _sanitize_attachments(attachments)
        clean = _bounded_text(
            content,
            label="Group message",
            maximum=MAX_PROFILE_MESSAGE_CHARS,
            empty=bool(clean_attachments),
        )
        if not clean:
            clean = "Review the attached files."
        await self._load()
        room = self._require(room_id)
        if self._room_is_running(room):
            raise ProfileHostError("ROOM_BUSY", "This group is already responding.", retryable=True)
        durable_attachments, created_media = self._materialize_attachments(
            room_id, clean_attachments
        ) if clean_attachments else ([], [])
        everyone, selected = _parse_mentions(clean, room["members"])
        responders = list(room["members"]) if everyone or not selected else [
            member for member in room["members"] if member in selected
        ]
        previous_messages = list(room["messages"])
        previous_trimmed = int(room.get("trimmedCount", 0))
        previous_watermarks = dict(room["watermarks"])
        previous_run = room.get("run")
        previous_updated_at = room["updatedAt"]
        previous_epoch = self._epochs.get(room_id)
        previous_readiness = {
            profile: dict(value)
            for profile, value in self._readiness.get(room_id, {}).items()
        }
        timestamp = _now()
        message: dict[str, Any] = {
            "id": str(uuid.uuid4()), "role": "user", "content": clean, "createdAt": timestamp,
        }
        if durable_attachments:
            message["attachments"] = durable_attachments
        self._append_message(room, message)
        self._trim(room)
        epoch = self._epochs.get(room_id, 0) + 1
        self._epochs[room_id] = epoch
        if clean_attachments:
            self._pending_attachments[room_id] = (epoch, clean_attachments)
        else:
            self._pending_attachments.pop(room_id, None)
        self._active[room_id] = set(responders)
        readiness = self._readiness.setdefault(room_id, {})
        for profile in responders:
            if readiness.get(profile, {}).get("state") != "ready":
                readiness[profile] = {
                    "state": "starting",
                    "updatedAt": timestamp,
                    "error": "",
                }
        boundary = len(room["messages"])
        group_run_id = str(uuid.uuid4())
        room["run"] = {
            "id": group_run_id,
            "mode": str(room.get("mode") or "panel"),
            "state": "running",
            "startedAt": timestamp,
            "finishedAt": "",
            "round": 0,
            "turnCount": 0,
            "members": {
                profile: {
                    "state": "queued",
                    "boundary": boundary,
                    "gatewayRunId": "",
                    "updatedAt": timestamp,
                    "error": "",
                }
                for profile in responders
            },
        }
        room["updatedAt"] = timestamp
        try:
            await self._persist()
        except Exception:
            for path in created_media:
                path.unlink(missing_ok=True)
            room["messages"] = previous_messages
            room["trimmedCount"] = previous_trimmed
            room["watermarks"] = previous_watermarks
            if previous_run is None:
                room.pop("run", None)
            else:
                room["run"] = previous_run
            room["updatedAt"] = previous_updated_at
            self._active.pop(room_id, None)
            pending = self._pending_attachments.get(room_id)
            if pending and pending[0] == epoch:
                self._pending_attachments.pop(room_id, None)
            if previous_epoch is None:
                self._epochs.pop(room_id, None)
            else:
                self._epochs[room_id] = previous_epoch
            if previous_readiness:
                self._readiness[room_id] = previous_readiness
            else:
                self._readiness.pop(room_id, None)
            raise
        public = self._public(room)
        await self._emit({"roomId": room_id, "type": "updated", "room": public})
        runner = self._run_council if room.get("mode") == "council" else self._run_room
        task = asyncio.create_task(
            runner(room_id, epoch, responders, bool(selected and not everyone)),
            name=f"profile-room:{room_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self._public(room, include_messages=include_messages)

    async def stop(self, raw_id: Any) -> None:
        room_id = _room_id(raw_id)
        await self._load()
        room = self._require(room_id)
        if room_id in self._stopping:
            return

        previous_epoch = self._epochs.get(room_id)
        previous_messages = list(room["messages"])
        previous_trimmed = int(room.get("trimmedCount", 0))
        previous_watermarks = dict(room["watermarks"])
        previous_run = json.loads(json.dumps(room.get("run"))) if room.get("run") else None
        previous_updated_at = room["updatedAt"]
        previous_pending = self._pending_attachments.get(room_id)
        runs = list(self._runs.get(room_id, {}).items())
        self._stopping.add(room_id)
        try:
            finished_at = _now()
            self._checkpoint_aborted_messages(room_id, finished_at)
            self._epochs[room_id] = self._epochs.get(room_id, 0) + 1
            self._pending_attachments.pop(room_id, None)
            run = room.get("run")
            if isinstance(run, dict) and run.get("state") == "running":
                for member in run.get("members", {}).values():
                    if isinstance(member, dict) and member.get("state") in {
                        "queued", "running", "needs_user",
                    }:
                        member.update({
                            "state": "aborted",
                            "updatedAt": finished_at,
                            "error": "The group response was stopped.",
                        })
                run["state"] = "aborted"
                run["finishedAt"] = finished_at
                room["updatedAt"] = finished_at
            try:
                await self._persist()
            except Exception:
                room["messages"] = previous_messages
                room["trimmedCount"] = previous_trimmed
                room["watermarks"] = previous_watermarks
                if previous_run is None:
                    room.pop("run", None)
                else:
                    room["run"] = previous_run
                room["updatedAt"] = previous_updated_at
                if previous_epoch is None:
                    self._epochs.pop(room_id, None)
                else:
                    self._epochs[room_id] = previous_epoch
                if previous_pending is None:
                    self._pending_attachments.pop(room_id, None)
                else:
                    self._pending_attachments[room_id] = previous_pending
                raise

            # Only retire waiters after the partial transcript is durable.  A
            # failed store write therefore leaves the live run recoverable and
            # the caller receives an honest stop failure instead of silent loss.
            for profile, run_id in runs:
                waiter = self._waiters.pop((profile, run_id), None)
                if waiter and not waiter.done():
                    waiter.set_exception(ProfileHostError(
                        "ROOM_STOPPED", "The group response was stopped."
                    ))
            await asyncio.gather(*(
                self._target_rpc(profile, "chat.abort", {"runId": run_id}, 30)
                for profile, run_id in runs
            ), return_exceptions=True)
            self._drop_live(room_id)
            await self._emit({
                "roomId": room_id,
                "type": "run-state",
                "room": self._public(self._rooms[room_id]),
            })
        finally:
            self._stopping.discard(room_id)

    async def stop_for_profile(self, profile: str) -> None:
        await self._load()
        for room_id, room in list(self._rooms.items()):
            run = room.get("run")
            members = run.get("members") if isinstance(run, dict) else None
            member = members.get(profile) if isinstance(members, dict) else None
            if (
                profile in self._active.get(room_id, set())
                or (
                    isinstance(run, dict)
                    and run.get("state") == "running"
                    and isinstance(member, dict)
                    and member.get("state") in {"queued", "running", "needs_user"}
                )
            ):
                await self.stop(room_id)

    async def remove_profile(self, profile: str) -> None:
        await self._load()
        changed = False
        for room_id, room in list(self._rooms.items()):
            if profile not in room["members"]:
                continue
            changed = True
            members = [member for member in room["members"] if member != profile]
            if len(members) < 2:
                self._rooms.pop(room_id)
                self._emitted_seq.pop(room_id, None)
                self._readiness.pop(room_id, None)
                self._drop_live(room_id)
                await self._emit({"roomId": room_id, "type": "deleted"})
                continue
            room["members"] = members
            self._readiness.get(room_id, {}).pop(profile, None)
            room["watermarks"].pop(profile, None)
            room["updatedAt"] = _now()
            await self._emit({
                "roomId": room_id, "type": "updated", "room": self._public(room),
            })
        if changed:
            await self._persist()

    async def resolve_approval(self, raw_id: Any, request_id: Any, decision: Any) -> None:
        room_id = _room_id(raw_id)
        request = self._attention(room_id, request_id, "approval")
        if decision not in {"allow-once", "allow-always", "deny"}:
            raise ProfileHostError("INVALID_PARAMS", "The approval decision is invalid.")
        await self._target_rpc(
            request["profile"], "exec.approval.resolve",
            {"id": request["id"], "decision": decision}, 30,
        )
        await self._clear_attention(room_id, request["id"])

    async def resolve_clarify(self, raw_id: Any, request_id: Any, answer: Any) -> None:
        room_id = _room_id(raw_id)
        request = self._attention(room_id, request_id, "clarify")
        clean = _bounded_text(answer, label="Answer", maximum=MAX_PROFILE_MESSAGE_CHARS)
        await self._target_rpc(
            request["profile"], "agent.clarify.resolve",
            {"id": request["id"], "answer": clean}, 30,
        )
        await self._clear_attention(room_id, request["id"])

    async def handle_profile_event(
        self,
        profile: str,
        event: str,
        payload: dict[str, Any],
    ) -> bool:
        room_id = _room_id_from_session(payload.get("sessionKey"))
        run_id = str(payload.get("runId") or "")
        if not room_id and run_id:
            for candidate, runs in self._runs.items():
                if runs.get(profile) == run_id:
                    room_id = candidate
                    break
        if room_id and not run_id:
            run_id = self._runs.get(room_id, {}).get(profile, "")
        if not room_id or room_id not in self._rooms:
            return False
        if room_id in self._stopping:
            # The stop transaction already captured the last published prefix.
            # Frames racing the durable write belong to the cancelled run and
            # must not resurrect its live bubble or create a duplicate message.
            return True
        if (
            profile not in self._active.get(room_id, set())
            and self._runs.get(room_id, {}).get(profile) != run_id
        ):
            # Internal room sessions must never leak into the ordinary profile
            # event stream. A terminal frame can legitimately arrive after an
            # abort, timeout, or host restart; record its disposition without
            # mutating the transcript or advancing the delivery watermark.
            if event == "chat" and str(payload.get("state") or "") in {
                "final", "aborted", "error",
            }:
                await self._record_late_terminal(
                    room_id, profile, run_id, str(payload.get("state") or "error")
                )
            return True

        if event in {"exec.approval.requested", "agent.clarify.requested"}:
            request_id = str(payload.get("id") or "")
            if request_id:
                kind = "approval" if event.startswith("exec.") else "clarify"
                self._attentions.setdefault(room_id, {})[request_id] = {
                    "kind": kind,
                    "profile": profile,
                    "id": request_id,
                    "request": self._safe_request(payload),
                }
                member = self._run_member(self._rooms[room_id], profile)
                if member is not None:
                    member["state"] = "needs_user"
                    member["attention"] = dict(
                        self._attentions[room_id][request_id]
                    )
                    member["updatedAt"] = _now()
                    await self._persist()
                await self._emit({
                    "roomId": room_id,
                    "type": "attention",
                    "profile": profile,
                    "room": self._public(self._rooms[room_id]),
                })
            return True
        if event in {"exec.approval.resolved", "agent.clarify.resolved"}:
            request_id = str(payload.get("id") or "")
            if request_id:
                await self._clear_attention(room_id, request_id)
            return True

        key = (profile, run_id)
        waiter = self._waiters.get(key)
        if waiter is None:
            if run_id:
                if key not in self._early and len(self._early) >= 64:
                    return True
                queued = self._early.setdefault(key, [])
                if len(queued) < 256:
                    queued.append((event, dict(payload)))
            return True
        if event == "agent":
            stream = payload.get("stream")
            inner = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            if stream == "assistant" and isinstance(inner.get("text"), str):
                text = str(inner["text"])
                room_streams = self._streams.setdefault(room_id, {})
                room_streams[profile] = room_streams.get(profile, "") + text
                await self._emit_live(room_id, "stream", profile)
            elif stream == "tool" and self._update_activity(room_id, profile, inner):
                await self._emit_live(room_id, "activity", profile)
            return True
        if event != "chat":
            return True
        state = str(payload.get("state") or "")
        if state == "iteration_step":
            self._streams.get(room_id, {}).pop(profile, None)
            changed = self._iteration_activities(room_id, profile, payload)
            await self._emit_live(room_id, "activity" if changed else "stream", profile)
            return True
        if state not in {"final", "aborted", "error"}:
            return True
        # Keep the terminal and its last published prefix reachable until the
        # member task durably commits it.  Stop can race the tiny window
        # between this frame and ``_commit_member_terminal``; removing either
        # here used to turn an already-visible answer into an empty stopped
        # row.  ``_finish_member`` owns the cleanup after commit/abort.
        if waiter.done():
            return True
        if state == "final":
            waiter.set_result(payload)
        else:
            waiter.set_exception(ProfileHostError(
                "ROOM_MEMBER_FAILED",
                "A group member stopped before completing its response."
                if state == "aborted"
                else str(payload.get("errorMessage") or "A group member could not complete its response.")[:500],
            ))
        return True

    async def shutdown(self) -> None:
        if self._closed:
            return
        for room_id, room in list(self._rooms.items()):
            if self._room_is_running(room):
                await self.stop(room_id)
        self._closed = True
        prepare_tasks = list(self._prepare_tasks.values())
        for task in prepare_tasks:
            task.cancel()
        await asyncio.gather(*prepare_tasks, return_exceptions=True)
        self._prepare_tasks.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        publishers = list(self._live_publish_tasks.values())
        for task in publishers:
            task.cancel()
        await asyncio.gather(*publishers, return_exceptions=True)
        self._live_publish_tasks.clear()
        for waiter in self._waiters.values():
            if not waiter.done():
                waiter.set_exception(ProfileHostError(
                    "HOST_STOPPED", "The profile host is shutting down."
                ))
        self._waiters.clear()

    async def _run_room(
        self,
        room_id: str,
        epoch: int,
        responders: list[str],
        _directed: bool = False,
    ) -> None:
        room = self._rooms.get(room_id)
        if room is None:
            return
        snapshot = [dict(message) for message in room["messages"]]
        group_run = room.get("run") if isinstance(room.get("run"), dict) else {}
        group_run_id = str(group_run.get("id") or uuid.uuid4())
        executor = "default" if "default" in responders else responders[0]
        pending = self._pending_attachments.get(room_id)
        attachments = pending[1] if pending and pending[0] == epoch else []

        async def run_member(profile: str) -> None:
            run_id = ""
            try:
                if not self._current(room_id, epoch):
                    return
                start = max(0, int(room["watermarks"].get(profile, 0)))
                member = self._run_member(room, profile)
                boundary = int((member or {}).get("boundary", len(snapshot)))
                delta = [
                    message for message in snapshot[start:]
                    if message.get("role") != "assistant" or message.get("profile") != profile
                ][-_MAX_CONTEXT_MESSAGES:]
                if not delta:
                    return
                params: dict[str, Any] = {
                    "sessionKey": _session_key(room_id),
                    "message": self._prompt(room, profile, delta),
                    "thinking": False,
                    "idempotencyKey": f"room-{group_run_id}-{profile}",
                    "disabledTools": ["message_profile"] + (
                        [] if profile == executor else _GROUP_NON_EXECUTOR_DISABLED_TOOLS
                    ),
                    "turnOrigin": "group",
                }
                if attachments:
                    params["attachments"] = attachments
                accepted = await self._target_rpc(profile, "chat.send", params, 60)
                run_id = str((accepted or {}).get("runId") or "")
                if not run_id:
                    raise ProfileHostError(
                        "ROOM_START_FAILED",
                        "A group member did not accept the response.",
                        retryable=True,
                    )
                await self._set_member_readiness(room_id, profile, "ready")
                self._runs.setdefault(room_id, {})[profile] = run_id
                if member is not None:
                    member.update({
                        "state": "running",
                        "gatewayRunId": run_id,
                        "updatedAt": _now(),
                        "error": "",
                    })
                    member.pop("attention", None)
                    await self._persist()
                if not self._current(room_id, epoch):
                    await self._target_rpc(profile, "chat.abort", {"runId": run_id}, 30)
                    return
                future = asyncio.get_running_loop().create_future()
                self._waiters[(profile, run_id)] = future
                for queued_event, queued_payload in self._early.pop((profile, run_id), []):
                    await self.handle_profile_event(profile, queued_event, queued_payload)
                terminal = await asyncio.wait_for(future, self._member_timeout)
                await self._commit_member_terminal(
                    room_id, profile, terminal, boundary, epoch
                )
                if self._current(room_id, epoch):
                    self._finish_member(room_id, profile)
                    await self._emit({
                        "roomId": room_id,
                        "type": "updated",
                        "profile": profile,
                        "room": self._public(room),
                    })
            except asyncio.TimeoutError:
                if run_id:
                    await self._target_rpc(profile, "chat.abort", {"runId": run_id}, 30)
                await self._member_error(
                    room_id, profile, "A group member took too long to respond.", epoch
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not run_id:
                    await self._set_member_readiness(
                        room_id, profile, "error", str(exc)[:500]
                    )
                await self._member_error(room_id, profile, str(exc)[:500], epoch)
            finally:
                self._waiters.pop((profile, run_id), None)
                if self._runs.get(room_id, {}).get(profile) == run_id:
                    self._runs[room_id].pop(profile, None)
                if (
                    self._current(room_id, epoch)
                    and profile in self._active.get(room_id, set())
                ):
                    self._finish_member(room_id, profile)
                    await self._emit({
                        "roomId": room_id,
                        "type": "run-state",
                        "profile": profile,
                        "room": self._public(room),
                    })

        try:
            await asyncio.gather(*(run_member(profile) for profile in responders))
        finally:
            if self._epochs.get(room_id) == epoch:
                pending = self._pending_attachments.get(room_id)
                if pending and pending[0] == epoch:
                    self._pending_attachments.pop(room_id, None)
                await self._settle_run(room_id)
                self._drop_live(room_id)
                if room_id in self._rooms:
                    await self._emit({
                        "roomId": room_id,
                        "type": "run-state",
                        "room": self._public(self._rooms[room_id]),
                    })

    async def _run_council(
        self,
        room_id: str,
        epoch: int,
        responders: list[str],
        directed: bool = False,
    ) -> None:
        room = self._rooms.get(room_id)
        if room is None:
            return
        run = room.get("run") if isinstance(room.get("run"), dict) else {}
        group_run_id = str(run.get("id") or uuid.uuid4())
        executor = "default" if "default" in room["members"] else responders[0]
        pending = self._pending_attachments.get(room_id)
        attachments = pending[1] if pending and pending[0] == epoch else []
        current = list(responders)
        turn_count = 0
        self._active[room_id] = set()

        async def run_member(
            profile: str,
            delta: list[dict[str, Any]],
            boundary: int,
            round_index: int,
        ) -> str:
            run_id = ""
            self._active.setdefault(room_id, set()).add(profile)
            members = run.setdefault("members", {})
            member = members.setdefault(profile, {
                "state": "queued",
                "boundary": boundary,
                "gatewayRunId": "",
                "updatedAt": _now(),
                "error": "",
            })
            member.update({
                "state": "queued",
                "boundary": boundary,
                "gatewayRunId": "",
                "updatedAt": _now(),
                "error": "",
            })
            member.pop("attention", None)
            member.pop("lateResult", None)
            await self._persist()
            try:
                params: dict[str, Any] = {
                    "sessionKey": _session_key(room_id),
                    "message": self._prompt(room, profile, delta),
                    "thinking": False,
                    "idempotencyKey": (
                        f"room-{group_run_id}-c{round_index + 1}-{turn_count + 1}-{profile}"
                    ),
                    "disabledTools": ["message_profile"] + (
                        [] if profile == executor else _GROUP_NON_EXECUTOR_DISABLED_TOOLS
                    ),
                    "turnOrigin": "group",
                }
                if attachments and round_index == 0:
                    params["attachments"] = attachments
                accepted = await self._target_rpc(profile, "chat.send", params, 60)
                run_id = str((accepted or {}).get("runId") or "")
                if not run_id:
                    raise ProfileHostError(
                        "ROOM_START_FAILED",
                        "A group member did not accept the response.",
                        retryable=True,
                    )
                await self._set_member_readiness(room_id, profile, "ready")
                self._runs.setdefault(room_id, {})[profile] = run_id
                member.update({
                    "state": "running",
                    "gatewayRunId": run_id,
                    "updatedAt": _now(),
                })
                await self._persist()
                if not self._current(room_id, epoch):
                    await self._target_rpc(profile, "chat.abort", {"runId": run_id}, 30)
                    return ""
                future = asyncio.get_running_loop().create_future()
                self._waiters[(profile, run_id)] = future
                for queued_event, queued_payload in self._early.pop((profile, run_id), []):
                    await self.handle_profile_event(profile, queued_event, queued_payload)
                terminal = await asyncio.wait_for(future, self._member_timeout)
                response = await self._commit_member_terminal(
                    room_id, profile, terminal, boundary, epoch
                )
                if self._current(room_id, epoch):
                    self._finish_member(room_id, profile)
                    await self._emit({
                        "roomId": room_id,
                        "type": "updated",
                        "profile": profile,
                        "room": self._public(room),
                    })
                return response
            except asyncio.TimeoutError:
                if run_id:
                    await self._target_rpc(profile, "chat.abort", {"runId": run_id}, 30)
                await self._member_error(
                    room_id, profile, "A group member took too long to respond.", epoch
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not run_id:
                    await self._set_member_readiness(
                        room_id, profile, "error", str(exc)[:500]
                    )
                await self._member_error(room_id, profile, str(exc)[:500], epoch)
            finally:
                self._waiters.pop((profile, run_id), None)
                if self._runs.get(room_id, {}).get(profile) == run_id:
                    self._runs[room_id].pop(profile, None)
                if (
                    self._current(room_id, epoch)
                    and profile in self._active.get(room_id, set())
                ):
                    self._finish_member(room_id, profile)
            return ""

        try:
            for round_index in range(_MAX_COUNCIL_ROUNDS):
                if not current or turn_count >= _MAX_COUNCIL_TURNS:
                    break
                run["round"] = round_index + 1
                await self._persist()
                next_profiles: set[str] = set()
                for profile in current:
                    if turn_count >= _MAX_COUNCIL_TURNS or not self._current(room_id, epoch):
                        break
                    start = max(0, int(room["watermarks"].get(profile, 0)))
                    boundary = len(room["messages"])
                    delta = [
                        message for message in room["messages"][start:]
                        if message.get("role") != "assistant"
                        or message.get("profile") != profile
                    ][-_MAX_CONTEXT_MESSAGES:]
                    if not delta:
                        continue
                    response = await run_member(profile, delta, boundary, round_index)
                    turn_count += 1
                    run["turnCount"] = turn_count
                    if response:
                        if not directed:
                            next_profiles.add(profile)
                        _everyone, mentions = _parse_mentions(response, room["members"])
                        next_profiles.update(mentions)
                current = [
                    profile for profile in room["members"] if profile in next_profiles
                ]
        finally:
            if self._epochs.get(room_id) == epoch:
                pending = self._pending_attachments.get(room_id)
                if pending and pending[0] == epoch:
                    self._pending_attachments.pop(room_id, None)
                await self._settle_run(room_id)
                self._drop_live(room_id)
                if room_id in self._rooms:
                    await self._emit({
                        "roomId": room_id,
                        "type": "run-state",
                        "room": self._public(self._rooms[room_id]),
                    })

    async def _commit_member_terminal(
        self,
        room_id: str,
        profile: str,
        terminal: dict[str, Any],
        boundary: int,
        epoch: int,
    ) -> str:
        if not self._current(room_id, epoch):
            return ""
        room = self._rooms[room_id]
        member = self._run_member(room, profile)
        response = _final_text(terminal)
        attachments, created_media = await self._import_terminal_attachments(
            room_id, profile, terminal
        )
        if not self._current(room_id, epoch):
            for path in created_media:
                path.unlink(missing_ok=True)
            return ""
        substantive_text = bool(response) and not re.fullmatch(
            r"\(?\s*pass\s*\)?\.?", response, re.IGNORECASE
        )
        substantive = substantive_text or bool(attachments)
        message: dict[str, Any] | None = None
        if substantive:
            calls = self._tool_calls(room_id, profile)
            message = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "profile": profile,
                "content": response,
                "createdAt": _now(),
            }
            if attachments:
                message["attachments"] = attachments
            if calls:
                message["toolCalls"] = calls
                message["tools"] = [call["name"] for call in calls]
            self._append_message(room, message)
            self._trim(room)
        elif created_media:
            for path in created_media:
                path.unlink(missing_ok=True)
        # Delivery is acknowledged only after a terminal response has been
        # incorporated. A timeout/crash therefore leaves the old watermark
        # intact and the missed context is retried.
        delivered_boundary = int((member or {}).get("boundary", boundary))
        room["watermarks"][profile] = min(
            delivered_boundary, len(room["messages"])
        )
        if member is not None:
            member.update({
                "state": "completed",
                "updatedAt": _now(),
                "error": "",
            })
            member.pop("attention", None)
        room["updatedAt"] = _now()
        try:
            await self._persist()
        except Exception:
            if message is not None and message in room["messages"]:
                room["messages"].remove(message)
            for path in created_media:
                path.unlink(missing_ok=True)
            raise
        return response if substantive else ""

    async def _set_member_readiness(
        self,
        room_id: str,
        profile: str,
        state: str,
        error: str = "",
    ) -> None:
        room = self._rooms.get(room_id)
        if room is None or profile not in room["members"]:
            return
        self._readiness.setdefault(room_id, {})[profile] = {
            "state": state,
            "updatedAt": _now(),
            "error": (error or "")[:500],
        }
        await self._emit({
            "roomId": room_id,
            "type": "readiness",
            "profile": profile,
            "room": self._public(room),
        })

    async def _member_error(
        self,
        room_id: str,
        profile: str,
        message: str,
        epoch: int,
    ) -> None:
        if self._current(room_id, epoch):
            room = self._rooms.get(room_id)
            member = self._run_member(room, profile) if room else None
            if member is not None:
                member.update({
                    "state": "failed",
                    "updatedAt": _now(),
                    "error": (message or "A group member could not respond.")[:500],
                })
                member.pop("attention", None)
                try:
                    await self._persist()
                except Exception:
                    logger.exception("Could not persist failed group member state")
            self._finish_member(room_id, profile)
            await self._emit({
                "roomId": room_id,
                "type": "error",
                "profile": profile,
                "error": message or "A group member could not respond.",
                **({"room": self._public(room)} if room else {}),
            })

    async def _record_late_terminal(
        self,
        room_id: str,
        profile: str,
        run_id: str,
        state: str,
    ) -> None:
        room = self._rooms.get(room_id)
        member = self._run_member(room, profile) if room else None
        if (
            member is None
            or not run_id
            or member.get("gatewayRunId") != run_id
            or member.get("state") not in {"failed", "aborted", "stranded"}
        ):
            return
        member["lateResult"] = {
            "state": state,
            "receivedAt": _now(),
            "disposition": "discarded",
        }
        await self._persist()
        await self._emit({
            "roomId": room_id,
            "type": "late-result",
            "profile": profile,
            "room": self._public(room),
        })

    @staticmethod
    def _run_member(room: dict[str, Any], profile: str) -> dict[str, Any] | None:
        run = room.get("run")
        members = run.get("members") if isinstance(run, dict) else None
        member = members.get(profile) if isinstance(members, dict) else None
        return member if isinstance(member, dict) else None

    async def _settle_run(self, room_id: str) -> None:
        room = self._rooms.get(room_id)
        if room is None:
            return
        run = room.get("run")
        members = run.get("members") if isinstance(run, dict) else None
        if not isinstance(members, dict) or run.get("state") != "running":
            return
        states = {
            str(member.get("state") or "failed")
            for member in members.values()
            if isinstance(member, dict)
        }
        if states & {"queued", "running", "needs_user"}:
            return
        run["state"] = (
            "completed" if states <= {"completed"}
            else "aborted" if states <= {"aborted"}
            else "partial"
        )
        run["finishedAt"] = _now()
        room["updatedAt"] = run["finishedAt"]
        await self._persist()

    def _finish_member(self, room_id: str, profile: str) -> None:
        active = self._active.get(room_id)
        if active is not None:
            active.discard(profile)
            if not active:
                self._active.pop(room_id, None)
        self._streams.get(room_id, {}).pop(profile, None)
        activities = self._activities.get(room_id, {})
        self._activities[room_id] = {
            key: value for key, value in activities.items() if key[0] != profile
        }
        attentions = self._attentions.get(room_id, {})
        self._attentions[room_id] = {
            key: value for key, value in attentions.items()
            if value["profile"] != profile
        }

    def _checkpoint_aborted_messages(self, room_id: str, finished_at: str) -> None:
        """Persist each accepted member's visible prefix before clearing live state.

        This mirrors direct chat's abort contract: a stopped assistant turn is
        still a durable timeline event, tool history stays attached, and the
        delivery watermark advances only for members that actually accepted the
        turn.  The method is synchronous so no stream frame can interleave with
        the snapshot on the event loop.
        """
        room = self._rooms[room_id]
        run = room.get("run") if isinstance(room.get("run"), dict) else {}
        members = run.get("members") if isinstance(run, dict) else {}
        started_at = str(run.get("startedAt") or "") if isinstance(run, dict) else ""
        duration_ms = self._elapsed_ms(started_at, finished_at)
        accepted = set(self._runs.get(room_id, {}))
        streams = self._streams.get(room_id, {})

        for profile in room["members"]:
            member = members.get(profile) if isinstance(members, dict) else None
            gateway_run_id = str(member.get("gatewayRunId") or "") if isinstance(member, dict) else ""
            if profile not in accepted and not gateway_run_id:
                continue
            content = str(streams.get(profile) or "")
            run_id = self._runs.get(room_id, {}).get(profile, "")
            waiter = self._waiters.get((profile, run_id)) if run_id else None
            if not content and waiter is not None and waiter.done() and not waiter.cancelled():
                # A final frame may have arrived but not yet reached the
                # durable commit. Preserve its authoritative text when Stop
                # wins that race. Failed/aborted futures raise here and simply
                # fall back to the visible stream prefix.
                try:
                    terminal = waiter.result()
                except Exception:
                    terminal = None
                if isinstance(terminal, dict):
                    content = _final_text(terminal)
            calls = self._tool_calls(room_id, profile)
            message: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "profile": profile,
                "content": content,
                "createdAt": finished_at,
                "aborted": True,
            }
            if duration_ms is not None:
                message["durationMs"] = duration_ms
            if calls:
                message["toolCalls"] = calls
                message["tools"] = [call["name"] for call in calls]
            self._append_message(room, message)
            boundary = int(member.get("boundary", len(room["messages"]))) if isinstance(member, dict) else len(room["messages"])
            room["watermarks"][profile] = min(boundary, len(room["messages"]))

        self._trim(room)

    @staticmethod
    def _elapsed_ms(started_at: str, finished_at: str) -> int | None:
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return None
        return max(0, min(int((finished - started).total_seconds() * 1000), 2_147_483_647))

    def _current(self, room_id: str, epoch: int) -> bool:
        return (
            not self._closed
            and room_id in self._rooms
            and self._epochs.get(room_id) == epoch
        )

    def _prompt(
        self,
        room: dict[str, Any],
        profile: str,
        messages: list[dict[str, Any]],
    ) -> str:
        transcript = "\n\n".join(
            f"{'User' if item['role'] == 'user' else item.get('profile', 'Member')}: "
            f"{item['content']}{self._attachment_note(item)}"
            for item in messages
        )
        if room.get("mode") == "council":
            return "\n".join((
                f"You are {profile}, participating in the local group \"{room['title']}\".",
                "This is a bounded, sequential council. You can see contributions made before your turn.",
                "Add only new, useful information. If you have nothing material to add, reply exactly (pass).",
                "You may mention another visible group member by @name when their next-round review is genuinely useful.",
                "Mention @user only when the user must decide or clarify something.",
                "Never expose private conversations or claim that profile directories are shared.",
                "",
                "New council messages:",
                transcript or "(No new text.)",
            ))
        return "\n".join((
            f"You are {profile}, participating in the local group \"{room['title']}\".",
            "This is one independent response in a shared group conversation. Other selected members may be composing responses to the same user turn in parallel.",
            "Reply to the group directly and conversationally. Build on the visible history, contribute your own useful perspective, and avoid restating points already present.",
            "Mention @user only when you genuinely need the user to decide or clarify something. Always return one real response for a turn you were selected for.",
            "Never expose private conversations or claim that profile directories are shared.",
            "",
            "New group messages:",
            transcript or "(No new text.)",
        ))

    def _definition(self, title: Any, members: Any) -> tuple[str, list[str]]:
        clean_title = _bounded_text(title, label="Group name", maximum=80)
        if not isinstance(members, list):
            raise ProfileHostError("INVALID_PARAMS", "Group members are invalid.")
        clean_members: list[str] = []
        for value in members:
            member = _bounded_text(value, label="Group member", maximum=64)
            if not _PROFILE_RE.fullmatch(member):
                raise ProfileHostError("INVALID_PARAMS", "Group members are invalid.")
            if member not in clean_members:
                clean_members.append(member)
        if len(clean_members) < 2:
            raise ProfileHostError("INVALID_PARAMS", "Choose at least two agents for a group.")
        if len(clean_members) > _MAX_MEMBERS:
            raise ProfileHostError(
                "INVALID_PARAMS", f"A group can include at most {_MAX_MEMBERS} agents."
            )
        available = set(self._profile_directory())
        if any(member not in available for member in clean_members):
            raise ProfileHostError(
                "PROFILE_NOT_FOUND", "One of the selected agents no longer exists."
            )
        return clean_title, clean_members

    @staticmethod
    def _room_mode(value: Any, *, default: str = "panel") -> str:
        if value is None:
            return default
        if not isinstance(value, str) or value not in {"panel", "council"}:
            raise ProfileHostError(
                "INVALID_PARAMS", "Group mode must be panel or council."
            )
        return value

    @staticmethod
    def _attachment_note(message: dict[str, Any]) -> str:
        attachments = message.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return ""
        names = [
            str(item.get("fileName"))
            for item in attachments
            if isinstance(item, dict) and item.get("fileName")
        ]
        return f"\n[Attachments: {', '.join(names)}]" if names else ""

    def _import_room(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProfileHostError("INVALID_PARAMS", "An imported group is invalid.")
        room_id = _room_id(value.get("id"))
        title, members = self._definition(value.get("title"), value.get("members"))
        messages = value.get("messages")
        if not isinstance(messages, list) or len(messages) > _MAX_MESSAGES:
            raise ProfileHostError("INVALID_PARAMS", "Imported group messages are invalid.")
        try:
            copied_messages = json.loads(json.dumps(messages, ensure_ascii=False))
            self._validate_messages(copied_messages, members)
            raw_watermarks = value.get("watermarks")
            if raw_watermarks is not None and not isinstance(raw_watermarks, dict):
                raise ValueError("invalid watermarks")
            source = raw_watermarks if isinstance(raw_watermarks, dict) else {}
            watermarks = {
                member: max(
                    0,
                    min(int(source.get(member, len(copied_messages))), len(copied_messages)),
                )
                for member in members
            }
            created_at = self._timestamp(value.get("createdAt"))
            updated_at = self._timestamp(value.get("updatedAt"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProfileHostError("INVALID_PARAMS", "An imported group is invalid.") from exc
        imported = {
            "id": room_id,
            "title": title,
            "members": members,
            "mode": self._room_mode(value.get("mode")),
            "messages": copied_messages,
            "watermarks": watermarks,
            "createdAt": created_at,
            "updatedAt": updated_at,
        }
        # An import carries positions, not sequences. Position is a correct
        # total order for the snapshot being imported, so it seeds the room's
        # sequence space.
        assign_message_sequences(imported)
        return imported

    @staticmethod
    def _contains_import(current: dict[str, Any], imported: dict[str, Any]) -> bool:
        """Accept an idempotent retry, including a destination that advanced."""
        if current.get("createdAt") != imported.get("createdAt"):
            return False
        current_messages = current.get("messages", [])
        imported_messages = imported.get("messages", [])
        if len(current_messages) < len(imported_messages):
            return False
        # Sequences are assigned by the destination, so a retry against a room
        # that has since trimmed its window carries different numbers for the
        # same content. Identity here is the message itself, not where it sits
        # in this room's sequence space.
        def without_seq(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {key: value for key, value in message.items() if key != "seq"}
                for message in messages
            ]

        return (
            without_seq(current_messages[:len(imported_messages)])
            == without_seq(imported_messages)
        )

    def _public(
        self,
        room: dict[str, Any],
        *,
        include_messages: bool = True,
    ) -> dict[str, Any]:
        room_id = room["id"]
        active = self._active.get(room_id, set())
        active_profiles = [profile for profile in room["members"] if profile in active]
        activities = [
            dict(value)
            for value in self._activities.get(room_id, {}).values()
            if value["profile"] in active
        ]
        activities.sort(key=lambda value: (value["startedAt"], value["id"]))
        attentions = [
            {
                **{key: value for key, value in attention.items() if key != "request"},
                "request": dict(attention["request"]),
            }
            for attention in self._attentions.get(room_id, {}).values()
        ]
        messages = room["messages"]
        result = {
            "id": room_id,
            "title": room["title"],
            "members": list(room["members"]),
            "mode": str(room.get("mode") or "panel"),
            # How many messages the group HAS, which is no longer how many it
            # is carrying: the window bounds residency, not history. A client
            # that conflated the two would report a group as shorter than it
            # is the moment it outgrew the window.
            "messageCount": int(room.get("trimmedCount", 0)) + len(messages),
            "latestMessage": (
                self._message_summary(messages[-1]) if messages else None
            ),
            "createdAt": room["createdAt"],
            "updatedAt": room["updatedAt"],
            "running": bool(active_profiles) or self._room_is_running(room),
            "activeProfiles": active_profiles,
            "streams": {
                profile: text
                for profile, text in self._streams.get(room_id, {}).items()
                if profile in active and text
            },
            "activities": activities,
            "needsUser": bool(attentions) or self._needs_user(room),
            "attentions": attentions,
            "runState": self._public_run(room),
            "readiness": {
                profile: {
                    "state": str(value.get("state") or "idle"),
                    "updatedAt": str(value.get("updatedAt") or ""),
                    "error": str(value.get("error") or ""),
                }
                for profile in room["members"]
                for value in [self._readiness.get(room_id, {}).get(profile, {
                    "state": "idle", "updatedAt": "", "error": "",
                })]
            },
        }
        if include_messages:
            result["messages"] = json.loads(json.dumps(messages))
        return result

    @staticmethod
    def _message_summary(message: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded identity/preview, never attachment or tool payloads."""
        summary: dict[str, Any] = {
            "id": message["id"],
            "role": message["role"],
            "content": str(message.get("content") or "")[:_MAX_SUMMARY_CONTENT_CHARS],
            "createdAt": message["createdAt"],
        }
        if message.get("role") == "assistant":
            summary["profile"] = message["profile"]
            if message.get("aborted") is True:
                summary["aborted"] = True
            if isinstance(message.get("durationMs"), int):
                summary["durationMs"] = message["durationMs"]
        return summary

    @staticmethod
    def _public_run(room: dict[str, Any]) -> dict[str, Any] | None:
        run = room.get("run")
        members = run.get("members") if isinstance(run, dict) else None
        if not isinstance(members, dict):
            return None
        return {
            "id": str(run.get("id") or ""),
            "mode": str(run.get("mode") or room.get("mode") or "panel"),
            "state": str(run.get("state") or ""),
            "startedAt": str(run.get("startedAt") or ""),
            "finishedAt": str(run.get("finishedAt") or ""),
            "round": int(run.get("round") or 0),
            "turnCount": int(run.get("turnCount") or 0),
            "members": {
                profile: {
                    "state": str(member.get("state") or ""),
                    "updatedAt": str(member.get("updatedAt") or ""),
                    "error": str(member.get("error") or ""),
                    **(
                        {"attention": json.loads(json.dumps(member["attention"]))}
                        if isinstance(member.get("attention"), dict)
                        else {}
                    ),
                    **(
                        {"lateResult": json.loads(json.dumps(member["lateResult"]))}
                        if isinstance(member.get("lateResult"), dict)
                        else {}
                    ),
                }
                for profile, member in members.items()
                if isinstance(profile, str) and isinstance(member, dict)
            },
        }

    @staticmethod
    def _needs_user(room: dict[str, Any]) -> bool:
        start = next((
            index for index in range(len(room["messages"]) - 1, -1, -1)
            if room["messages"][index]["role"] == "user"
        ), -1)
        return any(
            message["role"] == "assistant" and "@user" in message["content"].lower()
            for message in room["messages"][start + 1:]
        )

    @staticmethod
    def _room_is_running(room: dict[str, Any]) -> bool:
        run = room.get("run")
        return isinstance(run, dict) and run.get("state") == "running"

    def _require(self, room_id: str) -> dict[str, Any]:
        room = self._rooms.get(room_id)
        if room is None:
            raise ProfileHostError("ROOM_NOT_FOUND", "This group no longer exists.")
        return room

    def _attention(self, room_id: str, raw_id: Any, kind: str) -> dict[str, Any]:
        request_id = _bounded_text(raw_id, label="Request identity", maximum=256)
        request = self._attentions.get(room_id, {}).get(request_id)
        if request is None or request["kind"] != kind:
            raise ProfileHostError(
                "ROOM_REQUEST_CLOSED", "This group request is no longer pending."
            )
        return request

    async def _clear_attention(self, room_id: str, request_id: str) -> None:
        attention = self._attentions.get(room_id, {}).pop(request_id, None)
        room = self._rooms.get(room_id)
        if room:
            if isinstance(attention, dict):
                member = self._run_member(room, str(attention.get("profile") or ""))
                if member is not None:
                    member["state"] = "running"
                    member["updatedAt"] = _now()
                    member.pop("attention", None)
                    await self._persist()
            await self._emit({
                "roomId": room_id, "type": "attention", "room": self._public(room),
            })

    @staticmethod
    def _safe_request(payload: dict[str, Any]) -> dict[str, Any]:
        allowed = ("id", "command", "question", "choices", "expiresAt", "sessionKey")
        result: dict[str, Any] = {}
        for key in allowed:
            value = payload.get(key)
            if isinstance(value, str):
                result[key] = value[:32_000]
            elif isinstance(value, (int, float, bool)):
                result[key] = value
            elif key == "choices" and isinstance(value, list):
                result[key] = [
                    str(item)[:500] for item in value[:20] if isinstance(item, str)
                ]
        return result

    def _update_activity(
        self,
        room_id: str,
        profile: str,
        value: dict[str, Any],
    ) -> bool:
        phase = value.get("phase")
        call_id = str(value.get("toolCallId") or "")[:256]
        name = str(value.get("name") or "")[:128]
        if phase not in {"start", "result"} or not call_id:
            return False
        key = (profile, call_id)
        existing = self._activities.setdefault(room_id, {}).get(key)
        if not name and existing:
            name = existing["toolName"]
        if not name:
            return False
        arguments = (
            _project_arguments(name, value.get("argumentsJson", value.get("args")))
            if phase == "start"
            else (existing or {}).get("argumentsJson", "{}")
        )
        next_value = {
            "id": call_id,
            "profile": profile,
            "toolName": name,
            "argumentsJson": arguments,
            "state": "completed"
            if phase == "result" or (existing or {}).get("state") == "completed"
            else "running",
            "startedAt": (existing or {}).get("startedAt", int(time.time() * 1000)),
        }
        if existing == next_value:
            return False
        self._activities[room_id][key] = next_value
        return True

    def _iteration_activities(
        self,
        room_id: str,
        profile: str,
        payload: dict[str, Any],
    ) -> bool:
        changed = False
        if payload.get("role") == "assistant" and isinstance(payload.get("tool_calls"), list):
            for raw in payload["tool_calls"][:_MAX_TOOL_CALLS]:
                if not isinstance(raw, dict):
                    continue
                function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
                changed = self._update_activity(room_id, profile, {
                    "phase": "start",
                    "toolCallId": raw.get("id"),
                    "name": function.get("name"),
                    "argumentsJson": function.get("arguments"),
                }) or changed
        elif payload.get("role") == "tool":
            changed = self._update_activity(room_id, profile, {
                "phase": "result",
                "toolCallId": payload.get("tool_call_id"),
                "name": payload.get("name"),
            }) or changed
        return changed

    def _tool_calls(self, room_id: str, profile: str) -> list[dict[str, Any]]:
        values = [
            value
            for (owner, _), value in self._activities.get(room_id, {}).items()
            if owner == profile
        ]
        values.sort(key=lambda value: (value["startedAt"], value["id"]))
        return [
            {
                "id": value["id"],
                "name": value["toolName"],
                "argumentsJson": value["argumentsJson"],
            }
            for value in values[-_MAX_TOOL_CALLS:]
        ]

    async def _emit_live(self, room_id: str, event_type: str, profile: str) -> None:
        if room_id in self._live_publish_tasks:
            return

        async def publish() -> None:
            try:
                await asyncio.sleep(0.05)
                if room_id not in self._rooms:
                    return
                await self._emit({
                    "roomId": room_id,
                    "type": event_type,
                    "profile": profile,
                    "patch": self._live_patch(room_id),
                })
            finally:
                self._live_publish_tasks.pop(room_id, None)

        task = asyncio.create_task(publish(), name=f"profile-room-publish:{room_id}")
        self._live_publish_tasks[room_id] = task

    def _live_patch(self, room_id: str) -> dict[str, Any]:
        room = self._rooms[room_id]
        active = self._active.get(room_id, set())
        active_profiles = [profile for profile in room["members"] if profile in active]
        activities = [
            dict(value)
            for value in self._activities.get(room_id, {}).values()
            if value["profile"] in active
        ]
        activities.sort(key=lambda value: (value["startedAt"], value["id"]))
        attentions = [
            {
                **{key: value for key, value in attention.items() if key != "request"},
                "request": dict(attention["request"]),
            }
            for attention in self._attentions.get(room_id, {}).values()
        ]
        return {
            "running": bool(active_profiles) or self._room_is_running(room),
            "activeProfiles": active_profiles,
            "streams": {
                profile: text
                for profile, text in self._streams.get(room_id, {}).items()
                if profile in active and text
            },
            "activities": activities,
            "attentions": attentions,
            "needsUser": bool(attentions) or self._needs_user(room),
        }

    async def _emit(self, event: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        event = self._with_room_delta(event)
        try:
            result = self._on_event(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Profile room event callback failed")

    def _with_room_delta(self, event: dict[str, Any]) -> dict[str, Any]:
        """Attach the messages this event actually adds.

        Stripping ``messages`` from a snapshot event bounds the payload but
        leaves the receiver knowing only that *something* changed, so it has
        to ask for a page back to learn one new line — trading bytes for round
        trips during exactly the turn where latency is visible. Carrying the
        appended rows, plus the sequence range they cover, lets a receiver
        apply the change directly and fall back to a fetch only when the range
        does not meet what it already holds.
        """
        room = event.get("room")
        if not isinstance(room, dict):
            return event
        room_id = str(event.get("roomId") or room.get("id") or "")
        messages = room.get("messages")
        if not room_id or not isinstance(messages, list):
            return event
        sequences = [
            int(message["seq"])
            for message in messages
            if isinstance(message, dict) and isinstance(message.get("seq"), int)
        ]
        if len(sequences) != len(messages):
            return event
        enriched = dict(event)
        if sequences:
            enriched["windowFirstSeq"] = sequences[0]
            enriched["lastSeq"] = sequences[-1]
        # An empty room still establishes a baseline (-1), so the very first
        # message it receives arrives as a delta rather than forcing a fetch.
        established = room_id in self._emitted_seq
        previous = self._emitted_seq.get(room_id, -1)
        appended = [
            message for message, seq in zip(messages, sequences)
            if not established or seq > previous
        ]
        # A receiver that is further behind than one page is cheaper to let
        # fetch than to catch up inline; omitting the field is the signal.
        if established and len(appended) <= _MAX_HISTORY_PAGE:
            enriched["appended"] = json.loads(json.dumps(appended))
        self._emitted_seq[room_id] = sequences[-1] if sequences else previous
        return enriched

    def _drop_live(self, room_id: str) -> None:
        publisher = self._live_publish_tasks.pop(room_id, None)
        if publisher is not None:
            publisher.cancel()
        self._active.pop(room_id, None)
        self._runs.pop(room_id, None)
        self._streams.pop(room_id, None)
        self._activities.pop(room_id, None)
        self._attentions.pop(room_id, None)

    @staticmethod
    def _append_message(room: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
        """Append one durable message, stamping it with a fresh sequence.

        The sequence is the room's only stable message ordinate: it is handed
        out once, never reused, and never shifts when older messages leave the
        live window. Pagination cursors and delivery watermarks can therefore
        name a position that keeps meaning something.
        """
        message["seq"] = next_room_sequence(room)
        room["messages"].append(message)
        return message

    @staticmethod
    def _trim(room: dict[str, Any]) -> None:
        """Bound what the room keeps resident — not what it keeps.

        Messages leaving here leave the *window*: the durable store flags them
        and history pages straight back into them. Watermarks and run
        boundaries index the window, so they still shift by the same amount;
        sequence numbers do not, which is why a cursor survives this.
        """
        if len(room["messages"]) <= _MAX_MESSAGES:
            return
        removed = len(room["messages"]) - _MAX_MESSAGES
        del room["messages"][:removed]
        room["trimmedCount"] = int(room.get("trimmedCount", 0)) + removed
        room["watermarks"] = {
            key: max(0, int(value) - removed)
            for key, value in room["watermarks"].items()
        }
        run = room.get("run")
        members = run.get("members") if isinstance(run, dict) else None
        if isinstance(members, dict):
            for member in members.values():
                if isinstance(member, dict):
                    member["boundary"] = max(
                        0, int(member.get("boundary", 0)) - removed
                    )

    async def _load(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            recovered_run = False
            if self._sqlite_store.exists():
                recovered_run = await self._load_sqlite_authority()
            elif self._legacy_store_path.is_file():
                raw, parsed, recovered_run = await self._load_legacy_json()
                try:
                    await asyncio.to_thread(
                        self._sqlite_store.initialize_verified,
                        parsed,
                        legacy_digest=hashlib.sha256(raw).hexdigest(),
                    )
                    recovered_run = await self._load_sqlite_authority()
                except (OSError, RoomStoreError) as exc:
                    # A valid legacy store remains authoritative when the new
                    # database cannot be fully staged, verified and published.
                    # Never rename, truncate or delete it during migration.
                    if self._sqlite_store.exists():
                        recovered_run = await self._load_sqlite_authority()
                    else:
                        logger.warning(
                            "Room database migration could not be published; "
                            "continuing with the preserved JSON store: {}",
                            exc,
                        )
                        self._rooms = parsed
                        self._storage_mode = "legacy-json-fallback"
                        self._store_revision = 0
                        self._persisted_fingerprints = room_fingerprints(parsed)
            else:
                try:
                    await asyncio.to_thread(
                        self._sqlite_store.initialize_verified,
                        {},
                    )
                    recovered_run = await self._load_sqlite_authority()
                except (OSError, RoomStoreError) as exc:
                    raise ProfileHostError(
                        "ROOM_STORE_INVALID",
                        "The local group database could not be initialized safely.",
                    ) from exc
            self._loaded = True
            if recovered_run:
                # Commit the stranded verdict immediately so a second restart
                # does not keep presenting the interrupted turn as running.
                await self._persist()

    async def _load_legacy_json(
        self,
    ) -> tuple[bytes, dict[str, dict[str, Any]], bool]:
        try:
            raw = await asyncio.to_thread(self._legacy_store_path.read_bytes)
        except OSError as exc:
            raise ProfileHostError(
                "ROOM_STORE_INVALID",
                "The legacy group history could not be read safely.",
            ) from exc
        if len(raw) > _MAX_LEGACY_STORE_BYTES:
            raise ProfileHostError(
                "ROOM_STORE_INVALID",
                "The legacy group history is too large to migrate safely.",
            )
        try:
            value = json.loads(raw)
            rooms = value["rooms"] if value.get("version") == _STORE_VERSION else None
            parsed, recovered_run = self._parse_room_records(rooms)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProfileHostError(
                "ROOM_STORE_INVALID",
                "The legacy group history is damaged. The source file was left untouched.",
            ) from exc
        return raw, parsed, recovered_run

    async def _load_sqlite_authority(self) -> bool:
        try:
            loaded = await asyncio.to_thread(self._sqlite_store.load)
            durable_fingerprints = room_fingerprints({
                str(room["id"]): room
                for room in loaded.rooms
                if isinstance(room, dict) and isinstance(room.get("id"), str)
            })
            parsed, recovered_run = self._parse_room_records(loaded.rooms)
        except RoomStoreLimitError as exc:
            raise ProfileHostError(
                "ROOM_STORE_LIMIT",
                "The local group database reached its safe size limit.",
            ) from exc
        except (RoomStoreInvalidError, RoomStoreError, OSError) as exc:
            raise ProfileHostError(
                "ROOM_STORE_INVALID",
                "The local group database is damaged or unavailable. It was left untouched.",
            ) from exc
        except (KeyError, TypeError, ValueError, ProfileHostError) as exc:
            raise ProfileHostError(
                "ROOM_STORE_INVALID",
                "The local group database contains invalid records. It was left untouched.",
            ) from exc
        self._rooms = parsed
        self._storage_mode = "sqlite-wal"
        self._store_revision = loaded.revision
        # Keep fingerprints from the on-disk representation. Loading may
        # deliberately normalize an interrupted run to ``stranded``; that
        # difference must remain dirty so the recovery verdict is committed.
        self._persisted_fingerprints = durable_fingerprints
        return recovered_run

    def _parse_room_records(
        self,
        rooms: Any,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        if not isinstance(rooms, list) or len(rooms) > _MAX_ROOMS:
            raise ValueError("invalid room store")
        parsed: dict[str, dict[str, Any]] = {}
        recovered_run = False
        for room in rooms:
            if not isinstance(room, dict):
                raise ValueError("invalid room")
            try:
                room_id = _room_id(room.get("id"))
                title, members = self._definition(
                    room.get("title"), room.get("members")
                )
                messages = room.get("messages")
                if not isinstance(messages, list) or len(messages) > _MAX_MESSAGES:
                    raise ValueError("invalid messages")
                self._validate_messages(messages, members)
                watermarks = room.get("watermarks")
                watermarks = watermarks if isinstance(watermarks, dict) else {}
                parsed[room_id] = {
                    "id": room_id,
                    "title": title,
                    "members": members,
                    "mode": self._room_mode(room.get("mode")),
                    "messages": messages,
                    "watermarks": {
                        member: max(
                            0,
                            min(int(watermarks.get(member, 0)), len(messages)),
                        )
                        for member in members
                    },
                    "createdAt": self._timestamp(room.get("createdAt")),
                    "updatedAt": self._timestamp(room.get("updatedAt")),
                }
                durable_run = self._load_run(
                    room.get("run"), members, len(messages)
                )
            except ProfileHostError as exc:
                raise ValueError("invalid room record") from exc
            if durable_run is not None:
                parsed[room_id]["run"] = durable_run
                recovered_run = recovered_run or (
                    isinstance(room.get("run"), dict)
                    and room["run"].get("state") == "running"
                )
        return parsed, recovered_run

    def _load_run(
        self,
        value: Any,
        members: list[str],
        message_count: int,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("invalid room run")
        run_id = _room_id(value.get("id"))
        state = value.get("state")
        if state not in {"running", "completed", "partial", "aborted", "stranded"}:
            raise ValueError("invalid room run state")
        started_at = self._timestamp(value.get("startedAt"))
        mode = self._room_mode(value.get("mode"))
        try:
            round_index = max(0, min(int(value.get("round", 0)), _MAX_COUNCIL_ROUNDS))
            turn_count = max(0, min(int(value.get("turnCount", 0)), _MAX_COUNCIL_TURNS))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid room run progress") from exc
        raw_finished = value.get("finishedAt")
        finished_at = self._timestamp(raw_finished) if raw_finished else ""
        raw_members = value.get("members")
        if not isinstance(raw_members, dict) or any(key not in members for key in raw_members):
            raise ValueError("invalid room run members")
        parsed_members: dict[str, dict[str, Any]] = {}
        interrupted = state == "running"
        for profile, raw in raw_members.items():
            if not isinstance(raw, dict):
                raise ValueError("invalid room member run")
            member_state = raw.get("state")
            if member_state not in {
                "queued", "running", "needs_user", "completed", "failed",
                "aborted", "stranded",
            }:
                raise ValueError("invalid room member run state")
            try:
                boundary = max(0, min(int(raw.get("boundary", 0)), message_count))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid room member boundary") from exc
            if interrupted and member_state in {"queued", "running", "needs_user"}:
                member_state = "stranded"
            parsed_members[profile] = {
                "state": member_state,
                "boundary": boundary,
                "gatewayRunId": str(raw.get("gatewayRunId") or "")[:256],
                "updatedAt": self._timestamp(raw.get("updatedAt")),
                "error": (
                    "Flowly restarted before this group member finished. Retry the message."
                    if member_state == "stranded"
                    else str(raw.get("error") or "")[:500]
                ),
            }
            attention = raw.get("attention")
            if attention is not None:
                if (
                    not isinstance(attention, dict)
                    or attention.get("kind") not in {"approval", "clarify"}
                    or attention.get("profile") != profile
                    or not isinstance(attention.get("id"), str)
                    or not attention.get("id")
                    or len(attention["id"]) > 256
                    or not isinstance(attention.get("request"), dict)
                ):
                    raise ValueError("invalid room member attention")
                parsed_members[profile]["attention"] = {
                    "kind": attention["kind"],
                    "profile": profile,
                    "id": attention["id"],
                    "request": self._safe_request(attention["request"]),
                }
            late_result = raw.get("lateResult")
            if late_result is not None:
                if (
                    not isinstance(late_result, dict)
                    or late_result.get("state") not in {"final", "aborted", "error"}
                    or late_result.get("disposition") != "discarded"
                ):
                    raise ValueError("invalid late room result")
                parsed_members[profile]["lateResult"] = {
                    "state": late_result["state"],
                    "receivedAt": self._timestamp(late_result.get("receivedAt")),
                    "disposition": "discarded",
                }
        return {
            "id": run_id,
            "mode": mode,
            "state": "stranded" if interrupted else state,
            "startedAt": started_at,
            "finishedAt": _now() if interrupted else finished_at,
            "round": round_index,
            "turnCount": turn_count,
            "members": parsed_members,
        }

    def _validate_messages(self, messages: list[Any], members: list[str]) -> None:
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            message_id = _room_id(message.get("id"))
            role = message.get("role")
            if role not in {"user", "assistant"}:
                raise ValueError("invalid message role")
            content = message.get("content")
            if (
                not isinstance(content, str)
                or "\x00" in content
                or len(content) > MAX_PROFILE_MESSAGE_CHARS
            ):
                raise ValueError("invalid message content")
            profile = message.get("profile")
            # Historical replies remain readable after a member is removed
            # from the group. Only live membership is constrained by
            # ``members``; the durable author still needs a valid profile id.
            if (
                role == "assistant"
                and (not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile))
            ):
                raise ValueError("invalid message profile")
            if role == "user" and profile is not None:
                raise ValueError("invalid message profile")
            created_at = self._timestamp(message.get("createdAt"))
            attachments = message.get("attachments", [])
            if not isinstance(attachments, list) or len(attachments) > 10:
                raise ValueError("invalid attachments")
            clean_attachments = [_durable_attachment(attachment) for attachment in attachments]
            calls = message.get("toolCalls", [])
            if not isinstance(calls, list) or len(calls) > _MAX_TOOL_CALLS:
                raise ValueError("invalid tool calls")
            clean_calls: list[dict[str, str]] = []
            call_ids: set[str] = set()
            for call in calls:
                if not isinstance(call, dict):
                    raise ValueError("invalid tool call")
                call_id = call.get("id")
                name = call.get("name")
                arguments = call.get("argumentsJson")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or len(call_id) > 256
                    or "\x00" in call_id
                    or not isinstance(name, str)
                    or not name
                    or len(name) > 128
                    or "\x00" in name
                    or not isinstance(arguments, str)
                    or len(arguments) > 8192
                    or "\x00" in arguments
                    or call_id in call_ids
                ):
                    raise ValueError("invalid tool call")
                call_ids.add(call_id)
                try:
                    parsed_arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ValueError("invalid tool arguments") from exc
                if not isinstance(parsed_arguments, dict):
                    raise ValueError("invalid tool arguments")
                clean_calls.append({
                    "id": call_id,
                    "name": name,
                    "argumentsJson": arguments,
                })
            tools = message.get("tools", [])
            if not isinstance(tools, list) or len(tools) > _MAX_TOOL_CALLS:
                raise ValueError("invalid tools")
            if any(
                not isinstance(tool, str)
                or not tool
                or len(tool) > 128
                or "\x00" in tool
                for tool in tools
            ):
                raise ValueError("invalid tools")
            aborted = message.get("aborted", False)
            if not isinstance(aborted, bool) or (aborted and role != "assistant"):
                raise ValueError("invalid aborted message state")
            duration_ms = message.get("durationMs")
            if duration_ms is not None and (
                role != "assistant"
                or isinstance(duration_ms, bool)
                or not isinstance(duration_ms, int)
                or duration_ms < 0
                or duration_ms > 2_147_483_647
            ):
                raise ValueError("invalid message duration")
            clean: dict[str, Any] = {
                "id": message_id,
                "role": role,
                "content": content,
                "createdAt": created_at,
            }
            if role == "assistant":
                clean["profile"] = profile
            if clean_attachments:
                clean["attachments"] = clean_attachments
            if clean_calls:
                clean["toolCalls"] = clean_calls
            if tools:
                clean["tools"] = list(tools)
            if aborted:
                clean["aborted"] = True
            if duration_ms is not None:
                clean["durationMs"] = duration_ms
            normalized.append(clean)
        messages[:] = normalized

    @staticmethod
    def _timestamp(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 64:
            raise ValueError("invalid timestamp")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid timestamp") from exc
        return value

    async def _persist(self) -> None:
        async with self._write_lock:
            # Every room becomes durable through here, which makes it the one
            # place that can guarantee sequencing regardless of how the room
            # entered memory.
            for room in self._rooms.values():
                ensure_message_sequences(room)
            # Take the snapshot under the lock. Parallel member completions
            # mutate one shared room; serializing earlier could let an older
            # snapshot overwrite a newer durable response.
            snapshot = {
                room_id: json.loads(canonical_room_json(room))
                for room_id, room in self._rooms.items()
            }
            fingerprints = room_fingerprints(snapshot)
            if self._storage_mode == "legacy-json-fallback":
                encoded = (
                    json.dumps(
                        {"version": _STORE_VERSION, "rooms": list(snapshot.values())},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n"
                ).encode("utf-8")
                if len(encoded) > _MAX_LEGACY_STORE_BYTES:
                    raise ProfileHostError(
                        "ROOM_STORE_LIMIT",
                        "The legacy group history reached its safe size limit.",
                    )
                await asyncio.to_thread(self._write_legacy_bytes, encoded)
                self._persisted_fingerprints = fingerprints
                return

            changed_rooms = [
                snapshot[room_id]
                for room_id, fingerprint in fingerprints.items()
                if self._persisted_fingerprints.get(room_id) != fingerprint
            ]
            deleted_room_ids = sorted(
                set(self._persisted_fingerprints).difference(fingerprints)
            )
            try:
                revision = await asyncio.to_thread(
                    self._sqlite_store.apply,
                    changed_rooms=changed_rooms,
                    deleted_room_ids=deleted_room_ids,
                    expected_revision=self._store_revision,
                )
            except RoomStoreConflictError as exc:
                # The failed mutation is rolled back by its caller. Force the
                # next public operation to reload the winning transaction so
                # a normal retry is useful instead of conflicting forever.
                self._loaded = False
                self._rooms = {}
                self._persisted_fingerprints = {}
                self._store_revision = 0
                raise ProfileHostError(
                    "ROOM_STORE_CONFLICT",
                    "Groups changed in another Flowly process. Reload and try again.",
                    retryable=True,
                ) from exc
            except RoomStoreLimitError as exc:
                raise ProfileHostError(
                    "ROOM_STORE_LIMIT",
                    "The local group database reached its safe size limit.",
                ) from exc
            except (RoomStoreInvalidError, RoomStoreError, OSError) as exc:
                raise ProfileHostError(
                    "ROOM_STORE_INVALID",
                    "The local group database could not commit the change safely.",
                    retryable=True,
                ) from exc
            self._store_revision = revision
            self._persisted_fingerprints = fingerprints

    def _write_legacy_bytes(self, value: bytes) -> None:
        self._legacy_store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix=f".{self._legacy_store_path.name}.",
            suffix=".tmp",
            dir=self._legacy_store_path.parent,
        )
        temporary = Path(raw_path)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self._legacy_store_path)
            try:
                self._legacy_store_path.chmod(0o600)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
