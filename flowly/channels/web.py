"""Web chat channel — outbound WebSocket relay (no SSH, no password)."""

import asyncio
import base64
import io
import json
import mimetypes
import os
import ssl
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets
from loguru import logger

from flowly.gateway_logs.notable import notable

from flowly.browser_annotations import append_browser_annotation_context
from flowly.bus.events import InboundMessage as _Base
from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.agent.subagent_observation import client_event, event_version
from flowly.channels.base import BaseChannel
from flowly.config.schema import WebChannelConfig
from flowly.live_voice.events import EventAccess, current_event_access
from flowly.live_voice.relay_events import RelayOutbound, RelayRecipients
from flowly.live_voice.relay_transport import CAPABILITY as RELAY_VOICE_CAPABILITY
from flowly.live_voice.relay_transport import RelayBrowserVerifier, RelayMessage, RelayPrincipal
from flowly.profile import get_flowly_home
from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc
from flowly.profile_rooms import PROFILE_ROOM_METHODS
from flowly.render_capabilities import normalize_render_capabilities

# ─── Transport limits ──────────────────────────────────────────────────────
#
# The relay (flowly-relay/flowly-relay.ts:845) accepts up to 10 MB per WS
# frame. We bump the client side to 15 MB so we hit the relay's policy first
# (clearer error) instead of the websockets library's silent default of 1 MB.
#
# A single screenshot must fit comfortably under that limit AFTER base64
# expansion (~+33%) plus JSON envelope overhead. Targeting 800 KB for the
# raw JPEG keeps base64 around 1.1 MB — well within both relay (10 MB) and
# Anthropic vision (5 MB per image) ceilings.
_WS_MAX_SIZE = 15 * 1024 * 1024
_IMAGE_TARGET_BYTES = 800 * 1024  # 800 KB raw JPEG before base64
_IMAGE_MAX_DIMENSION = 1280  # px on the longest edge
_IMAGE_INITIAL_QUALITY = 75
_IMAGE_MIN_QUALITY = 40
_OUTBOUND_QUEUE_LIMIT = 50  # cap pending replays to avoid unbounded growth
_PROFILE_BINDING_LIMIT = 1024
_PROFILE_DIRECTORY_BINDING_LIMIT = 256
_PROFILE_BINDING_TTL_SECONDS = 6 * 60 * 60
_PROFILE_RUN_BINDING_LIMIT = 2048
_PROFILE_LONG_RUNNING_METHODS = frozenset({
    "profiles.connect",
    "profiles.configure",
    "profiles.delete.commit",
    "profiles.rpc",
    "profiles.stop",
})

LocalEventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class _AccountReplySocket:
    """Keep a delayed account RPC reply on its authenticated relay connection."""

    def __init__(self, channel, socket, principal: RelayPrincipal, expires_at: int):
        self.channel, self.socket, self.principal, self.expires_at = channel, socket, principal, expires_at
        self.recipient = channel._relay_recipients.browsers.get(principal.session_id)

    async def send(self, payload: str) -> None:
        if self.recipient is None:
            return
        async with self.channel._relay_recipients.leases.get(self.recipient).lock:
            await self._send_locked(payload)

    async def _send_locked(self, payload: str) -> None:
        channel, source = self.channel, self.principal
        if channel._ws is not self.socket:
            return
        current = channel._relay_principals.get(source.session_id)
        verifier = channel._relay_authority
        leases = channel._relay_recipients.leases
        state = leases.get(self.recipient)
        if (current is None or verifier is None or current.uid != source.uid or current.link_id != source.link_id
                or state.retired or leases.owner(state).uid != source.uid
                or verifier.now() >= min(self.expires_at, current.expires_at)):
            frame = json.loads(payload)
            if frame.get('type') != 'rpc':
                return
            payload = json.dumps({'type': 'rpc', 'id': frame.get('id', ''), 'sessionId': source.session_id,
                                  'error': {'code': 'VOICE_AUTH_REQUIRED', 'message': 'The account connection changed or expired.'}})
        else:
            frame = json.loads(payload)
            if frame.get('type') not in {'rpc', 'event'}:
                return
            # The relay checks this grant against its live JWT identity and
            # exact agent connection before routing or persistence. Always
            # stamp the originating session, regardless of a handler's fields.
            frame['sessionId'] = source.session_id
            frame['voiceDelivery'] = {'version': 1, 'linkId': source.link_id, 'userId': source.uid,
                                      'sessionId': source.session_id,
                                      'expiresAt': min(self.expires_at, current.expires_at, int(state.expires_at))}
            payload = json.dumps(frame)
        await asyncio.wait_for(self.socket.send(payload), timeout=5.0)

    def __getattr__(self, name):
        return getattr(self.socket, name)


def _build_ssl_context() -> ssl.SSLContext | None:
    """Build an SSL context using certifi's CA bundle.

    In a Nuitka-bundled binary the system CA store is not available, so
    websockets.connect() would fall back to an empty trust store and fail
    every wss:// handshake with CERTIFICATE_VERIFY_FAILED. Explicitly
    loading certifi's cacert.pem fixes this for wss connections.

    Returns None on unexpected failure so callers fall back to default.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception as exc:
        logger.warning(f"[WebChannel] Failed to build certifi SSL context: {exc}")
        return None


def _compress_image_for_transport(
    path: Path,
    *,
    max_dimension: int = _IMAGE_MAX_DIMENSION,
    target_bytes: int = _IMAGE_TARGET_BYTES,
    initial_quality: int = _IMAGE_INITIAL_QUALITY,
    min_quality: int = _IMAGE_MIN_QUALITY,
) -> tuple[bytes, str] | None:
    """Compress an image so it fits within ``target_bytes`` raw bytes.

    Strategy:
      1. If the file is already small enough, return its bytes verbatim.
      2. Otherwise open with PIL, resize to ``max_dimension`` on the longer
         edge, and re-encode as JPEG. Lower quality progressively until the
         target size is met or quality floor is hit.
      3. Always returns JPEG (any input format) because JPEG compresses far
         better than PNG/WebP for screenshot content (UI is 95% solid colour
         + sharp text — JPEG q60-70 looks identical and is 3-5× smaller).

    The defaults produce a transport-sized image (≤1280px / ≤800 KB) for the
    relay frame. Callers that only need a lightweight inline preview (the direct
    gateway's bubble thumbnail) pass a smaller ``max_dimension`` / ``target_bytes``
    and serve the full-res original separately via ``/api/media``.

    Returns ``(jpeg_bytes, "image/jpeg")`` on success or ``None`` if PIL is
    unavailable AND the file is over the cap (caller should skip it rather
    than crash the relay with a 1009 frame).
    """
    raw_size = path.stat().st_size
    mime = mimetypes.guess_type(str(path))[0] or "image/png"

    # Fast path: already small enough, no point re-encoding. (Byte budget only —
    # a small-byte file is cheap to ship as-is regardless of its pixel size.)
    if raw_size <= target_bytes:
        return path.read_bytes(), mime

    try:
        from PIL import Image  # type: ignore
    except ImportError:
        # Without PIL we can't downscale. Skip the attachment rather than
        # blow up the WebSocket. The agent will still send the text response.
        logger.warning(
            f"[WebChannel] Cannot compress {path.name} ({raw_size / 1024:.0f}KB) — "
            "Pillow not installed. Attachment dropped."
        )
        return None

    try:
        with Image.open(str(path)) as img:
            # Strip alpha — JPEG can't carry it and screenshots rarely need it.
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            w, h = img.size
            if max(w, h) > max_dimension:
                scale = max_dimension / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

            quality = initial_quality
            while True:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                size = buf.tell()
                if size <= target_bytes or quality <= min_quality:
                    return buf.getvalue(), "image/jpeg"
                # Step quality down by 10. Empirically this converges in 1-3
                # iterations for typical screenshots.
                quality = max(min_quality, quality - 10)
    except Exception as exc:
        logger.warning(f"[WebChannel] Failed to compress {path.name}: {exc}")
        return None


def _poster_b64(asset: Any) -> str | None:
    """Base64 JPEG preview for a hosted attachment, from the asset's poster.

    Video has no inline preview of its own — the relay's ``sharp`` thumbnailer
    only understands images — so the poster ffmpeg pulled at generation time is
    what a client shows before the first byte of the clip arrives. No poster
    (no ffmpeg on this host) simply means no preview, never no attachment.
    """
    poster_path = getattr(asset, "poster_path", None)
    if not poster_path:
        return None
    poster = Path(poster_path)
    if not poster.is_file():
        return None
    compressed = _compress_image_for_transport(
        poster, max_dimension=512, target_bytes=48 * 1024, initial_quality=70
    )
    if compressed is None:
        return None
    return base64.b64encode(compressed[0]).decode("ascii")


def _save_attachments(attachments: list[dict], media_dir: Path) -> list[str]:
    """Resolve attachments to a media reference list.

    Each entry is either a local file path (string) OR an HTTP(S) URL
    (string). Downstream context-building code branches on the prefix.

    Resolution order, per attachment:
      1. ``cdnUrl``: relay uploaded the file to S3 already and surfaced
         the CloudFront URL — pass it through verbatim. Keeps the bot
         off the base64 hot path entirely (videos especially).
      2. ``filePath``: native path on this same machine (desktop local
         — zero-copy).
      3. ``content``: base64-encoded payload from older clients that
         haven't moved to the upload-first flow yet. Decoded and saved
         under ``media_dir`` so the rest of the pipeline can read it
         like any other local file.
    """
    from flowly.media.authority import capture_media_access, media_visible, publish_media_bytes
    from flowly.session.ownership import SessionAccessError

    access = capture_media_access()
    media_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for att in attachments:
        # 1. cdnUrl — preferred path post-relay-upload-rewrite. Skip
        # disk entirely; the LLM provider downloads it directly.
        cdn_url = att.get("cdnUrl", "")
        if isinstance(cdn_url, str) and cdn_url.startswith(("http://", "https://")):
            paths.append(cdn_url)
            continue

        # 2. Native file path (desktop local optimisation)
        file_path = att.get("filePath", "")
        if file_path and Path(file_path).is_file():
            if not media_visible(Path(file_path)):
                raise SessionAccessError()
            paths.append(str(Path(file_path)))
            continue

        # 3. Fall back to base64 content
        content = att.get("content", "")
        if not content:
            continue
        if isinstance(content, str) and "," in content and content.startswith("data:"):
            content = content.split(",", 1)[1]
        try:
            data = base64.b64decode(content)
        except Exception:
            continue
        mime = att.get("mimeType", "")
        filename = att.get("fileName", "")
        ext = Path(filename).suffix if filename else (mimetypes.guess_extension(mime) or "")
        fpath = media_dir / f"{uuid.uuid4().hex}{ext}"
        fpath = publish_media_bytes(data, fpath, access=access)
        paths.append(str(fpath))
    return paths


class WebChannel(BaseChannel):
    """
    Web chat channel using an outbound relay WebSocket.

    The VPS connects OUTWARD to the proxy (like Telegram polls Telegram servers).
    The browser connects to the proxy with Firebase JWT — no SSH, no password.

    Protocol:
      VPS  → proxy /relay?token=<agent_jwt>  (persistent outbound connection)
      Browser → proxy /?token=<browser_jwt>  (routed through agent connection)

    Messages forwarded from proxy have a `sessionId` field identifying the browser.
    Responses sent back include the same `sessionId` so the proxy can route them.
    """

    name = "web"

    def __init__(self, config: WebChannelConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WebChannelConfig = config
        self._ws = None
        self._relay_authority: RelayBrowserVerifier | None = None
        self._relay_authority_enabled = False
        self._relay_principals: dict[str, RelayPrincipal] = {}
        self._relay_recipients = RelayRecipients()
        self._outbound_lock = asyncio.Lock()
        self._reconnect_delay = 5  # seconds
        self._max_reconnect_delay = 60
        # Track active browser sessions: sessionId → asyncio.Event (response ready)
        self._pending: dict[str, asyncio.Queue] = {}
        self._subagent_observers: dict[str, float] = {}
        self._subagent_event_versions: dict[str, int] = {}
        self._profile_subagent_observers: dict[tuple[str, str], tuple[int, float]] = {}
        self._profile_subagent_reads: dict[object, str] = {}
        # Map session_key (e.g. "web:FirestoreId") → relay session_id (browser UUID)
        self._session_key_to_relay_id: dict[str, str] = {}
        # Outbound replay queue. When a send fails (transient WS drop, frame
        # too large after retry, etc.) the serialised payload is parked here
        # so the next successful connection can flush it. Bounded to prevent
        # runaway growth on prolonged outages.
        self._outbound_queue: list[RelayOutbound] = []
        # In-flight media.fetch replies (relay-bridged playback windows).
        # Tracked only so an exception surfaces in logs instead of vanishing
        # with the task; each one is short-lived (a single ≤4 MB disk read).
        self._media_fetch_tasks: set[asyncio.Task] = set()
        # Stable cronSessionId provisioned by the relay during handshake.
        # Used as the default `to` for cron jobs with deliver=true, channel="web"
        # so bot-created crons route to the same "Scheduled Tasks" conversation
        # as desktop/web-created ones.
        self._cron_session_id: str | None = None
        # Callback invoked after every `ready` — lets gateway_cmd run
        # reconciliation (sync jobs.json → Firestore, fix stale `to` fields).
        self._on_ready: Any = None
        # Active asyncio.Tasks keyed by run_id. Populated when a
        # chat.send creates the message-processing task, drained
        # automatically via add_done_callback. ``chat.abort`` looks
        # up the task by run_id and calls ``.cancel()`` — that
        # propagates a CancelledError through the in-flight agent
        # loop's awaits (LLM stream, tool execution, …) and tears
        # everything down. Without this map the abort RPC was a
        # no-op, leaving the agent to finish its turn while the
        # user's stop button did nothing.
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        # ``chat.abort`` RPC handler invokes this with the run_id to
        # interrupt. The gateway wires it to ``agent.mark_aborted``
        # in ``cli/gateway_cmd.py`` (see set_abort_callback). When
        # the callback is missing we fall back to the legacy
        # ``task.cancel()`` path — but the latter has never actually
        # worked since the tracked task only awaits the bus publish
        # and is done by the time abort fires.
        self._abort_callback: Callable[[str], None] | None = None
        self._local_event_callback: LocalEventCallback | None = None
        # The primary gateway owns the ProfileHost; the relay channel only
        # borrows it. Browser session ids are transport-authenticated routing
        # authority and never accepted from profiles.rpc params.
        self._profile_host: Any | None = None
        self._profile_event_token: str | None = None
        self._profile_directory_sessions: dict[str, float] = {}
        # Relay sessions that have opened the group surface, with the moment
        # they last touched it. A session hears about groups only after it
        # asks about them — the same gate the gateway applies.
        self._profile_room_sessions: dict[str, float] = {}
        self._profile_room_event_modes: dict[str, str] = {}
        self._profile_room_token: str | None = None
        self._profile_conversation_sessions: dict[
            tuple[str, str], dict[str, float]
        ] = {}
        self._profile_bindings_by_relay: dict[
            str, set[tuple[str, str]]
        ] = {}
        self._profile_run_bindings: dict[
            tuple[str, str], tuple[str, float]
        ] = {}

    @property
    def chat_commands(self):
        from flowly.session.commands import ChatCommandStore

        if getattr(self, "_chat_commands", None) is None:
            self._chat_commands = ChatCommandStore(":memory:")
        return self._chat_commands

    def set_chat_commands(self, store) -> None:
        """Share the gateway's profile-scoped durable acceptance ledger."""
        previous = getattr(self, "_chat_commands", None)
        if previous is not None and previous is not store:
            previous.close()
        self._chat_commands = store

    @property
    def cron_session_id(self) -> str | None:
        """Stable cronSessionId for this server's Scheduled Tasks conversation."""
        return self._cron_session_id

    def set_on_ready(self, callback: Any) -> None:
        """Register an async callback invoked after each relay handshake."""
        self._on_ready = callback

    def set_abort_callback(self, callback: Callable[[str], None]) -> None:
        """Register a sync callback invoked by ``chat.abort`` RPCs.

        The callback receives the ``run_id`` of the turn to
        interrupt. The gateway wires it to ``agent.mark_aborted`` so
        the streaming loop can break cooperatively while preserving
        the partial text. Sync (not async) because mark_aborted is a
        cheap set update — no await needed.
        """
        self._abort_callback = callback

    def set_local_event_callback(self, callback: LocalEventCallback) -> None:
        """Mirror web-channel live events to local gateway clients.

        Relay delivery remains the source of truth for iOS/web. This optional
        callback is only for already-connected local desktop clients that want
        lightweight liveness signals without joining the relay conversation.
        """
        self._local_event_callback = callback

    def set_profile_host(self, host: Any | None) -> None:
        """Attach the primary gateway's authenticated isolated-profile host."""
        if self._profile_host is host:
            return
        if self._profile_host is not None:
            if self._profile_event_token:
                self._profile_host.unsubscribe_events(self._profile_event_token)
            if self._profile_room_token:
                self._profile_host.unsubscribe_room_events(self._profile_room_token)
        self._clear_profile_subscriptions()
        self._profile_host = host
        self._profile_event_token = (
            host.subscribe_events(self._forward_profile_event)
            if host is not None
            else None
        )
        # Groups reach the relay the same way single-bot chat already does.
        # Without this a phone could only ever ask what changed, so a reply
        # being written arrived in poll-sized steps instead of as it was
        # written.
        self._profile_room_token = (
            host.subscribe_room_events(self._forward_profile_room_event)
            if host is not None
            else None
        )

    async def _emit_local_event(self, event_name: str, data: dict[str, Any]) -> None:
        cb = self._local_event_callback
        if not cb:
            return
        try:
            result = cb(event_name, data)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.debug(f"[WebChannel] local event mirror failed: {exc}")

    @staticmethod
    def _profile_lease_owner(session_id: str) -> str:
        return f"relay:{session_id}"

    def _bind_profile_directory(self, session_id: str) -> None:
        if session_id and len(session_id) <= 256:
            now = time.monotonic()
            self._profile_directory_sessions[session_id] = now
            self._prune_profile_bindings(now)
            while (
                len(self._profile_directory_sessions)
                > _PROFILE_DIRECTORY_BINDING_LIMIT
            ):
                oldest = min(
                    self._profile_directory_sessions,
                    key=self._profile_directory_sessions.__getitem__,
                )
                self._profile_directory_sessions.pop(oldest, None)

    def _bind_profile_rooms(self, session_id: str, event_mode: Any) -> None:
        """Open the group surface for one relay session.

        Called only from an authenticated group RPC, so a session hears about
        groups exactly when it has shown an interest in them and never
        before — the gateway gates its own clients the same way.
        """
        if not session_id or len(session_id) > 256:
            return
        now = time.monotonic()
        self._profile_room_sessions[session_id] = now
        if event_mode in {"full-v1", "delta-v1"}:
            self._profile_room_event_modes[session_id] = event_mode
        self._prune_profile_bindings(now)
        while len(self._profile_room_sessions) > _PROFILE_DIRECTORY_BINDING_LIMIT:
            oldest = min(
                self._profile_room_sessions,
                key=self._profile_room_sessions.__getitem__,
            )
            self._profile_room_sessions.pop(oldest, None)
            self._profile_room_event_modes.pop(oldest, None)

    async def _forward_profile_room_event(self, envelope: dict[str, Any]) -> None:
        """Deliver one group update to the sessions watching groups."""
        if not self._profile_room_sessions:
            return
        compact: str | None = None
        verbatim = json.dumps({
            "type": "event", "event": "profile.room", "data": envelope,
        })
        for session_id in sorted(self._profile_room_sessions):
            if self._profile_room_event_modes.get(session_id) == "delta-v1":
                if compact is None:
                    compact = json.dumps({
                        "type": "event",
                        "event": "profile.room",
                        "data": self._compact_room_event(envelope),
                    })
                await self._send_or_queue(compact)
            else:
                await self._send_or_queue(verbatim)

    @staticmethod
    def _compact_room_event(envelope: dict[str, Any]) -> dict[str, Any]:
        """Drop the resident window from an update that already names its delta.

        A snapshot carries every message the room is holding, which is the
        whole live window on every reply. Clients that asked for deltas have
        the appended rows and the sequence range they cover, so the window is
        bytes they would only throw away — and over a phone connection those
        bytes are the difference between a group being live and being
        expensive.
        """
        room = envelope.get("room")
        if not isinstance(room, dict):
            return envelope
        compact_room = dict(room)
        messages = compact_room.pop("messages", None)
        if isinstance(messages, list):
            compact_room.setdefault("messageCount", len(messages))
            compact_room.setdefault(
                "latestMessage", messages[-1] if messages else None
            )
        return {**envelope, "room": compact_room}

    def _bind_profile_conversation(
        self, profile: str, session_key: str, session_id: str
    ) -> None:
        if not (
            profile
            and session_key
            and session_id
            and len(profile) <= 64
            and len(session_key) <= 256
            and len(session_id) <= 256
        ):
            return
        now = time.monotonic()
        key = (profile, session_key)
        subscribers = self._profile_conversation_sessions.setdefault(key, {})
        is_new = session_id not in subscribers
        subscribers[session_id] = now
        bindings = self._profile_bindings_by_relay.setdefault(session_id, set())
        first_default = profile == "default" and not any(
            candidate_profile == "default" for candidate_profile, _ in bindings
        )
        bindings.add(key)
        if is_new and first_default and self._profile_host is not None:
            self._profile_host.retain_default_events(
                self._profile_lease_owner(session_id)
            )
        self._prune_profile_bindings(now)

    def _unbind_profile_conversation(
        self, profile: str, session_key: str, session_id: str
    ) -> None:
        key = (profile, session_key)
        subscribers = self._profile_conversation_sessions.get(key)
        if subscribers is not None:
            subscribers.pop(session_id, None)
            if not subscribers:
                self._profile_conversation_sessions.pop(key, None)
        bindings = self._profile_bindings_by_relay.get(session_id)
        if bindings is None:
            return
        bindings.discard(key)
        if not bindings:
            self._profile_bindings_by_relay.pop(session_id, None)
        if profile == "default" and not any(
            candidate_profile == "default" for candidate_profile, _ in bindings
        ):
            if self._profile_host is not None:
                self._profile_host.release_default_events(
                    self._profile_lease_owner(session_id)
                )

    def _remove_profile_relay_session(self, session_id: str) -> None:
        self._profile_directory_sessions.pop(session_id, None)
        self._profile_room_sessions.pop(session_id, None)
        self._profile_room_event_modes.pop(session_id, None)
        for profile, session_key in tuple(
            self._profile_bindings_by_relay.get(session_id, set())
        ):
            self._unbind_profile_conversation(profile, session_key, session_id)

    def _clear_profile_subscriptions(self) -> None:
        self._clear_profile_subagent_observers()
        for session_id in tuple(self._profile_bindings_by_relay):
            self._remove_profile_relay_session(session_id)
        self._profile_directory_sessions.clear()
        self._profile_room_sessions.clear()
        self._profile_room_event_modes.clear()
        self._profile_conversation_sessions.clear()
        self._profile_bindings_by_relay.clear()
        self._profile_run_bindings.clear()

    def _prune_profile_bindings(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        cutoff = now - _PROFILE_BINDING_TTL_SECONDS
        for session_id, touched_at in tuple(self._profile_directory_sessions.items()):
            if touched_at < cutoff:
                self._profile_directory_sessions.pop(session_id, None)
        for session_id, touched_at in tuple(self._profile_room_sessions.items()):
            if touched_at < cutoff:
                self._profile_room_sessions.pop(session_id, None)
                self._profile_room_event_modes.pop(session_id, None)
        all_bindings = sorted(
            (
                (touched_at, profile, session_key, session_id)
                for (profile, session_key), subscribers
                in self._profile_conversation_sessions.items()
                for session_id, touched_at in subscribers.items()
            ),
            key=lambda item: item[0],
        )
        expired = [item for item in all_bindings if item[0] < cutoff]
        overflow = max(0, len(all_bindings) - _PROFILE_BINDING_LIMIT)
        victims = expired + [
            item for item in all_bindings[:overflow] if item not in expired
        ]
        for _touched, profile, session_key, session_id in victims:
            self._unbind_profile_conversation(profile, session_key, session_id)
        for key, (_session_key, touched_at) in tuple(self._profile_run_bindings.items()):
            if touched_at < cutoff:
                self._profile_run_bindings.pop(key, None)
        while len(self._profile_run_bindings) > _PROFILE_RUN_BINDING_LIMIT:
            oldest = min(
                self._profile_run_bindings,
                key=lambda key: self._profile_run_bindings[key][1],
            )
            self._profile_run_bindings.pop(oldest, None)

    async def _forward_profile_event(self, envelope: dict[str, Any]) -> None:
        """Route a nested profile event only to relay sessions bound to it."""
        now = time.monotonic()
        self._prune_profile_bindings(now)
        profile = str(envelope.get("profile") or "")
        event_type = str(envelope.get("type") or "")
        data = envelope.get("data")
        payload = data if isinstance(data, dict) else {}
        if event_type.startswith("subagent."):
            self._prune_profile_subagent_observers(now)
            if self._ws is None:
                return
            async def send_task(session_id: str, version: int) -> None:
                name, body = client_event(event_type, payload, version)
                frame = json.dumps({
                    "type": "event", "event": "profile.event", "sessionId": session_id,
                    "data": {**envelope, "type": name, "data": body},
                })
                pending = RelayOutbound(frame, self._outbound_event_access(frame))
                await asyncio.wait_for(self._deliver_relay_outbound(pending), timeout=1)
            await asyncio.gather(*(send_task(session, version)
                for (session, candidate), (version, _expiry) in self._profile_subagent_observers.items()
                if candidate == profile), return_exceptions=True)
            return
        targets: set[str] = set()

        if event_type == "directory":
            targets.update(self._profile_directory_sessions)
        elif event_type in {"connection", "error", "needsInput"}:
            targets.update(self._profile_directory_sessions)
            for (candidate_profile, _session_key), subscribers in (
                self._profile_conversation_sessions.items()
            ):
                if candidate_profile == profile:
                    targets.update(subscribers)

        session_key = str(payload.get("sessionKey") or "")
        if session_key:
            subscribers = self._profile_conversation_sessions.get(
                (profile, session_key), {}
            )
            targets.update(subscribers)
            for session_id in subscribers:
                subscribers[session_id] = now

        run_id = str(payload.get("runId") or "")
        if run_id and not session_key:
            bound = self._profile_run_bindings.get((profile, run_id))
            if bound is not None:
                run_session_key, _ = bound
                envelope = {**envelope, "data": {**payload, "sessionKey": run_session_key}}
                self._profile_run_bindings[(profile, run_id)] = (
                    run_session_key, now
                )
                targets.update(
                    self._profile_conversation_sessions.get(
                        (profile, run_session_key), {}
                    )
                )

        for session_id in sorted(targets):
            await self._send_or_queue(json.dumps({
                "type": "event",
                "sessionId": session_id,
                "event": "profile.event",
                "data": envelope,
            }))

        if (
            run_id
            and event_type == "chat"
            and payload.get("state") in {"final", "aborted", "error"}
        ):
            self._profile_run_bindings.pop((profile, run_id), None)

    async def _handle_profile_rpc(self, ws, msg: dict[str, Any]) -> None:
        rpc_id = str(msg.get("id") or "")
        session_id = str(msg.get("sessionId") or "")
        method = str(msg.get("method") or "")
        params = msg.get("params")
        host = self._profile_host
        if host is None:
            await ws.send(json.dumps({
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "error": {
                    "code": "PROFILE_HOST_UNAVAILABLE",
                    "message": "This agent host does not manage isolated profiles.",
                    "retryable": False,
                },
            }))
            return
        if method == "profiles.manager.claim":
            # Only the Desktop on the agent's own machine runs its agents; a
            # phone or a remote app must not tell the host to stop keeping them.
            await ws.send(json.dumps({
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "error": {
                    "code": "PROFILE_MANAGER_LOCAL_ONLY",
                    "message": "Only Flowly Desktop on this machine can manage its agents.",
                    "retryable": False,
                },
            }))
            return

        self._bind_profile_directory(session_id)
        if method in PROFILE_ROOM_METHODS:
            self._bind_profile_rooms(
                session_id,
                params.get("eventMode") if isinstance(params, dict) else None,
            )
        profile = ""
        inner_method = ""
        inner_session_key = ""
        if isinstance(params, dict) and method == "profiles.rpc":
            profile = str(params.get("name") or "").strip()
            inner_method = str(params.get("method") or "")
            inner_params = params.get("params")
            try:
                _validated_method, validated_params = validate_profile_rpc(
                    inner_method, inner_params
                )
            except ProfileHostError:
                validated_params = {}
            inner_session_key = str(validated_params.get("sessionKey") or "")
            if inner_session_key:
                # Bind before dispatch: chat.send may emit its first event
                # immediately after the acknowledgement on a fast local model.
                self._bind_profile_conversation(
                    profile, inner_session_key, session_id
                )

        # A call's memory reads arrive here from the phone. Name and outcome
        # only, so a missing read can be told from a refused one.
        voice_read = inner_method.startswith("voice.")
        if voice_read:
            logger.info("[WebChannel] voice profile RPC {} for {} arrived", inner_method, profile or "?")
        read_token = object()
        if inner_method in {"subagents.list", "subagents.get"} and session_id and len(self._profile_subagent_reads) < 128:
            self._profile_subagent_reads[read_token] = session_id
        try:
            result = await host.dispatch(method, params)
        except ProfileHostError as exc:
            if voice_read:
                logger.warning("[WebChannel] voice profile RPC {} refused: {}", inner_method, exc.code)
            await ws.send(json.dumps({
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                },
            }))
            return
        except FileNotFoundError:
            error = {"code": "PROFILE_NOT_FOUND", "message": "The agent no longer exists."}
        except FileExistsError:
            error = {"code": "PROFILE_ALREADY_EXISTS", "message": "An agent with this name already exists."}
        except ValueError as exc:
            error = {"code": "INVALID_PARAMS", "message": str(exc)[:500]}
        except Exception as exc:
            if feature_rpc.has_voice_account():
                logger.error('[WebChannel] profile RPC {} failed ({})', method, type(exc).__name__)
            else:
                logger.exception("[WebChannel] profile rpc {} failed", method)
            error = {"code": "INTERNAL", "message": "The profile operation failed."}
        else:
            if self._profile_subagent_reads.get(read_token) == session_id and session_id:
                self._observe_profile_subagents(session_id, profile, validated_params)
            if (
                inner_method in {"chat.send", "chat.inflight"}
                and inner_session_key
                and isinstance(result, dict)
            ):
                receipt = result if inner_method == "chat.send" else result.get("inflight")
                run_id = str(receipt.get("runId") or "") if isinstance(receipt, dict) else ""
                if run_id:
                    self._profile_run_bindings[(profile, run_id)] = (
                        inner_session_key, time.monotonic()
                    )
                    self._prune_profile_bindings()
            await ws.send(json.dumps({
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "result": result,
            }))
            return
        finally:
            self._profile_subagent_reads.pop(read_token, None)

        await ws.send(json.dumps({
            "type": "rpc",
            "id": rpc_id,
            "sessionId": session_id,
            "error": {**error, "retryable": False},
        }))

    def _session_key_for_relay_id(self, session_id: str) -> str:
        """Best-effort reverse lookup for relay session id → stable session key."""
        if not session_id:
            return ""
        fallback = ""
        for key, relay_id in self._session_key_to_relay_id.items():
            if relay_id != session_id:
                continue
            if key.startswith("web:"):
                return key
            fallback = fallback or key
        return fallback or f"web:{session_id}"

    async def start(self) -> None:
        """Connect outbound to relay proxy and keep reconnecting (Telegram-like)."""
        if not self.config.enabled:
            return
        if not self.config.auth_token:
            logger.error("[WebChannel] auth_token not set — cannot connect to relay")
            return
        if not self.config.server_id:
            # Try env fallback
            self.config.server_id = os.environ.get("FLOWLY_SERVER_ID", "")
        if not self.config.server_id:
            logger.error("[WebChannel] server_id not set — cannot connect to relay")
            return

        self._running = True
        delay = self._reconnect_delay

        while self._running:
            try:
                await self._connect_and_run()
                delay = self._reconnect_delay  # reset on clean disconnect
            except Exception as e:
                if not self._running:
                    break
                notable("relay.lost", delay=delay, reason=e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def stop(self) -> None:
        self._running = False
        # These tasks only publish to the agent queue. A turn already handed
        # off is owned by AgentLoop and must not be stopped with the channel.
        pending = list(self._active_tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._active_tasks.clear()
        self._clear_profile_subscriptions()
        self._subagent_observers.clear()
        self._subagent_event_versions.clear()
        self._relay_principals.clear()
        self._relay_recipients.clear()
        self._relay_authority = None
        self._relay_authority_enabled = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _media_attachments(self, msg: OutboundMessage) -> list[dict[str, Any]]:
        """Describe this turn's non-image media as Attachment V2, by media id.

        Images are deliberately left alone: they already reach the relay as
        compressed base64 content blocks, that path works, and rerouting it
        would risk a regression for the media people actually send today.

        Everything else — video above all — stays on this machine and travels
        as a ``mediaId`` plus its poster. No cloud copy: clients that reach
        this bot's gateway stream it directly, and clients that only reach the
        relay stream it THROUGH the relay, which bridges the request to this
        socket (``media.fetch``) without storing a byte. Delivery therefore
        depends on nothing but the bot being online — no account, no hosted
        storage, and nothing at rest anywhere else.
        """
        from flowly.media.assets import (
            ASSETS_META_KEY,
            KIND_IMAGE,
            assets_from_meta,
            attachment_v2,
            describe,
            index_by_path,
            kind_for_mime,
        )

        by_path = index_by_path(assets_from_meta(msg.metadata.get(ASSETS_META_KEY)))
        out: list[dict[str, Any]] = []
        for media_path in msg.media:
            if not isinstance(media_path, str) or media_path.startswith(("http://", "https://")):
                continue
            p = Path(media_path)
            if not p.is_file():
                continue
            asset = by_path.get(media_path)
            if asset is None:
                mime = mimetypes.guess_type(str(p))[0] or ""
                if kind_for_mime(mime) == KIND_IMAGE:
                    continue
                asset = describe(p, probe_media=False)
            if asset.kind == KIND_IMAGE:
                continue
            logger.info(
                f"[WebChannel] Attached {asset.kind} {p.name} "
                f"({asset.size / 1024:.0f}KB) by media id"
            )
            out.append(attachment_v2(asset, thumbnail=_poster_b64(asset), media_id=p.name))
        return out

    async def send(self, msg: OutboundMessage) -> None:
        """Send agent response back to the browser via the relay proxy.

        Three layers of resilience:
          1. Every image attachment is compressed before encoding so frames
             stay under the relay's 10 MB ceiling.
          2. If the WS is currently down or the send raises, the payload is
             parked in the outbound queue and replayed on the next connect.
          3. The connect call uses an explicit ``max_size`` matching the
             relay so we get a clear error instead of the websockets-library
             default of 1 MB silently rejecting frames.
        """
        session_id = msg.chat_id  # chat_id = sessionId for web channel
        canonical_session = msg.metadata.get('session_key')
        session_key = canonical_session if isinstance(canonical_session, str) and canonical_session else self._session_key_for_relay_id(session_id)

        progress = msg.metadata.get("tool_progress_event")
        if isinstance(progress, dict) and progress.get("state") == "tool_progress":
            data = {**progress, "sessionKey": self._session_key_for_relay_id(session_id), "source": "relay"}
            await self._send_or_queue(json.dumps({
                "type": "event", "sessionId": session_id, "event": "chat", "data": data,
            }))
            asyncio.create_task(self._emit_local_event("chat", data))
            return

        # Goal status is CHIP state, not conversation: emit the snapshot as a
        # `goal.updated` event only. Sending the explanatory text as a chat
        # final would persist a fake assistant bubble in every client.
        if msg.metadata.get("goalStatus") is True:
            goal_snapshot = msg.metadata.get("goal")
            terminal = bool(msg.metadata.get("goalTerminal")) and bool(msg.content.strip())
            if isinstance(goal_snapshot, dict):
                goal_event = {
                    "type": "event",
                    "sessionId": session_id,
                    "event": "goal.updated",
                    "data": {"sessionKey": session_key, "goal": goal_snapshot},
                }
                await self._send_or_queue(json.dumps(goal_event))
                asyncio.create_task(
                    self._emit_local_event("goal.updated", goal_event["data"])
                )
            # A goal that ENDED still reports in words; routine progress stops
            # here as chip state.
            if not terminal:
                return
            # The snapshot already went out; keep the message path from
            # appending the same revision again.
            msg.metadata.pop("goal", None)

        # Live per-iteration tool-turn event from the loop. The loop
        # emits one of these after every assistant_with_tool_calls or
        # tool_result it adds to the in-flight turn; we forward it
        # straight to the relay as a ``state:"iteration_step"`` chat
        # event so the relay can write to ``tool_turns/`` Firestore
        # LIVE (with inProgress:true) and the desktop / iOS panel
        # populates as the run progresses. Short-circuit here so we
        # don't fall through to the regular final-message path —
        # iteration events carry no chat content, only structured
        # tool-turn payloads.
        iter_event = msg.metadata.get("iteration_event")
        if isinstance(iter_event, dict) and iter_event:
            event_msg = {
                "type": "event",
                "sessionId": session_id,
                "event": "chat",
                "data": {
                    "state": "iteration_step",
                    "runId": iter_event.get("runId") or "",
                    "sessionKey": session_key,
                    "source": "relay",
                    "iterationIdx": iter_event.get("iterationIdx", 0),
                    "role": iter_event.get("role"),
                    "content": iter_event.get("content", ""),
                    **(
                        {"tool_calls": iter_event["tool_calls"]}
                        if iter_event.get("tool_calls")
                        else {}
                    ),
                    **(
                        {"tool_call_id": iter_event["tool_call_id"]}
                        if iter_event.get("tool_call_id")
                        else {}
                    ),
                    **({"name": iter_event["name"]} if iter_event.get("name") else {}),
                    **(
                        {"tool_activity": iter_event["tool_activity"]}
                        if iter_event.get("tool_activity")
                        else {}
                    ),
                    **(
                        {"goalRun": True}
                        if iter_event.get("goalRun") is True
                        else {}
                    ),
                },
            }
            await self._send_or_queue(json.dumps(event_msg))
            asyncio.create_task(self._emit_local_event("chat", event_msg["data"]))
            return

        run_id = msg.metadata.get("run_id", str(uuid.uuid4()))
        stream_run_id = msg.metadata.get("stream_run_id")

        # Build content blocks — always start with text
        content_blocks: list[dict[str, Any]] = []
        if msg.content:
            content_blocks.append({"type": "text", "text": msg.content})

        # Non-image media (video, audio) cannot ride the WS as base64: even a
        # short clip is tens of megabytes and base64 inflates it by a third. It
        # goes to hosted storage instead and travels as a URL. Done BEFORE the
        # image loop so a delivery failure is known while the payload is still
        # being assembled.
        attachments_meta = await self._media_attachments(msg)

        # Encode media files as base64 image blocks (relay uploads to S3)
        for media_path in msg.media:
            try:
                p = Path(media_path)
                if not p.is_file():
                    continue
                mime_type = mimetypes.guess_type(str(p))[0] or "image/png"
                if not mime_type.startswith("image/"):
                    # Already handled above — never a silent drop.
                    continue
                compressed = _compress_image_for_transport(p)
                if compressed is None:
                    # Pillow missing or compression blew up — drop the
                    # image rather than blow up the WS frame.
                    continue
                jpeg_bytes, jpeg_mime = compressed
                data = base64.b64encode(jpeg_bytes).decode("ascii")
                content_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": jpeg_mime,
                            "data": data,
                        },
                    }
                )
                raw_kb = p.stat().st_size / 1024
                sent_kb = len(jpeg_bytes) / 1024
                if sent_kb < raw_kb * 0.9:
                    logger.info(
                        f"[WebChannel] Attached image {p.name} "
                        f"({raw_kb:.0f}KB → {sent_kb:.0f}KB compressed)"
                    )
                else:
                    logger.info(f"[WebChannel] Attached image {p.name} ({sent_kb:.0f}KB)")
            except Exception as e:
                logger.warning(f"[WebChannel] Failed to attach media {media_path}: {e}")

        # Ensure at least one content block
        if not content_blocks:
            content_blocks.append({"type": "text", "text": ""})

        # Send final chat event (browser MoltbotClient listens for this).
        #
        # We also include ``usage`` and ``model`` on the top-level
        # ``data`` object so native clients (desktop / iOS) can update
        # their conversation Firestore doc with per-turn token counts
        # and the effective model. This drives the context-window
        # indicator in the composer (last-turn prompt_tokens ÷
        # modelContextLength = fill %) without adding a separate
        # round-trip to ask the backend what happened.
        #
        # Missing fields default to sensible empties so older clients
        # that don't know about ``usage`` or ``model`` keep working.
        data_block: dict[str, Any] = {
            "state": "final",
            "runId": run_id,
            "sessionKey": session_key,
            "source": "relay",
            "message": {
                "content": content_blocks,
            },
        }
        goal_snapshot = msg.metadata.get("goal")
        if isinstance(goal_snapshot, dict):
            data_block["goal"] = goal_snapshot
        if stream_run_id:
            # ``runId`` is the durable assistant-message identity used by the
            # relay's Firestore document. ``streamRunId`` is the chat.send
            # lifecycle identity used by deltas, Stop, tool turns, and client
            # streaming state. They must remain distinct: chat.send's
            # idempotency key also identifies the durable USER document.
            data_block["streamRunId"] = str(stream_run_id)
        if msg.metadata.get("goal_run") is True:
            data_block["goalRun"] = True
        if msg.metadata.get("goalTerminal") is True:
            data_block["goalNotice"] = True
        usage_meta = msg.metadata.get("usage")
        if isinstance(usage_meta, dict) and usage_meta:
            data_block["usage"] = {
                "prompt_tokens": int(usage_meta.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage_meta.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage_meta.get("total_tokens", 0) or 0),
                "cache_read_tokens": int(usage_meta.get("cache_read_tokens", 0) or 0),
                "cache_write_tokens": int(usage_meta.get("cache_write_tokens", 0) or 0),
            }
        model_meta = msg.metadata.get("model")
        if model_meta:
            data_block["model"] = str(model_meta)
        # ``usage`` above is the RAW provider dialect: ``prompt_tokens`` is the
        # full input on OpenAI-compatible providers but only the uncached
        # remainder on native Anthropic. Dividing it by a context length is
        # therefore wrong for whichever dialect the client didn't assume. These
        # two fields are the agent loop's already-normalized answer — occupancy
        # and the ceiling for the provider that actually ran the turn. Omitted
        # when unknown, so a client's own fallback still runs.
        context_tokens_meta = msg.metadata.get("contextTokens")
        if isinstance(context_tokens_meta, int) and context_tokens_meta > 0:
            data_block["contextTokens"] = context_tokens_meta
        context_window_meta = msg.metadata.get("contextWindow")
        if isinstance(context_window_meta, int) and context_window_meta > 0:
            data_block["contextWindow"] = context_window_meta
        context_stale_meta = msg.metadata.get("contextTokensStale")
        if isinstance(context_stale_meta, bool):
            data_block["contextTokensStale"] = context_stale_meta
        context_source_meta = msg.metadata.get("contextTokensSource")
        if context_source_meta in {
            "provider_usage",
            "last_provider_usage",
            "unavailable",
        }:
            data_block["contextTokensSource"] = context_source_meta
        context_measured_at_meta = msg.metadata.get("contextTokensMeasuredAt")
        if isinstance(context_measured_at_meta, str) and context_measured_at_meta:
            data_block["contextTokensMeasuredAt"] = context_measured_at_meta

        # Attachment V2 for hosted media. The relay persists these onto the
        # assistant message and forwards them on the live event, so a clip shows
        # up in the same bubble as the text instead of arriving as nothing at
        # all. Omitted entirely when the turn produced no non-image media, so an
        # older relay sees precisely the wire shape it already handles.
        if attachments_meta:
            data_block["attachments"] = attachments_meta

        # Tool turn messages — the assistant_with_tool_calls / tool_result
        # entries the loop appended during this turn. The relay writes
        # each one to a separate ``tool_turns/`` Firestore subcollection
        # so chat history rendering can surface every tool call as its
        # own collapsible card. Mirrors ChatGPT's "Used the X tool"
        # blocks alongside the final reply.
        #
        # Backward compat: this field is OMITTED when the agent didn't
        # produce any tool turns. Old relays / old desktops that don't
        # know about ``toolMessages`` ignore it entirely; old bots
        # never set it, so nothing changes for them. The new
        # ``tool_turns/`` subcollection is invisible to old clients
        # that only query ``messages/`` — they keep seeing the
        # single-doc final message exactly as before.
        tool_messages = msg.metadata.get("tool_messages")
        if isinstance(tool_messages, list) and tool_messages:
            data_block["toolMessages"] = tool_messages

        # ``aborted`` propagates through to the relay → Firestore →
        # client UI so a turn that was stopped mid-flight can be
        # rendered with an [Aborted] marker instead of looking like
        # a normal short reply. The ``state`` field stays as
        # ``"final"`` here — this IS the final WS event for the
        # turn — but the boolean lets the client distinguish a
        # voluntary brief answer from a user-interrupted one. Only
        # emitted when truthy so older relays / clients that don't
        # know the field see exactly the same wire shape as before.
        if msg.metadata.get("aborted"):
            data_block["aborted"] = True
        if isinstance(msg.metadata.get("error"), dict):
            # The relay may persist the user-facing error response, but must
            # not promote it to a successful assistant completion or send an
            # unread push notification.
            data_block["failed"] = True
        duration_ms = msg.metadata.get("duration_ms")
        if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
            data_block["durationMs"] = max(0, int(duration_ms))

        self.chat_commands.settle(
            data_block["sessionKey"], str(stream_run_id or run_id),
            "aborted" if data_block.get("aborted") else
            "error" if data_block.get("failed") else "completed",
        )
        event_msg = {
            "type": "event",
            "sessionId": session_id,
            "event": "chat",
            "data": data_block,
        }

        payload = json.dumps(event_msg)
        await self._send_or_queue(payload)
        asyncio.create_task(self._emit_local_event("chat", data_block))
        if isinstance(goal_snapshot, dict):
            goal_event = {
                "type": "event",
                "sessionId": session_id,
                "event": "goal.updated",
                "data": {
                    "sessionKey": data_block["sessionKey"],
                    "goal": goal_snapshot,
                },
            }
            await self._send_or_queue(json.dumps(goal_event))
            asyncio.create_task(self._emit_local_event("goal.updated", goal_event["data"]))

    async def send_cron_register(self, job: dict) -> None:
        """Push a bot-created cron job to Firestore via relay.

        `job` must be a dict with keys:
          name (str), message (str), schedule (dict with type + value), channel (str)
        """
        payload = json.dumps({"type": "cron.register", "job": job})
        await self._send_or_queue(payload)

    async def send_cron_unregister(self, name: str) -> None:
        """Remove a bot-created cron task from Firestore via relay."""
        payload = json.dumps({"type": "cron.unregister", "name": name})
        await self._send_or_queue(payload)

    def _outbound_event_access(self, payload: str) -> EventAccess:
        """Capture authority before a frame leaves its producer or enters replay."""
        from flowly.live_voice.authority import VoiceAuthorityError, current_request_owner
        from flowly.session.ownership import SessionAccessError

        access = current_event_access()
        if access is not None:
            return access
        try:
            frame = json.loads(payload)
            data = frame.get('data')
            data = data if isinstance(data, dict) else {}
            directory = None
            if frame.get('event') == 'profile.event':
                profile = data.get('profile')
                if profile and profile != 'default':
                    from flowly.profile import describe_profile

                    directory = describe_profile(profile).path / 'sessions'
                data = data.get('data') if isinstance(data.get('data'), dict) else {}
            key = data.get('sessionKey') or data.get('session_key')
            if not key and frame.get('sessionId'):
                key = self._session_key_for_relay_id(frame['sessionId'])
            run_id = data.get('runId')
            if run_id and directory is None:
                from flowly.agent import inflight

                scopes = tuple(scope for scope in (self.chat_commands.control_scope(run_id),
                                                    inflight.control_scope(run_id)) if scope is not None)
                if scopes:
                    if any(key and scope.key != key for scope in scopes):
                        return EventAccess(blocked=True)
                    return EventAccess(scopes=scopes)
            if str(frame.get('event', '')).startswith('artifact.'):
                from flowly.artifacts.store import get_store

                scope = get_store().control_scope(data.get('id'))
                return EventAccess(scopes=(scope,)) if scope is not None else EventAccess(blocked=True)
            access = EventAccess.capture(key, sessions_dir=directory)
            # A reader without original authority cannot attribute historical
            # private content to whoever happens to own the session now.
            if access.producer().uid is not None and current_request_owner() is None:
                return EventAccess(blocked=True)
            return access
        except (ValueError, TypeError, OSError, SessionAccessError, VoiceAuthorityError):
            return EventAccess(blocked=True)

    async def _deliver_relay_outbound(self, pending: RelayOutbound) -> bool:
        """True means delivered or permanently denied; False awaits a connection/lease."""
        from flowly.live_voice.authority import VoiceAuthorityError

        try:
            owner = pending.access.producer()
        except VoiceAuthorityError:
            return True
        if not pending.access.permits(owner):
            return True
        socket = self._ws
        if socket is None:
            return False
        if owner.uid is None:
            await asyncio.wait_for(socket.send(pending.payload), timeout=5.0)
            return True
        verifier = self._relay_authority
        if verifier is None or not self._relay_authority_enabled:
            return False
        frame = json.loads(pending.payload)
        if frame.get('type') not in {'rpc', 'event'}:
            return True
        target = frame.get('sessionId')
        targets = {target} if target else set(self._relay_principals)
        if target and target not in self._relay_principals:
            data = frame.get('data') or {}
            if frame.get('event') == 'profile.event':
                inner = data.get('data') or {}
                targets = set(self._profile_conversation_sessions.get((data.get('profile'), inner.get('sessionKey')), {}))
            else:
                key = data.get('sessionKey') or data.get('session_key')
                replacement = self._relay_id_for(key) if key else None
                targets = {replacement} if replacement else set()
        self._relay_recipients.synchronize(self._relay_principals)
        pending.delivered.intersection_update((identity.link_id, session_id)
                                              for session_id, identity in self._relay_principals.items())
        delivered = False
        for session_id in sorted(targets):
            recipient = self._relay_recipients.browsers.get(session_id)
            if recipient is None:
                continue
            state = self._relay_recipients.leases.get(recipient)
            async with state.lock:
                identity = self._relay_principals.get(session_id)
                if (self._ws is not socket or identity is None or state.retired
                        or identity.uid != owner.uid or identity.link_id != verifier.link_id
                        or identity.expires_at <= verifier.now()
                        or self._relay_recipients.leases.owner(state) != owner
                        or not pending.access.permits(owner)):
                    continue
                destination = (identity.link_id, session_id)
                if destination not in pending.delivered:
                    public = {**frame, 'sessionId': session_id, 'voiceDelivery': {
                        'version': 1, 'linkId': identity.link_id, 'userId': owner.uid, 'sessionId': session_id,
                        'expiresAt': min(int(state.expires_at), identity.expires_at)}}
                    await asyncio.wait_for(socket.send(json.dumps(public)), timeout=5.0)
                    pending.delivered.add(destination)
                delivered = True
        return delivered

    async def _send_or_queue(self, payload: str) -> None:
        pending = RelayOutbound(payload, self._outbound_event_access(payload))
        async with self._outbound_lock:
            try:
                if await self._deliver_relay_outbound(pending):
                    return
            except asyncio.CancelledError:
                self._enqueue_payload(pending)
                raise
            except Exception as error:
                logger.warning('[WebChannel] Outbound delivery deferred ({})', type(error).__name__)
            self._enqueue_payload(pending)

    def _enqueue_payload(self, pending: RelayOutbound) -> None:
        if len(self._outbound_queue) >= _OUTBOUND_QUEUE_LIMIT:
            self._outbound_queue.pop(0)
            logger.warning('[WebChannel] Outbound queue full; oldest event discarded')
        self._outbound_queue.append(pending)

    async def _flush_outbound_queue(self) -> None:
        """Replay original authority, without capturing the reconnecting account."""
        async with self._outbound_lock:
            pending, self._outbound_queue = self._outbound_queue, []
            for index, item in enumerate(pending):
                try:
                    if not await self._deliver_relay_outbound(item):
                        self._enqueue_payload(item)
                except BaseException as error:
                    for remaining in pending[index:]:
                        self._enqueue_payload(remaining)
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    if not isinstance(error, Exception):
                        raise
                    logger.warning('[WebChannel] Outbound replay deferred ({})', type(error).__name__)
                    return

    def _relay_id_for(self, session_key: str) -> str | None:
        """Map a bot session key to the relay session id (browser UUID)."""
        relay_id = self._session_key_to_relay_id.get(session_key)
        if not relay_id:
            # Try with web: prefix
            relay_id = self._session_key_to_relay_id.get(f"web:{session_key}")
        return relay_id

    async def send_approval_event(self, session_key: str, pending) -> None:
        """Push exec approval request to the browser/iOS via relay."""
        if not self._ws:
            return

        relay_id = self._relay_id_for(session_key)
        if not relay_id:
            logger.warning(f"[WebChannel] No relay session found for {session_key}")
            return

        from flowly.exec.wire import approval_to_wire

        event_msg = {
            "type": "event",
            "sessionId": relay_id,
            "event": "exec.approval.requested",
            "data": approval_to_wire(pending),
        }
        # Approval requests are user-blocking — must survive a flapping WS.
        await self._send_or_queue(json.dumps(event_msg))
        logger.info(f"[WebChannel] Sent approval event {pending.id} to relay session {relay_id}")

    async def send_approval_closed(
        self, session_key: str, approval_id: str, reason: str,
    ) -> None:
        """Tell relay clients an approval stopped waiting.

        Relay-connected surfaces (iOS, a cloud-connected desktop) previously
        never learned that a request died: only the local gateway broadcast a
        close. A card decided on one device therefore stayed on screen on every
        other one.
        """
        if not self._ws:
            return

        relay_id = self._relay_id_for(session_key)
        if not relay_id:
            return

        from flowly.exec.wire import approval_closed_to_wire

        event_msg = {
            "type": "event",
            "sessionId": relay_id,
            "event": "exec.approval.closed",
            "data": approval_closed_to_wire(approval_id, reason, session_key),
        }
        # Retiring a prompt matters as much as raising it — queue on a flap.
        await self._send_or_queue(json.dumps(event_msg))

    async def send_clarify_event(self, session_key: str, pending) -> None:
        """Push an agent clarify question to the browser/iOS via relay."""
        if not self._ws:
            return

        relay_id = self._relay_id_for(session_key)
        if not relay_id:
            logger.warning(f"[WebChannel] No relay session found for {session_key}")
            return

        from flowly.clarify.wire import clarify_to_wire

        event_msg = {
            "type": "event",
            "sessionId": relay_id,
            "event": "agent.clarify.requested",
            "data": clarify_to_wire(pending),
        }
        # Clarify questions are user-blocking — must survive a flapping WS.
        await self._send_or_queue(json.dumps(event_msg))
        logger.info(f"[WebChannel] Sent clarify event {pending.id} to relay session {relay_id}")

    async def send_clarify_closed(
        self, session_key: str, clarify_id: str, reason: str,
    ) -> None:
        """Tell relay clients a question stopped waiting (``answered`` / ``timeout``).

        The gateway has broadcast this for a while; the relay path never did, so
        a question answered on the phone left a dead prompt on every other
        relay-connected surface.
        """
        if not self._ws:
            return

        relay_id = self._relay_id_for(session_key)
        if not relay_id:
            return

        from flowly.clarify.wire import clarify_closed_to_wire

        event_msg = {
            "type": "event",
            "sessionId": relay_id,
            "event": "agent.clarify.closed",
            "data": clarify_closed_to_wire(clarify_id, reason, session_key),
        }
        # Retiring a prompt matters as much as raising it — queue on a flap.
        await self._send_or_queue(json.dumps(event_msg))

    async def send_plan_event(
        self,
        session_key: str,
        event_name: str,
        data: dict,
    ) -> None:
        """Push a plan.* event to the browser/iOS via relay, scoped to the
        conversation (routed by ``sessionKey`` → relay session id). Mirrors
        ``send_clarify_event`` so only the devices in that chat receive it.

        NOTE (relay fan-out): the bot uses one relay id as the event envelope's
        origin, but the relay routes plan events by the payload's ``sessionKey``
        to every owner-scoped subscriber viewing that conversation.
        ``plan.get`` remains the canonical reconnect/catch-up source.
        """
        if not self._ws or not session_key:
            return
        relay_id = self._session_key_to_relay_id.get(session_key)
        if not relay_id:
            relay_id = self._session_key_to_relay_id.get(f"web:{session_key}")
        if not relay_id:
            logger.debug(f"[WebChannel] No relay session for plan event {session_key}")
            return
        event_msg = {
            "type": "event",
            "sessionId": relay_id,
            "event": event_name,
            "data": data,
        }
        # Plan approvals are user-blocking — survive a flapping WS via the queue.
        await self._send_or_queue(json.dumps(event_msg))

    async def send_compaction_event(
        self,
        session_key: str,
        tokens_before: int,
        tokens_after: int,
        messages_removed: int,
        phase: str = "completed",
        compaction_id: str = "",
    ) -> None:
        """Notify the browser/iOS that context is being compacted or was compacted.

        On ``phase="completed"`` this event is ALSO what makes the relay write
        the transcript's context-boundary row. The bot used to publish that row
        itself as an ordinary reply carrying ``[context-optimized]`` — which
        every consumer of turn terminals then had to recognise by its text to
        avoid settling a turn that was still streaming. Keyed off a typed event
        instead, no reply-shaped message is involved and nothing has to guess.
        """
        if not self._ws:
            return

        relay_id = self._session_key_to_relay_id.get(session_key)
        if not relay_id:
            relay_id = self._session_key_to_relay_id.get(f"web:{session_key}")
        if not relay_id:
            return

        event_msg = {
            "type": "event",
            "sessionId": relay_id,
            "event": "compaction",
            "data": {
                "phase": phase,
                "tokensBefore": tokens_before,
                "tokensAfter": tokens_after,
                "messagesRemoved": messages_removed,
                # The relay routes conversation-scoped events by the payload's
                # sessionKey (same as plan.*), falling back to the origin
                # session id. Sending it means every device viewing this chat
                # gets the event, not just the socket that happened to be the
                # envelope's origin.
                "sessionKey": session_key,
                # Identity of this compaction cycle: correlates started with
                # completed, and gives the relay a stable document id for the
                # boundary row so a duplicate event cannot draw two dividers.
                **({"compactionId": compaction_id} if compaction_id else {}),
            },
        }
        try:
            await self._send_or_queue(json.dumps(event_msg))
        except Exception as e:
            logger.debug(f"[WebChannel] Failed to send compaction event: {e}")

    async def send_title_event(self, session_key: str, title: str) -> None:
        """Push a bot-generated session title to the relay.

        The relay owns conversation-title encryption (it holds the DEK), so it
        can't be done client-side for encrypted chats. The relay's
        ``conversation.title`` handler encrypts this and writes it onto the
        conversation doc — the same path a manual rename takes. No-ops for
        non-relay sessions (gateway), which have no relay session mapping.
        """
        if not self._ws or not title:
            return

        relay_id = self._session_key_to_relay_id.get(session_key)
        if not relay_id:
            relay_id = self._session_key_to_relay_id.get(f"web:{session_key}")
        if not relay_id:
            return

        event_msg = {
            "type": "conversation.title",
            "sessionId": relay_id,
            "title": title,
        }
        try:
            await self._send_or_queue(json.dumps(event_msg))
        except Exception as e:
            logger.debug(f"[WebChannel] Failed to send title event: {e}")

    async def _connect_and_run(self) -> None:
        """Open one WebSocket connection to the relay proxy and process messages."""
        import time

        import jwt

        jwt_secret = os.environ.get("MOLTBOT_PROXY_JWT_SECRET", "")
        if not jwt_secret or jwt_secret == "flowly-moltbot-proxy-secret-change-in-production":
            # Use jwt_secret from config, fallback to auth_token
            jwt_secret = self.config.jwt_secret or self.config.auth_token or ""
        if not jwt_secret:
            logger.warning(
                "[WebChannel] No JWT secret configured — set MOLTBOT_PROXY_JWT_SECRET env var"
            )

        # Build agent JWT
        now = int(time.time())
        payload = {
            "type": "agent",
            "serverId": self.config.server_id,
            "gatewayAuthToken": self.config.auth_token,
            "iat": now,
            "exp": now + 3600 * 24,  # 24h — long-lived agent token
            "iss": "flowly",
            "aud": "moltbot-proxy",
        }
        token = jwt.encode(payload, jwt_secret, algorithm="HS256")
        url = f"{self.config.relay_url}?token={token}"

        logger.info(f"[WebChannel] Connecting to relay: {self.config.relay_url}")

        ssl_ctx = _build_ssl_context() if self.config.relay_url.startswith("wss://") else None
        async with websockets.connect(
            url,
            ping_interval=30,
            ping_timeout=10,
            ssl=ssl_ctx,
            # Match the relay's policy (flowly-relay.ts:845 = 10 MB) with a
            # small headroom so the relay rejects oversized frames first
            # (clearer error path) instead of the client failing silently
            # with the websockets-library 1 MB default.
            max_size=_WS_MAX_SIZE,
        ) as ws:
            self._subagent_observers.clear()
            self._subagent_event_versions.clear()
            self._clear_profile_subagent_observers()
            self._ws = ws
            self._relay_authority = None
            self._relay_authority_enabled = False
            self._relay_principals.clear()
            self._relay_recipients.clear()
            notable("relay.connected")

            # Replay anything that piled up while disconnected. Done before
            # entering the recv loop so a fresh inbound message can't race
            # against a stale outbound one.
            await self._flush_outbound_queue()

            long_rpc_tasks: set[asyncio.Task[None]] = set()

            def _long_rpc_done(task: asyncio.Task[None]) -> None:
                long_rpc_tasks.discard(task)
                if task.cancelled():
                    return
                exc = task.exception()
                if exc is not None:
                    logger.warning(
                        "[WebChannel] background feature RPC failed: %s",
                        type(exc).__name__,
                    )

            try:
                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        msg = self._decode_relay_message(ws, json.loads(raw))
                        if msg is None:
                            continue
                        if (
                            msg.get("type") == "rpc"
                            and (
                                msg.get("method") in feature_rpc.LONG_RUNNING_METHODS
                                or msg.get("method") in _PROFILE_LONG_RUNNING_METHODS
                            )
                        ):
                            # Keep receiving relay pings and other sessions'
                            # traffic while a browser OAuth flow is pending.
                            task = asyncio.create_task(
                                self._handle_relay_message(ws, msg),
                                name=f"relay-feature-rpc-{msg.get('method')}",
                            )
                            long_rpc_tasks.add(task)
                            task.add_done_callback(_long_rpc_done)
                        else:
                            await self._handle_relay_message(ws, msg)
                    except json.JSONDecodeError:
                        logger.warning("[WebChannel] Invalid JSON from relay")
                    except Exception as e:
                        logger.error(f"[WebChannel] Error handling relay message: {e}")
            finally:
                pending_long_rpcs = tuple(long_rpc_tasks)
                for task in pending_long_rpcs:
                    task.cancel()
                if pending_long_rpcs:
                    await asyncio.gather(*pending_long_rpcs, return_exceptions=True)
                if self._ws is ws:
                    self._clear_profile_subscriptions()
                    self._subagent_observers.clear()
                    self._subagent_event_versions.clear()
                    self._relay_principals.clear()
                    self._relay_recipients.clear()
                    self._relay_authority = None
                    self._relay_authority_enabled = False
                    self._ws = None

    async def _serve_media_fetch(self, ws, msg: dict) -> None:
        """Answer one relay-bridged media window request.

        Stateless by design: read the requested window, send ONE
        ``media.result`` frame, forget. The relay paces playback by simply not
        asking for the next window until this one reached the client, so there
        is no stream state here to leak when a socket drops mid-clip.

        Every failure is answered, not just logged — the relay is holding a
        client's HTTP request open and needs something to end it with.
        """
        request_id = str(msg.get("requestId") or "")
        if not request_id:
            return  # nothing to correlate a reply to
        reply: dict[str, Any] = {"type": "media.result", "requestId": request_id}
        try:
            from flowly.media.serving import read_media_window

            def _read():
                from flowly.live_voice.authority import HOST_OWNER, request_owner_scope

                offset = msg.get("offset")
                length = msg.get("length")
                # This legacy transport has no signed account certificate.
                # It must not inherit an internal worker's private authority.
                with request_owner_scope(HOST_OWNER):
                    return read_media_window(
                        str(msg.get("mediaId") or ""),
                        offset=int(offset) if isinstance(offset, (int, float)) else 0,
                        length=int(length) if isinstance(length, (int, float)) else 0,
                    )

            window = await asyncio.to_thread(_read)
            if not window.ok:
                reply.update({"ok": False, "error": window.error})
            else:
                reply.update({
                    "ok": True,
                    "size": window.size,
                    "mimeType": window.mime_type,
                    "eof": window.eof,
                })
                if window.data:
                    reply["data"] = base64.b64encode(window.data).decode("ascii")
        except Exception as exc:  # noqa: BLE001 - the relay must get an answer
            logger.warning(f"[WebChannel] media.fetch failed: {exc}")
            reply.update({"ok": False, "error": "internal"})
        try:
            await ws.send(json.dumps(reply))
        except Exception as exc:  # noqa: BLE001 - socket may have dropped
            logger.debug(f"[WebChannel] media.result send failed: {exc}")

    def _decode_relay_message(self, ws, msg: object) -> dict | None:
        """Verify in receive order, before long RPCs run concurrently."""
        from flowly.live_voice.authority import VoiceAuthorityError

        if isinstance(msg, RelayMessage):
            return msg
        if not isinstance(msg, dict) or not isinstance(msg.get('type'), str):
            return None
        if msg.get('type') == 'relay.browser':
            if self._ws is not ws or self._relay_authority is None or not self._relay_authority_enabled:
                return None
            try:
                verified = self._relay_authority.verify(msg)
            except VoiceAuthorityError:
                logger.warning('[WebChannel] Relay browser identity could not be verified')
                return None
            identity = verified.principal
            if verified.kind == 'disconnected':
                self._relay_principals.pop(identity.session_id, None)
            else:
                self._relay_principals = {key: value for key, value in self._relay_principals.items()
                                          if value.expires_at > self._relay_authority.now()}
                if identity.session_id not in self._relay_principals and len(self._relay_principals) >= _PROFILE_BINDING_LIMIT:
                    self._relay_principals.pop(next(iter(self._relay_principals)))
                self._relay_principals[identity.session_id] = identity
            self._relay_recipients.synchronize(self._relay_principals)
            if verified.kind == 'request':
                params = verified.get('params')
                if (verified.get('method') in {'voice.events.bind', 'voice.events.clear'}
                        or isinstance(params, dict) and 'voiceAccess' in params):
                    # Reserve receive order before asynchronous certificate
                    # verification; a slow old bind cannot undo a later clear.
                    verified.recipient_binding = self._relay_recipients.begin(identity)
            return verified
        # Old relays stamp sessionId onto every browser-forwarded frame. It
        # therefore cannot impersonate an agent-only handshake or media call.
        if 'sessionId' in msg and msg.get('type') in {'ready', 'relay.authority.enabled', 'media.fetch'}:
            return None
        if self._relay_authority_enabled and msg.get('type') in {'rpc', 'browser-connected', 'browser-disconnected'}:
            return None
        return msg

    async def _handle_relay_message(self, ws, msg: dict) -> None:
        """Handle a message forwarded by the relay proxy."""
        msg = self._decode_relay_message(ws, msg)
        if msg is None:
            return
        msg_type = msg.get("type")

        if msg_type == "ready":
            if (self._ws is ws and self._relay_authority is None and isinstance(msg.get('capabilities'), list)
                    and RELAY_VOICE_CAPABILITY in msg['capabilities']):
                from flowly.live_voice.authority import VoiceAuthorityError

                try:
                    self._relay_authority = RelayBrowserVerifier(msg.get('relayAuthority'), server_id=self.config.server_id)
                except VoiceAuthorityError:
                    logger.warning('[WebChannel] Relay identity handshake could not be verified')
                    return
                await ws.send(json.dumps({'type': 'relay.authority.enable', 'version': 1,
                                          'linkId': self._relay_authority.link_id}))
            cron_session_id = msg.get("cronSessionId")
            if cron_session_id:
                self._cron_session_id = cron_session_id
                logger.debug(
                    f"[WebChannel] Relay confirmed agent ready — cronSessionId={cron_session_id[:8]}"
                )
            else:
                logger.debug("[WebChannel] Relay confirmed agent ready (no cronSessionId)")
            if self._on_ready:
                try:
                    result = self._on_ready()
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception as e:
                    logger.warning(f"[WebChannel] on_ready callback failed: {e}")

        elif msg_type == 'relay.authority.enabled':
            if (self._ws is ws and self._relay_authority is not None and type(msg.get('version')) is int and msg['version'] == 1
                    and msg.get('linkId') == self._relay_authority.link_id):
                self._relay_authority_enabled = True

        elif msg_type == "browser-connected":
            session_id = msg.get("sessionId", "")
            logger.debug(f"[WebChannel] Browser connected: {session_id}")

        elif msg_type == "browser-disconnected":
            session_id = msg.get("sessionId", "")
            logger.debug(f"[WebChannel] Browser disconnected: {session_id}")
            self._pending.pop(session_id, None)
            self._subagent_observers.pop(session_id, None)
            self._subagent_event_versions.pop(session_id, None)
            self._clear_profile_subagent_observers(session_id)
            self._remove_profile_relay_session(session_id)

        elif msg_type == "rpc":
            await self._handle_rpc(ws, msg)

        elif msg_type == "ping":
            session_id = msg.get("sessionId")
            pong = {"type": "pong", "timestamp": msg.get("timestamp")}
            if session_id:
                pong["sessionId"] = session_id
            await ws.send(json.dumps(pong))

        elif msg_type == "media.fetch":
            # The relay is bridging a client's playback request to this bot.
            # Served in a task so a slow disk can't stall the receive loop —
            # pings and unrelated RPCs must keep flowing while we read.
            task = asyncio.create_task(self._serve_media_fetch(ws, msg))
            self._media_fetch_tasks.add(task)
            task.add_done_callback(self._media_fetch_tasks.discard)

        elif msg_type in ("cron.registered", "cron.unregistered"):
            # Relay ACKs the cron.register / cron.unregister we sent — no
            # action needed, the write to Firestore is relay-side. We log
            # at debug so failures (if relay ever switches to emitting
            # cron.register.failed etc.) are still surfaced by the
            # unhandled branch below.
            job_name = msg.get("job", {}).get("name") or msg.get("name") or "?"
            logger.debug(f"[WebChannel] Relay {msg_type}: '{job_name}' synced to Firestore")

        else:
            logger.debug(f"[WebChannel] Unhandled relay message type: {msg_type}")

    async def _handle_rpc(self, ws, msg: dict) -> None:
        from flowly.live_voice.authority import request_owner_scope
        from flowly.live_voice.events import event_access_scope
        from flowly.session.ownership import SessionAccessError, require_rpc_session

        binding = getattr(msg, 'recipient_binding', None)
        lease_method = msg.get('method') in {'voice.events.bind', 'voice.events.clear'}
        raw_socket = ws
        try:
            if 'voiceAuthority' in msg:
                raise feature_rpc.FeatureRpcError('VOICE_AUTH_REQUIRED', 'Profile authority is not accepted over relay.')
            source = msg.principal if isinstance(msg, RelayMessage) else None
            original_params = msg.get('params') or {}
            if isinstance(original_params, dict) and 'voiceAccess' in original_params and source is None:
                raise feature_rpc.FeatureRpcError('VOICE_AUTH_UNAVAILABLE', 'Update the relay before opening account-owned voice work.')
            owner, params, certificate = await feature_rpc.resolve_voice_access(original_params)
            if owner.uid is not None:
                if source is None or source.uid != owner.uid or self._ws is not ws:
                    raise feature_rpc.FeatureRpcError('VOICE_AUTH_REQUIRED', 'The relay account does not match this request.')
                current = self._relay_principals.get(source.session_id)
                if (current is None or self._relay_authority is None or current.uid != owner.uid
                        or current.link_id != source.link_id or current.expires_at <= self._relay_authority.now()):
                    raise feature_rpc.FeatureRpcError('VOICE_AUTH_REQUIRED', 'The account connection changed or expired.')
            if lease_method and (binding is None or params or (msg['method'] == 'voice.events.bind' and certificate is None)):
                raise feature_rpc.FeatureRpcError('VOICE_AUTH_REQUIRED', 'A verified account is required for this event lease.')
            if binding is not None and not await self._relay_recipients.bind(
                    binding, None if msg.get('method') == 'voice.events.clear' else certificate):
                raise feature_rpc.FeatureRpcError('VOICE_AUTH_REQUIRED', 'The account connection changed or expired.')
            if owner.uid is not None and msg.get('method') != 'voice.events.clear':
                ws = _AccountReplySocket(self, ws, source, certificate.expires_at)
        except feature_rpc.FeatureRpcError as error:
            if binding is not None:
                await self._relay_recipients.bind(binding, None)
            await ws.send(json.dumps({'type': 'rpc', 'id': msg.get('id', ''), 'sessionId': msg.get('sessionId', ''),
                                      'error': {'code': error.code, 'message': error.message}}))
            return
        if lease_method:
            result = ({'cleared': True} if msg['method'] == 'voice.events.clear'
                      else {'bound': True, 'expiresAt': certificate.expires_at})
            await ws.send(json.dumps({'type': 'rpc', 'id': msg.get('id', ''), 'sessionId': msg.get('sessionId', ''), 'result': result}))
            await self._flush_outbound_queue()
            return
        with request_owner_scope(owner), event_access_scope(None):
            try:
                require_rpc_session(msg.get('method'), params)
                await self._dispatch_rpc(ws, {**msg, 'params': params})
            except (feature_rpc.FeatureRpcError, SessionAccessError) as error:
                await ws.send(json.dumps({'type': 'rpc', 'id': msg.get('id', ''), 'sessionId': msg.get('sessionId', ''),
                                          'error': {'code': error.code, 'message': error.message}}))
            except Exception as error:
                if not feature_rpc.has_voice_account():
                    raise
                # Keep private exception text and traceback locals out of the
                # outer relay listener after this account scope has unwound.
                logger.error('[WebChannel] account RPC {} failed ({})', msg.get('method'), type(error).__name__)
                await ws.send(json.dumps({'type': 'rpc', 'id': msg.get('id', ''), 'sessionId': msg.get('sessionId', ''),
                                          'error': {'code': 'INTERNAL', 'message': 'The request could not be completed.'}}))
        if binding is not None and self._ws is raw_socket:
            await self._flush_outbound_queue()

    async def _dispatch_rpc(self, ws, msg: dict) -> None:
        """Handle an RPC call from the browser (forwarded by proxy)."""
        method = msg.get("method", "")
        rpc_id = msg.get("id", "")
        params = msg.get("params", {})
        session_id = msg.get("sessionId", "")

        if method == "chat.send":
            from flowly.session.commands import validate_chat_target
            try:
                validate_chat_target(params)
            except ValueError as exc:
                await ws.send(json.dumps({
                    "type": "rpc", "id": rpc_id, "sessionId": session_id,
                    "error": {"code": "TASK_TARGET_CHANGED", "message": str(exc)},
                }))
                return
            queued = params.get('queueForNextTurn', False)
            queue_error = None
            if type(queued) is not bool:
                queue_error = {'code': 'INVALID_REQUEST', 'message': 'queueForNextTurn must be a boolean'}
            elif queued and getattr(self, 'supports_turn_start', False) is not True:
                queue_error = {'code': 'CHAT_QUEUE_UNAVAILABLE', 'message': 'This runtime does not support queued chat turns.'}
            if queue_error is not None:
                await ws.send(json.dumps({'type': 'rpc', 'id': rpc_id, 'sessionId': session_id, 'error': queue_error}))
                return
            message_text = params.get("message", "")
            # A stable sessionKey (the chat document id, not the
            # short-lived WebSocket session_id) is what keeps the same
            # conversation's history together across reconnects,
            # browser refreshes, and tab re-entries. If the client
            # omits it, fall back to the WS id but LOG the lapse so
            # session-drift bugs surface in the operator log instead
            # of only in chat weirdness (e.g. "bot doesn't remember
            # my first message" after a page refresh — two jsonl
            # files were actually created).
            session_key = params.get("sessionKey") or f"web:{session_id}"
            if isinstance(session_key, str) and session_key.startswith("desktop:voice:"):
                await ws.send(json.dumps({
                    "type": "rpc", "id": rpc_id, "sessionId": session_id,
                    "error": {"code": "VOICE_TRANSCRIPT_ONLY", "message": "Resume voice to continue this conversation."},
                }))
                return
            if not params.get("sessionKey"):
                logger.warning(
                    "[WebChannel] chat.send without sessionKey; using "
                    f"ws_id={session_id[:8]} as session_key. Conversation "
                    "history will fragment across reconnects. Client "
                    "should send a stable sessionKey (chat document id)."
                )
            idempotency_key = params.get("idempotencyKey") or str(uuid.uuid4())
            from flowly.session.commands import ChatCommandConflictError

            try:
                created, receipt = self.chat_commands.accept(session_key, idempotency_key, params)
            except (ChatCommandConflictError, ValueError, TypeError) as exc:
                code = "IDEMPOTENCY_CONFLICT" if isinstance(exc, ChatCommandConflictError) else "INVALID_REQUEST"
                await ws.send(json.dumps({
                    "type": "rpc", "id": rpc_id, "sessionId": session_id,
                    "error": {"code": code, "message": str(exc)},
                }))
                return
            if not created:
                self._session_key_to_relay_id[session_key] = session_id
                await ws.send(json.dumps({
                    "type": "rpc", "id": rpc_id, "sessionId": session_id,
                    "result": receipt,
                }))
                return

            # Optional per-session runtime cwd (Desktop sends the project
            # folder the user opened in the right-rail). Pin it before
            # the run so exec / codex tools resolve to it via
            # session_key. Omit → falls through to FLOWLY_CWD / config /
            # workspace. Invalid → RPC error rather than silently running
            # in the wrong place. Same shape gateway/server.py already
            # supports (see _ws_rpc_chat_send).
            cwd = params.get("cwd")
            if cwd:
                from flowly.runtime_cwd import set_session_cwd

                try:
                    set_session_cwd(session_key, cwd)
                    logger.debug(
                        f"[WebChannel] chat.send pinned cwd={cwd} for session={session_key}"
                    )
                except ValueError:
                    # Defensive: a client may ship a path that doesn't
                    # exist on THIS bot's filesystem — most obvious case
                    # is an older desktop client (or another channel) that
                    # doesn't yet gate cwd by bot kind, sending a local
                    # path to a remote VPS bot.  Hard-rejecting the whole
                    # chat.send would lose the user's message; drop the
                    # cwd silently and fall through the resolve_runtime_cwd
                    # chain (FLOWLY_CWD / config / workspace).
                    logger.warning(
                        f"[WebChannel] chat.send cwd={cwd!r} not a valid "
                        f"directory on this host; ignoring and falling "
                        f"back to workspace (session={session_key})"
                    )

            voice_mode = bool(params.get("voiceMode", False))
            render_capabilities = normalize_render_capabilities(params.get("renderCapabilities"))

            # Track mapping so approval events can find the relay session
            self._session_key_to_relay_id[session_key] = session_id
            if not session_key.startswith("web:"):
                self._session_key_to_relay_id[f"web:{session_key}"] = session_id
            run_id = idempotency_key

            # Save attachments to disk
            media: list[str] = []
            attachments = params.get("attachments") or []
            try:
                message_text = append_browser_annotation_context(message_text, attachments)
                if attachments:
                    from flowly.live_voice.events import EventAccess, event_access_scope

                    media_dir = get_flowly_home() / "media"
                    source = self.chat_commands.control_scope(run_id)
                    access = EventAccess(scopes=(source,)) if source is not None else EventAccess(blocked=True)
                    with event_access_scope(access):
                        media = _save_attachments(attachments, media_dir)
            except Exception:
                self.chat_commands.settle(session_key, run_id, 'error')
                logger.exception('Could not prepare attachments for chat run {}', run_id)
                await ws.send(json.dumps({
                    'type': 'rpc', 'id': rpc_id, 'sessionId': session_id,
                    'error': {'code': 'CHAT_INPUT_FAILED', 'message': 'The attached files could not be prepared. Please try again.'},
                }))
                return

            # Track the in-flight stream so a client that leaves and re-enters
            # this chat mid-run can fetch the partial via the chat.inflight RPC
            # (served by this same web channel, line ~954) and restore the live
            # bubble. Keyed by the SAME session_key the desktop passes, so its
            # chat.inflight call resolves to this run. Mirrors the direct gateway
            # (GatewayServer._run_chat); previously only the gateway fed this,
            # so relay/cloud chats had no resume.
            from flowly.agent import inflight

            if getattr(self, 'supports_turn_start', False) is not True:
                inflight.begin(session_key, run_id, message_text)

            # ONE streaming implementation, shared with autonomous goal turns
            # (see ``_make_stream_callback``). Two copies drifted before: the
            # goal path forgot the local ``agent`` event and the embedded
            # desktop bot showed no live text for goal turns at all.
            stream_callback = self._make_stream_callback(
                session_id, session_key, run_id,
            )

            # Process message asynchronously (don't block the recv loop).
            # Tracking the Task by run_id is what makes chat.abort
            # actually able to cancel an in-flight turn — without
            # the map the abort handler had nothing to call .cancel()
            # on and the stop button was effectively a no-op.
            task = asyncio.create_task(
                self._process_message(
                    session_id,
                    session_key,
                    message_text,
                    run_id,
                    stream_callback,
                    media,
                    voice_mode,
                    render_capabilities,
                )
            )
            self._track_chat_publish(task, session_key, run_id)
            if getattr(self, 'supports_turn_start', False) is not True:
                self.chat_commands.settle(session_key, run_id, "running")
            # ACK immediately with runId
            ack = {
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "result": {"runId": run_id},
            }
            await ws.send(json.dumps(ack))

        elif method == "chat.command":
            from flowly.session.commands import command_status
            try:
                response = {"result": command_status(self.chat_commands, params)}
            except ValueError as exc:
                response = {"error": {"code": "INVALID_REQUEST", "message": str(exc)}}
            await ws.send(json.dumps({"type": "rpc", "id": rpc_id, "sessionId": session_id, **response}))

        elif method == "chat.abort":
            from flowly.session.commands import validate_command_control
            try:
                validate_command_control(self.chat_commands, params)
            except ValueError as exc:
                await ws.send(json.dumps({
                    'type': 'rpc', 'id': rpc_id, 'sessionId': session_id,
                    'error': {'code': 'TASK_TARGET_CHANGED', 'message': str(exc)},
                }))
                return
            run_id = params.get("runId", "")
            cancelled = False
            legacy_cancelled = False
            # ``task.cancel()`` used to be the heart of this handler,
            # but ``self._active_tasks[run_id]`` only ever held the
            # short-lived task that pushes the inbound to the bus —
            # done in microseconds, so the cancel was a no-op by the
            # time Stop reached us. The actual turn runs inside
            # ``agent.run()`` and must stay alive long enough to persist its
            # partial transcript and emit one authoritative final event.
            #
            # Instead, mark the run as aborted on the agent. The
            # streaming loop polls this flag between every chunk and the
            # shared run-abort controller cancels the active tool child task.
            # The parent turn then exits cooperatively, preserving whatever
            # partial text was accumulated. The OutboundMessage
            # the agent eventually publishes carries ``aborted: true``
            # in its metadata so the relay + client UI can render the
            # partial with an [Aborted] marker.
            from flowly.session.control_access import run_control_guard

            with run_control_guard(self.chat_commands, params) as actual_session:
                if self._abort_callback is not None:
                    try:
                        # Older callbacks return None after accepting a stop;
                        # an explicit rejection must remain a failed request.
                        cancelled = self._abort_callback(run_id) is not False
                        logger.info(
                            f"[WebChannel] chat.abort marked run_id={run_id} for cooperative interrupt"
                        )
                    except Exception as error:
                        if feature_rpc.has_voice_account():
                            logger.error('[WebChannel] account chat.abort failed ({})', type(error).__name__)
                        else:
                            logger.exception(f"[WebChannel] abort_callback failed for run_id={run_id}")
                else:
                    logger.warning(
                        f"[WebChannel] chat.abort: no abort_callback registered "
                        f"(run_id={run_id}) — falling back to legacy task.cancel()"
                    )
                    task = self._active_tasks.get(run_id)
                    if task is not None and not task.done():
                        legacy_cancelled = task.cancel()
                        cancelled = legacy_cancelled
            # ACK only. The run remains in-flight until AgentLoop persists its
            # partial transcript and emits the single authoritative
            # state:"final", aborted:true event. Sending a second terminal
            # "aborted" event here used to make clients clear the run id/tool
            # panel before that final arrived.
            ack = {
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "result": {"ok": True, "cancelled": cancelled},
            }
            await ws.send(json.dumps(ack))
            if legacy_cancelled:
                # A legacy embedder without AgentLoop.mark_aborted cannot
                # produce the authoritative partial final. Preserve its old
                # terminal event so existing clients do not remain busy
                # forever; current bots always use the cooperative path.
                await ws.send(
                    json.dumps(
                        {
                            "type": "event",
                            "sessionId": session_id,
                            "event": "chat",
                            # Same routing requirement as the streaming and
                            # final events: without sessionKey the relay can
                            # only reach the socket the turn started on.
                            "data": {
                                "state": "aborted",
                                "runId": run_id,
                                "sessionKey": actual_session or self._session_key_for_relay_id(session_id),
                            },
                        }
                    )
                )

        elif method == "chat.history":
            # History is managed by Firestore on the client side — return empty
            ack = {
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "result": {"messages": []},
            }
            await ws.send(json.dumps(ack))

        elif method == "exec.approval.resolve":
            approval_id = params.get("id", "")
            decision = params.get("decision", "")
            if decision in ("allow-once", "allow-always", "deny"):
                from flowly.exec.approval_manager import get_approval_manager

                manager = get_approval_manager()
                ok = manager.resolve(approval_id, decision)
                ack = {"type": "rpc", "id": rpc_id, "sessionId": session_id, "result": {"ok": ok}}
            else:
                ack = {
                    "type": "rpc",
                    "id": rpc_id,
                    "sessionId": session_id,
                    "error": {"code": "INVALID", "message": "Invalid decision"},
                }
            await ws.send(json.dumps(ack))

        elif method == "agent.clarify.resolve":
            clarify_id = params.get("id", "")
            answer = params.get("answer", "")
            if clarify_id and isinstance(answer, str):
                from flowly.clarify.manager import get_clarify_manager

                manager = get_clarify_manager()
                ok = manager.resolve(clarify_id, answer)
                ack = {"type": "rpc", "id": rpc_id, "sessionId": session_id, "result": {"ok": ok}}
            else:
                ack = {
                    "type": "rpc",
                    "id": rpc_id,
                    "sessionId": session_id,
                    "error": {"code": "INVALID", "message": "Invalid clarify resolve"},
                }
            await ws.send(json.dumps(ack))

        elif method == "commands.list":
            # Slash command catalogue for the composer's ``/``
            # autocomplete dropdown. The gateway server has the same
            # handler for desktop-direct connections; this branch
            # handles the relay path where the web/iOS client talks
            # to the bot through ``wss://relay.useflowlyapp.com``.
            from flowly.agent.skill_bundles import build_commands_catalogue

            ack = {
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "result": build_commands_catalogue(
                    surface=(
                        params.get("surface")
                        if isinstance(params.get("surface"), str)
                        else None
                    ),
                ),
            }
            await ws.send(json.dumps(ack))

        elif method.startswith("profiles."):
            await self._handle_profile_rpc(ws, msg)

        elif method in feature_rpc.FEATURE_METHODS:
            # Every desktop/iOS feature RPC (connections, config, memory, kg,
            # sessions, audit, persona, provider, skills, assistants, pairing)
            # is served from the transport-agnostic ``feature_rpc`` dispatch —
            # the same surface the direct gateway serves. One place to add an
            # RPC; both transports light it up.
            await self._handle_feature_rpc(ws, rpc_id, session_id, method, params)

        else:
            # Answer, never just log. A silent drop leaves the client waiting
            # out its full RPC timeout — version skew between an app and its
            # bot then reads as a hung connection instead of a clean,
            # immediate "this host doesn't speak that yet".
            logger.warning(f"[WebChannel] Unknown RPC method: {method}")
            await ws.send(json.dumps({
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "error": {
                    "code": "METHOD_NOT_FOUND",
                    "message": "This Flowly does not support this operation. It may need an update.",
                    "retryable": False,
                },
            }))

    async def _handle_feature_rpc(
        self, ws, rpc_id: str, session_id: str, method: str, params: dict
    ) -> None:
        """Serve a feature RPC via the shared ``feature_rpc`` dispatch, wrapped
        in the relay reply envelope.

        A structured :class:`feature_rpc.FeatureRpcError` becomes an ``error``
        reply; anything else becomes INTERNAL (and is logged). A mutation that
        needs a restart is ACKed FIRST, then the gateway is bounced in the
        background — the restart kills THIS connection, so awaiting it would cut
        the socket before the reply flushed; the client reconnects on its own.
        """
        try:
            result, needs_restart = await feature_rpc.dispatch(method, params)
        except feature_rpc.FeatureRpcError as e:
            await ws.send(
                json.dumps(
                    {
                        "type": "rpc",
                        "id": rpc_id,
                        "sessionId": session_id,
                        "error": {"code": e.code, "message": e.message},
                    }
                )
            )
            return
        except Exception as e:
            if method.startswith('voice.') or 'voiceAccess' in params or feature_rpc.has_voice_account():
                logger.error('[WebChannel] voice feature RPC {} failed ({})', method, type(e).__name__)
                message = 'Voice request could not be completed.'
            else:
                logger.exception(f"[WebChannel] feature rpc {method} failed")
                message = str(e)
            await ws.send(
                json.dumps(
                    {
                        "type": "rpc",
                        "id": rpc_id,
                        "sessionId": session_id,
                        "error": {"code": "INTERNAL", "message": message},
                    }
                )
            )
            return
        if method in {"subagents.list", "subagents.get"} and session_id:
            now = time.monotonic()
            self._prune_subagent_observers(now)
            if session_id in self._subagent_observers or len(self._subagent_observers) < 64:
                self._subagent_observers[session_id] = now + 120
                if "eventVersion" in params:
                    self._subagent_event_versions[session_id] = event_version(params)
        if method == "system.capabilities" and self._profile_host is not None:
            result = {**result, "profileHost": self._profile_host.capabilities()}
        await ws.send(
            json.dumps(
                {
                    "type": "rpc",
                    "id": rpc_id,
                    "sessionId": session_id,
                    "result": result,
                }
            )
        )
        if needs_restart:
            self._schedule_feature_restart()

    def _prune_profile_subagent_observers(self, now: float) -> None:
        for key, (_version, expiry) in tuple(self._profile_subagent_observers.items()):
            if expiry <= now:
                self._remove_profile_subagent_observer(key)

    def _remove_profile_subagent_observer(self, key: tuple[str, str]) -> None:
        if self._profile_subagent_observers.pop(key, None) is not None:
            session_id, profile = key
            if profile == "default" and self._profile_host is not None:
                self._profile_host.release_default_events(self._profile_lease_owner(session_id) + ":subagents")

    def _clear_profile_subagent_observers(self, session_id: str | None = None) -> None:
        # In-flight profile RPCs can complete after browser-disconnected. They
        # must not resurrect the subscription that disconnect just removed.
        self._profile_subagent_reads = {token: reader for token, reader in self._profile_subagent_reads.items()
                                       if session_id is not None and reader != session_id}
        for key in tuple(self._profile_subagent_observers):
            if session_id is None or key[0] == session_id:
                self._remove_profile_subagent_observer(key)

    def _observe_profile_subagents(self, session_id: str, profile: str, params: dict) -> None:
        now = time.monotonic()
        self._prune_profile_subagent_observers(now)
        key = (session_id, profile)
        previous = self._profile_subagent_observers.get(key)
        if previous is None and len(self._profile_subagent_observers) >= 64:
            return
        version = event_version(params) if "eventVersion" in params else (previous[0] if previous else 1)
        self._profile_subagent_observers[key] = (version, now + 120)
        if previous is None and profile == "default" and self._profile_host is not None:
            self._profile_host.retain_default_events(self._profile_lease_owner(session_id) + ":subagents")

    def _prune_subagent_observers(self, now: float) -> None:
        self._subagent_observers = {key: expiry for key, expiry in self._subagent_observers.items() if expiry > now}
        self._subagent_event_versions = {key: version for key, version in self._subagent_event_versions.items()
                                        if key in self._subagent_observers}

    async def send_subagent_event(self, event_name: str, data: dict) -> None:
        """Live updates for successful task-history readers; no offline replay.

        A list/get refresh renews a two-minute lease. Reconnect requires a new
        snapshot. Routing uses the authenticated browser's relay session only.
        """
        if not self._ws:
            return
        now = time.monotonic()
        self._prune_subagent_observers(now)
        async def send(session_id: str) -> None:
            name, body = client_event(event_name, data, self._subagent_event_versions.get(session_id, 1))
            payload = {"type": "event", "event": name, "data": body, "sessionId": session_id}
            frame = json.dumps(payload)
            pending = RelayOutbound(frame, self._outbound_event_access(frame))
            await asyncio.wait_for(self._deliver_relay_outbound(pending), timeout=1)
        await asyncio.gather(*(send(key) for key in self._subagent_observers), return_exceptions=True)

    def _schedule_feature_restart(self) -> None:
        """Bounce the gateway after the ACK frame has flushed, so a config/
        channel change takes effect without cutting the reply mid-flight."""

        async def _run() -> None:
            await asyncio.sleep(0.5)
            try:
                from flowly.integrations.service_control import restart_gateway

                await restart_gateway()
            except Exception:
                logger.exception("[WebChannel] feature restart failed")

        asyncio.create_task(_run())

    def _make_stream_callback(self, session_id: str, session_key: str, run_id: str):
        """The per-delta fan-out every relay turn uses.

        ``sessionKey`` is what makes the relay route a delta to the
        CONVERSATION rather than to the socket that started the turn — so a
        client that reconnected mid-turn (or never started one, as with an
        autonomous goal turn) still receives the live reply.
        """

        async def stream_callback(delta: str) -> None:
            from flowly.agent import inflight

            inflight.append(session_key, run_id, delta)
            data = {
                "state": "streaming",
                "runId": run_id,
                "sessionKey": session_key,
                "delta": delta,
            }
            await self._send_or_queue(json.dumps({
                "type": "event",
                "sessionId": session_id,
                "event": "chat",
                "data": data,
            }))
            local_data = {**data, "source": "relay"}
            asyncio.create_task(self._emit_local_event("chat", local_data))
            asyncio.create_task(self._emit_local_event(
                "agent",
                {
                    "stream": "assistant",
                    "runId": run_id,
                    "sessionKey": session_key,
                    "source": "relay",
                    "data": {"text": delta},
                },
            ))

        return stream_callback

    async def run_autonomous_turn(
        self,
        session_key: str,
        goal_metadata: dict[str, Any],
    ) -> bool:
        """Run an agent-authored turn exactly like a client's ``chat.send``.

        Same run identity, streaming callback, in-flight registration and
        final delivery — the only difference is that the prompt is resolved
        inside the agent (from the standing goal) and announced through
        ``on_user_message`` once the goal guards have passed.
        """
        session_id = self._session_key_to_relay_id.get(session_key) or ""
        if not session_id and session_key.startswith("web:"):
            session_id = session_key.split(":", 1)[1]
        if not session_id:
            # Nothing to stream to yet (fresh process, client not back). Report
            # it so the caller can still run the turn over the bus rather than
            # dropping the goal's work.
            logger.debug("[WebChannel] no relay session for {}", session_key)
            return False
        run_id = str(uuid.uuid4())
        self.chat_commands.accept(session_key, run_id, {
            'turnOrigin': 'goal', 'goalId': goal_metadata.get('_goal_continuation_goal_id'),
        })

        from flowly.agent import inflight

        if getattr(self, 'supports_turn_start', False) is not True:
            inflight.begin(session_key, run_id, "")

        async def announce_user(text: str) -> None:
            """Publish the agent-authored prompt as this run's user turn."""
            inflight.begin(session_key, run_id, text)
            data = {
                "state": "user",
                "runId": run_id,
                "sessionKey": session_key,
                "goalRun": True,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
            await self._send_or_queue(json.dumps({
                "type": "event",
                "sessionId": session_id,
                "event": "chat",
                "data": data,
            }))
            asyncio.create_task(self._emit_local_event("chat", {**data, "source": "relay"}))

        started = goal_metadata.pop("on_run_started", None)
        try:
            if started is not None:
                started(run_id)
        except BaseException as exc:
            self.chat_commands.settle(session_key, run_id, 'aborted' if isinstance(exc, asyncio.CancelledError) else 'error')
            inflight.finish(session_key, run_id)
            raise
        metadata = {**goal_metadata, "on_user_message": announce_user}
        task = asyncio.create_task(self._process_message(
            session_id,
            session_key,
            "",
            run_id,
            self._make_stream_callback(session_id, session_key, run_id),
            extra_metadata=metadata,
        ))
        self._track_chat_publish(task, session_key, run_id)
        return True

    def _track_chat_publish(self, task: asyncio.Task, session_key: str, run_id: str) -> None:
        self._active_tasks[run_id] = task

        def finished(completed: asyncio.Task) -> None:
            self._active_tasks.pop(run_id, None)
            # Successful publication is only a handoff to the agent queue;
            # its receipt and live partial must remain until the actual turn.
            if completed.cancelled():
                status = 'aborted'
            elif completed.exception() is not None:
                status = 'error'
            else:
                return
            from flowly.agent import inflight

            self.chat_commands.settle(session_key, run_id, status)
            inflight.finish(session_key, run_id)

        task.add_done_callback(finished)

    async def _process_message(
        self,
        session_id: str,
        session_key: str,
        content: str,
        run_id: str,
        stream_callback=None,
        media: list[str] | None = None,
        voice_mode: bool = False,
        render_capabilities: tuple[str, ...] = (),
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Push message to bus and wait for agent response."""
        metadata: dict[str, Any] = {
            "session_key": session_key,
            "run_id": run_id,
            "stream_callback": stream_callback,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        if getattr(self, 'supports_turn_start', False) is True:
            from flowly.agent import inflight

            async def on_turn_started(text: str) -> None:
                self.chat_commands.settle(session_key, run_id, 'running')
                inflight.begin(session_key, run_id, text, goal_run=bool(metadata.get('goal_run')))

            metadata['_on_turn_started'] = on_turn_started

            async def on_iteration(event: dict) -> None:
                wrapped = {**event, 'runId': run_id, 'state': 'iteration_step'}
                inflight.append_iteration(session_key, run_id, wrapped)
                await self.send(OutboundMessage(
                    channel='web', chat_id=session_id, content='',
                    metadata={'iteration_event': wrapped, 'session_key': session_key},
                ))

            metadata['on_iteration'] = on_iteration
        if voice_mode:
            metadata["voice_mode"] = True
        if render_capabilities:
            metadata["render_capabilities"] = render_capabilities

        inbound_with_session = _WebInboundMessage(
            channel="web",
            sender_id=session_id,
            chat_id=session_id,
            content=content,
            media=media or [],
            metadata=metadata,
            _session_key=session_key,
        )

        try:
            await self.bus.publish_inbound(inbound_with_session)
        except asyncio.CancelledError:
            self.chat_commands.settle(session_key, run_id, 'aborted')
            raise
        except Exception:
            from flowly.agent import inflight

            self.chat_commands.settle(session_key, run_id, 'error')
            inflight.finish(session_key, run_id)
            logger.exception('Could not publish chat run {} to the agent queue', run_id)
            try:
                await self.send(OutboundMessage(
                    channel='web', chat_id=session_id,
                    content='The message could not be queued. Please try again.',
                    metadata={'run_id': run_id, 'session_key': session_key,
                              'error': {'code': 'CHAT_DISPATCH_FAILED'}},
                ))
            except Exception:
                logger.exception('Could not deliver the dispatch failure for run {}', run_id)


# ---------------------------------------------------------------------------
# InboundMessage subclass that allows overriding session_key
# ---------------------------------------------------------------------------

@dataclass
class _WebInboundMessage(_Base):
    _session_key: str = ""

    @property
    def session_key(self) -> str:  # type: ignore[override]
        return self._session_key or f"web:{self.chat_id}"
