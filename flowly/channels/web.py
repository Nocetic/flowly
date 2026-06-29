"""Web chat channel — outbound WebSocket relay (no SSH, no password)."""

import asyncio
import base64
import io
import json
import mimetypes
import os
import ssl
import uuid
from pathlib import Path
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed
from loguru import logger

from flowly.bus.events import InboundMessage, OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.base import BaseChannel
from flowly.channels import feature_rpc
from flowly.config.schema import WebChannelConfig

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
_IMAGE_MAX_DIMENSION = 1280       # px on the longest edge
_IMAGE_INITIAL_QUALITY = 75
_IMAGE_MIN_QUALITY = 40
_OUTBOUND_QUEUE_LIMIT = 50        # cap pending replays to avoid unbounded growth


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
        fpath.write_bytes(data)
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
        self._reconnect_delay = 5  # seconds
        self._max_reconnect_delay = 60
        # Track active browser sessions: sessionId → asyncio.Event (response ready)
        self._pending: dict[str, asyncio.Queue] = {}
        # Map session_key (e.g. "web:FirestoreId") → relay session_id (browser UUID)
        self._session_key_to_relay_id: dict[str, str] = {}
        # Outbound replay queue. When a send fails (transient WS drop, frame
        # too large after retry, etc.) the serialised payload is parked here
        # so the next successful connection can flush it. Bounded to prevent
        # runaway growth on prolonged outages.
        self._outbound_queue: list[str] = []
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
                logger.warning(f"[WebChannel] Disconnected ({e}), reconnecting in {delay}s...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None

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
                    "iterationIdx": iter_event.get("iterationIdx", 0),
                    "role": iter_event.get("role"),
                    "content": iter_event.get("content", ""),
                    **(
                        {"tool_calls": iter_event["tool_calls"]}
                        if iter_event.get("tool_calls") else {}
                    ),
                    **(
                        {"tool_call_id": iter_event["tool_call_id"]}
                        if iter_event.get("tool_call_id") else {}
                    ),
                    **(
                        {"name": iter_event["name"]}
                        if iter_event.get("name") else {}
                    ),
                },
            }
            await self._send_or_queue(json.dumps(event_msg))
            return

        run_id = msg.metadata.get("run_id", str(uuid.uuid4()))

        # Build content blocks — always start with text
        content_blocks: list[dict[str, Any]] = []
        if msg.content:
            content_blocks.append({"type": "text", "text": msg.content})

        # Encode media files as base64 image blocks (relay uploads to S3)
        for media_path in msg.media:
            try:
                p = Path(media_path)
                if not p.is_file():
                    continue
                mime_type = mimetypes.guess_type(str(p))[0] or "image/png"
                if not mime_type.startswith("image/"):
                    continue
                compressed = _compress_image_for_transport(p)
                if compressed is None:
                    # Pillow missing or compression blew up — drop the
                    # image rather than blow up the WS frame.
                    continue
                jpeg_bytes, jpeg_mime = compressed
                data = base64.b64encode(jpeg_bytes).decode("ascii")
                content_blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": jpeg_mime,
                        "data": data,
                    },
                })
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
            "message": {
                "content": content_blocks,
            },
        }
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

        event_msg = {
            "type": "event",
            "sessionId": session_id,
            "event": "chat",
            "data": data_block,
        }

        payload = json.dumps(event_msg)
        await self._send_or_queue(payload)

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

    async def _send_or_queue(self, payload: str) -> None:
        """Send a serialised payload, or park it for replay on failure.

        Failure modes covered:
          - WS not connected yet (cold start or mid-reconnect)
          - WS closed mid-send (1009, 1011, network drop)
          - Frame oversized despite compression (rare, but graceful)

        The queue is bounded; oldest entries get dropped to make room.
        """
        if not self._ws:
            logger.warning("[WebChannel] Not connected — queuing payload for replay")
            self._enqueue_payload(payload)
            return

        try:
            await self._ws.send(payload)
        except ConnectionClosed as e:
            logger.warning(
                f"[WebChannel] Send failed (ConnectionClosed: code={e.code} "
                f"reason={e.reason!r}) — queuing for replay"
            )
            self._enqueue_payload(payload)
        except Exception as e:
            logger.error(f"[WebChannel] Failed to send response: {e}")
            self._enqueue_payload(payload)

    def _enqueue_payload(self, payload: str) -> None:
        """Park a payload for replay. Drops the oldest if at capacity."""
        if len(self._outbound_queue) >= _OUTBOUND_QUEUE_LIMIT:
            dropped = self._outbound_queue.pop(0)
            logger.warning(
                f"[WebChannel] Outbound queue full ({_OUTBOUND_QUEUE_LIMIT}) — "
                f"dropping oldest payload ({len(dropped)} bytes)"
            )
        self._outbound_queue.append(payload)

    async def _flush_outbound_queue(self) -> None:
        """Replay any queued payloads after a successful reconnect."""
        if not self._outbound_queue or not self._ws:
            return
        # Snapshot + clear so any send-induced re-enqueues don't double-replay.
        pending = self._outbound_queue
        self._outbound_queue = []
        logger.info(f"[WebChannel] Flushing {len(pending)} queued payload(s)")
        for payload in pending:
            try:
                await self._ws.send(payload)
            except Exception as e:
                # Connection dropped mid-flush — re-park remainder and bail.
                logger.warning(f"[WebChannel] Flush interrupted: {e}")
                idx = pending.index(payload)
                for remaining in pending[idx:]:
                    self._enqueue_payload(remaining)
                return

    async def send_approval_event(
        self,
        session_key: str,
        approval_id: str,
        command: str,
        expires_at: float,
        supports_always: bool = True,
    ) -> None:
        """Push exec approval request to the browser/iOS via relay."""
        if not self._ws:
            return

        # Resolve to relay session_id (browser UUID) from session_key
        relay_id = self._session_key_to_relay_id.get(session_key)
        if not relay_id:
            # Try with web: prefix
            relay_id = self._session_key_to_relay_id.get(f"web:{session_key}")
        if not relay_id:
            logger.warning(f"[WebChannel] No relay session found for {session_key}")
            return

        event_msg = {
            "type": "event",
            "sessionId": relay_id,
            "event": "exec.approval.requested",
            "data": {
                "id": approval_id,
                "command": command,
                "expiresAt": expires_at,
                "supportsAlways": supports_always,
            },
        }
        # Approval requests are user-blocking — must survive a flapping WS.
        await self._send_or_queue(json.dumps(event_msg))
        logger.info(f"[WebChannel] Sent approval event {approval_id} to relay session {relay_id}")

    async def send_clarify_event(
        self,
        session_key: str,
        clarify_id: str,
        question: str,
        choices: list[str] | None,
        expires_at: float,
    ) -> None:
        """Push an agent clarify question to the browser/iOS via relay."""
        if not self._ws:
            return

        relay_id = self._session_key_to_relay_id.get(session_key)
        if not relay_id:
            relay_id = self._session_key_to_relay_id.get(f"web:{session_key}")
        if not relay_id:
            logger.warning(f"[WebChannel] No relay session found for {session_key}")
            return

        event_msg = {
            "type": "event",
            "sessionId": relay_id,
            "event": "agent.clarify.requested",
            "data": {
                "id": clarify_id,
                "question": question,
                "choices": choices,
                "expiresAt": expires_at,
            },
        }
        # Clarify questions are user-blocking — must survive a flapping WS.
        await self._send_or_queue(json.dumps(event_msg))
        logger.info(f"[WebChannel] Sent clarify event {clarify_id} to relay session {relay_id}")

    async def send_compaction_event(
        self,
        session_key: str,
        tokens_before: int,
        tokens_after: int,
        messages_removed: int,
        phase: str = "completed",
    ) -> None:
        """Notify the browser/iOS that context is being compacted or was compacted."""
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
            },
        }
        try:
            await self._ws.send(json.dumps(event_msg))
            logger.info(f"[WebChannel] Sent compaction event to relay session {relay_id}")
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
            await self._ws.send(json.dumps(event_msg))
            logger.info(f"[WebChannel] Sent auto-title to relay session {relay_id}: {title!r}")
        except Exception as e:
            logger.debug(f"[WebChannel] Failed to send title event: {e}")

    async def _connect_and_run(self) -> None:
        """Open one WebSocket connection to the relay proxy and process messages."""
        from jose import jwt as jose_jwt
        import time

        jwt_secret = os.environ.get("MOLTBOT_PROXY_JWT_SECRET", "")
        if not jwt_secret or jwt_secret == "flowly-moltbot-proxy-secret-change-in-production":
            # Use jwt_secret from config, fallback to auth_token
            jwt_secret = self.config.jwt_secret or self.config.auth_token or ""
        if not jwt_secret:
            logger.warning("[WebChannel] No JWT secret configured — set MOLTBOT_PROXY_JWT_SECRET env var")

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
        token = jose_jwt.encode(payload, jwt_secret, algorithm="HS256")
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
            self._ws = ws
            logger.info("[WebChannel] Connected to relay proxy")

            # Replay anything that piled up while disconnected. Done before
            # entering the recv loop so a fresh inbound message can't race
            # against a stale outbound one.
            await self._flush_outbound_queue()

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    await self._handle_relay_message(ws, msg)
                except json.JSONDecodeError:
                    logger.warning("[WebChannel] Invalid JSON from relay")
                except Exception as e:
                    logger.error(f"[WebChannel] Error handling relay message: {e}")

        self._ws = None

    async def _handle_relay_message(self, ws, msg: dict) -> None:
        """Handle a message forwarded by the relay proxy."""
        msg_type = msg.get("type")

        if msg_type == "ready":
            cron_session_id = msg.get("cronSessionId")
            if cron_session_id:
                self._cron_session_id = cron_session_id
                logger.info(f"[WebChannel] Relay confirmed agent ready — cronSessionId={cron_session_id[:8]}")
            else:
                logger.info("[WebChannel] Relay confirmed agent ready (no cronSessionId)")
            if self._on_ready:
                try:
                    result = self._on_ready()
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception as e:
                    logger.warning(f"[WebChannel] on_ready callback failed: {e}")

        elif msg_type == "browser-connected":
            session_id = msg.get("sessionId", "")
            logger.info(f"[WebChannel] Browser connected: {session_id}")

        elif msg_type == "browser-disconnected":
            session_id = msg.get("sessionId", "")
            logger.info(f"[WebChannel] Browser disconnected: {session_id}")
            self._pending.pop(session_id, None)

        elif msg_type == "rpc":
            await self._handle_rpc(ws, msg)

        elif msg_type == "ping":
            session_id = msg.get("sessionId")
            pong = {"type": "pong", "timestamp": msg.get("timestamp")}
            if session_id:
                pong["sessionId"] = session_id
            await ws.send(json.dumps(pong))

        elif msg_type in ("cron.registered", "cron.unregistered"):
            # Relay ACKs the cron.register / cron.unregister we sent — no
            # action needed, the write to Firestore is relay-side. We log
            # at debug so failures (if relay ever switches to emitting
            # cron.register.failed etc.) are still surfaced by the
            # unhandled branch below.
            job_name = msg.get("job", {}).get("name") or msg.get("name") or "?"
            logger.debug(
                f"[WebChannel] Relay {msg_type}: '{job_name}' synced to Firestore"
            )

        else:
            logger.debug(f"[WebChannel] Unhandled relay message type: {msg_type}")

    async def _handle_rpc(self, ws, msg: dict) -> None:
        """Handle an RPC call from the browser (forwarded by proxy)."""
        method = msg.get("method", "")
        rpc_id = msg.get("id", "")
        params = msg.get("params", {})
        session_id = msg.get("sessionId", "")

        if method == "chat.send":
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
            if not params.get("sessionKey"):
                logger.warning(
                    "[WebChannel] chat.send without sessionKey; using "
                    f"ws_id={session_id[:8]} as session_key. Conversation "
                    "history will fragment across reconnects. Client "
                    "should send a stable sessionKey (chat document id)."
                )
            idempotency_key = params.get("idempotencyKey") or str(uuid.uuid4())

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
                    logger.info(
                        f"[WebChannel] chat.send pinned cwd={cwd} for session={session_key}"
                    )
                except ValueError:
                    err = {
                        "type": "rpc",
                        "id": rpc_id,
                        "sessionId": session_id,
                        "error": {
                            "code": "INVALID_CWD",
                            "message": f"Not an existing absolute directory: {cwd}",
                        },
                    }
                    await ws.send(json.dumps(err))
                    return

            voice_mode = bool(params.get("voiceMode", False))

            # Track mapping so approval events can find the relay session
            self._session_key_to_relay_id[session_key] = session_id
            if not session_key.startswith("web:"):
                self._session_key_to_relay_id[f"web:{session_key}"] = session_id
            run_id = idempotency_key

            # Save attachments to disk
            media: list[str] = []
            attachments = params.get("attachments") or []
            if attachments:
                media_dir = get_flowly_home() / "media"
                media = _save_attachments(attachments, media_dir)

            # ACK immediately with runId
            ack = {
                "type": "rpc",
                "id": rpc_id,
                "sessionId": session_id,
                "result": {"runId": run_id},
            }
            await ws.send(json.dumps(ack))

            # Track the in-flight stream so a client that leaves and re-enters
            # this chat mid-run can fetch the partial via the chat.inflight RPC
            # (served by this same web channel, line ~954) and restore the live
            # bubble. Keyed by the SAME session_key the desktop passes, so its
            # chat.inflight call resolves to this run. Mirrors the direct gateway
            # (GatewayServer._run_chat); previously only the gateway fed this,
            # so relay/cloud chats had no resume.
            from flowly.agent import inflight
            inflight.begin(session_key, run_id, message_text)

            # Build streaming callback — sends delta events to this browser session.
            # Captures ws and session_id at call time (safe even if self._ws reconnects).
            async def stream_callback(delta: str) -> None:
                inflight.append(session_key, run_id, delta)
                await ws.send(json.dumps({
                    "type": "event",
                    "sessionId": session_id,
                    "event": "chat",
                    "data": {"state": "streaming", "runId": run_id, "delta": delta},
                }))

            # Process message asynchronously (don't block the recv loop).
            # Tracking the Task by run_id is what makes chat.abort
            # actually able to cancel an in-flight turn — without
            # the map the abort handler had nothing to call .cancel()
            # on and the stop button was effectively a no-op.
            task = asyncio.create_task(
                self._process_message(session_id, session_key, message_text, run_id, stream_callback, media, voice_mode)
            )
            self._active_tasks[run_id] = task

            # Auto-drain when the task completes. NOTE: this task only PUBLISHES
            # the message to the bus and returns almost immediately — the real
            # turn runs later in the agent loop. So we must NOT finish the
            # in-flight partial here: doing so dropped the entry milliseconds
            # after begin(), before the run had even started, leaving
            # chat.inflight returning null for the whole tool phase (a client
            # re-entering mid tool-loop saw nothing). The agent loop finishes
            # the partial at true run completion instead (see AgentLoop
            # ._process_turn). Here we only reclaim the task slot.
            def _on_done(_t: object, _rid: str = run_id) -> None:
                self._active_tasks.pop(_rid, None)

            task.add_done_callback(_on_done)

        elif method == "chat.abort":
            run_id = params.get("runId", "")
            # ``task.cancel()`` used to be the heart of this handler,
            # but ``self._active_tasks[run_id]`` only ever held the
            # short-lived task that pushes the inbound to the bus —
            # done in microseconds, so the cancel was a no-op by the
            # time Stop reached us. The actual LLM call runs inside
            # ``agent.run()``'s long-lived task and can't be cancelled
            # per-message without a refactor.
            #
            # Instead, mark the run as aborted on the agent. The
            # streaming loop polls this flag between every chunk
            # (see ``_chat_with_stream`` and the tool-loop's
            # post-LLM check) and bails out cooperatively, preserving
            # whatever partial text was accumulated. The OutboundMessage
            # the agent eventually publishes carries ``aborted: true``
            # in its metadata so the relay + client UI can render the
            # partial with an [Aborted] marker.
            if self._abort_callback is not None:
                try:
                    self._abort_callback(run_id)
                    logger.info(
                        f"[WebChannel] chat.abort marked run_id={run_id} for cooperative interrupt"
                    )
                except Exception:
                    logger.exception(
                        f"[WebChannel] abort_callback failed for run_id={run_id}"
                    )
            else:
                logger.warning(
                    f"[WebChannel] chat.abort: no abort_callback registered "
                    f"(run_id={run_id}) — falling back to legacy task.cancel()"
                )
                task = self._active_tasks.get(run_id)
                if task is not None and not task.done():
                    task.cancel()
            # ACK + aborted event. The aborted WS event lets the
            # client clear its streaming bubble immediately rather
            # than waiting for the partial final to land via Firestore
            # (which can take 100–500ms). The Firestore-delivered
            # message arrives shortly after with the partial text.
            ack = {
                "type": "rpc", "id": rpc_id, "sessionId": session_id,
                "result": {"ok": True, "cancelled": True},
            }
            await ws.send(json.dumps(ack))
            aborted_event = {
                "type": "event",
                "sessionId": session_id,
                "event": "chat",
                "data": {"state": "aborted", "runId": run_id},
            }
            await ws.send(json.dumps(aborted_event))

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
                ack = {"type": "rpc", "id": rpc_id, "sessionId": session_id, "error": {"code": "INVALID", "message": "Invalid decision"}}
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
                ack = {"type": "rpc", "id": rpc_id, "sessionId": session_id, "error": {"code": "INVALID", "message": "Invalid clarify resolve"}}
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
                "result": build_commands_catalogue(),
            }
            await ws.send(json.dumps(ack))

        elif method in feature_rpc.FEATURE_METHODS:
            # Every desktop/iOS feature RPC (connections, config, memory, kg,
            # sessions, audit, persona, provider, skills, assistants, pairing)
            # is served from the transport-agnostic ``feature_rpc`` dispatch —
            # the same surface the direct gateway serves. One place to add an
            # RPC; both transports light it up.
            await self._handle_feature_rpc(ws, rpc_id, session_id, method, params)

        else:
            logger.warning(f"[WebChannel] Unknown RPC method: {method}")

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
            await ws.send(json.dumps({
                "type": "rpc", "id": rpc_id, "sessionId": session_id,
                "error": {"code": e.code, "message": e.message},
            }))
            return
        except Exception as e:
            logger.exception(f"[WebChannel] feature rpc {method} failed")
            await ws.send(json.dumps({
                "type": "rpc", "id": rpc_id, "sessionId": session_id,
                "error": {"code": "INTERNAL", "message": str(e)},
            }))
            return
        await ws.send(json.dumps({
            "type": "rpc", "id": rpc_id, "sessionId": session_id,
            "result": result,
        }))
        if needs_restart:
            self._schedule_feature_restart()

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

    async def _process_message(
        self,
        session_id: str,
        session_key: str,
        content: str,
        run_id: str,
        stream_callback=None,
        media: list[str] | None = None,
        voice_mode: bool = False,
    ) -> None:
        """Push message to bus and wait for agent response."""
        metadata: dict[str, Any] = {
            "session_key": session_key,
            "run_id": run_id,
            "stream_callback": stream_callback,
        }
        if voice_mode:
            metadata["voice_mode"] = True

        inbound_with_session = _WebInboundMessage(
            channel="web",
            sender_id=session_id,
            chat_id=session_id,
            content=content,
            media=media or [],
            metadata=metadata,
            _session_key=session_key,
        )

        await self.bus.publish_inbound(inbound_with_session)


# ---------------------------------------------------------------------------
# InboundMessage subclass that allows overriding session_key
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field
from datetime import datetime
from flowly.bus.events import InboundMessage as _Base
from flowly.profile import get_flowly_home


@dataclass
class _WebInboundMessage(_Base):
    _session_key: str = ""

    @property
    def session_key(self) -> str:  # type: ignore[override]
        return self._session_key or f"web:{self.chat_id}"
