"""HTTP + WebSocket API server for gateway integrations."""

import asyncio
import base64
import json
import mimetypes
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from aiohttp import web
from loguru import logger

from flowly.agent.subagent_registry import SubagentRegistry
from flowly.artifacts.context import is_internal_context_artifact
from flowly.channels import feature_rpc
from flowly.gateway.auth import (
    WsTicketStore,
    extract_request_token,
    host_origin_allowed,
    is_loopback_host,
    token_matches,
)
from flowly.profile import get_flowly_home
from flowly.session.manager import SessionManager

# Maximum allowed request body size (1MB)
_MAX_BODY_SIZE = 1024 * 1024


@web.middleware
async def _cors_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
    """Permissive CORS for the gateway HTTP API.

    The gateway only ever binds to ``127.0.0.1``, so allowing any origin is
    safe: only processes already on the user's machine can reach it. The
    middleware exists so the renderer running under Vite (``localhost:5173``)
    can hit ``/api/cron/health`` and friends during ``npm run dev`` without
    the browser blocking it as cross-origin. Production Electron builds
    serve the renderer from ``file://`` and never trip CORS in the first
    place — this is a development-only convenience.

    OPTIONS preflights short-circuit before the route handler runs so we
    don't 404 on routes that are GET/POST-only.
    """
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "600",
            },
        )
    response = await handler(request)
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Vary", "Origin")
    return response

# Type alias for the chat callback used by the /ws endpoint.
# Signature: (session_key, message, run_id, stream_callback, media, voice_mode)
#         -> (response_text, metadata)
# ``metadata`` carries ``usage`` (prompt_tokens / completion_tokens /
# cache_read_tokens / cache_write_tokens) + the effective ``model`` so
# the final chat event can deliver token counts to the TUI status bar.
# Returning a tuple instead of bare text was required to fix the
# "context bar never fills" bug: gateway used to drop usage on the floor
# between agent.process_direct (which knows usage) and the WS final
# event (which the TUI listens on for token deltas).
ChatCallback = Callable[
    [
        str,
        str,
        str,
        Callable[[str], Awaitable[None]] | None,
        list[str],
        bool,
        Callable[[dict], Awaitable[None]] | None,
    ],
    Awaitable[tuple[str, dict]],
]


# Per-file attachment cap for the direct gateway's base64-over-WS upload path.
# 25 MB raw → ~33.5 MB base64; we cap the encoded char length too so an
# oversized frame is rejected before we spend memory decoding it.
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_MAX_ATTACHMENT_B64_CHARS = 34 * 1024 * 1024

# Inline bubble-preview thumbnail for reply media on the direct gateway path.
# Small on purpose: it renders instantly with no fetch, while the full-res
# original is served on demand via ``GET /api/media?id=…`` (tap to zoom). At
# ~512 px / ~48 KB it stays crisp in the chat bubble yet is ~15-20× lighter than
# the 1280 px / 800 KB transport image — so an image-heavy history reload no
# longer ships megabytes of inline base64.
_THUMB_MAX_DIMENSION = 512
_THUMB_TARGET_BYTES = 48 * 1024
_THUMB_INITIAL_QUALITY = 70


def _save_attachments(attachments: list[dict], media_dir: Path) -> list[str]:
    """Resolve attachments to local file paths or already-uploaded URLs.

    Each attachment may contain:
      - ``cdnUrl``: a public media URL already uploaded by a trusted client.
      - ``filePath``: a native file path (preferred — zero-copy, used by desktop app
        when both app and gateway run on the same machine).
      - ``content``: base64-encoded file data (used by remote clients like iOS/relay).

    When ``filePath`` is provided and the file exists on disk, it is used directly
    without any decoding or copying.  Otherwise the base64 ``content`` is decoded
    and written to *media_dir*.
    """
    media_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for att in attachments:
        cdn_url = att.get("cdnUrl", "")
        if isinstance(cdn_url, str) and cdn_url.startswith(("http://", "https://")):
            paths.append(cdn_url)
            continue

        # Prefer native file path (desktop local optimisation)
        file_path = att.get("filePath", "")
        if file_path and Path(file_path).is_file():
            paths.append(str(Path(file_path)))
            continue

        # Fall back to base64 content (remote / relay clients)
        content = att.get("content", "")
        if not content:
            continue
        # Strip data URL prefix if present
        if isinstance(content, str) and "," in content and content.startswith("data:"):
            content = content.split(",", 1)[1]
        # Reject oversized payloads BEFORE decoding so a hostile/huge frame
        # can't blow up memory. 25 MB file → ~33.5 MB base64; cap the encoded
        # length, then re-check the decoded size. Mirrors the client-side guard
        # and the 25 MB media cap.
        if isinstance(content, str) and len(content) > _MAX_ATTACHMENT_B64_CHARS:
            logger.warning("[gateway] attachment rejected: base64 payload exceeds cap")
            continue
        try:
            data = base64.b64decode(content)
        except Exception:
            continue
        if len(data) > _MAX_ATTACHMENT_BYTES:
            logger.warning("[gateway] attachment rejected: decoded size exceeds cap")
            continue
        mime = att.get("mimeType", "")
        filename = att.get("fileName", "")
        ext = Path(filename).suffix if filename else (mimetypes.guess_extension(mime) or "")
        fpath = media_dir / f"{uuid.uuid4().hex}{ext}"
        fpath.write_bytes(data)
        paths.append(str(fpath))
    return paths


# mtime-keyed cache of computed inline thumbnails. ``chat.history`` rebuilds a
# session's attachments on every reload, so without this the same images are
# re-decoded + re-compressed each time. Keyed by (path, mtime_ns, size) so a file
# edited in place (same path) misses and recomputes. Bounded LRU — a long-lived
# gateway shouldn't grow this without limit. Process-local; lost on restart.
_THUMB_CACHE: "OrderedDict[tuple, tuple[str, str]]" = OrderedDict()
_THUMB_CACHE_MAX = 256


def _thumbnail_b64(p: Path) -> tuple[str, str] | None:
    """Return ``(base64_jpeg, mime)`` for *p*'s inline thumbnail, or ``None`` if it
    can't be produced (no Pillow / unreadable). Cached by path+mtime+size."""
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    hit = _THUMB_CACHE.get(key)
    if hit is not None:
        _THUMB_CACHE.move_to_end(key)
        return hit
    try:
        from flowly.channels.web import _compress_image_for_transport
        compressed = _compress_image_for_transport(
            p,
            max_dimension=_THUMB_MAX_DIMENSION,
            target_bytes=_THUMB_TARGET_BYTES,
            initial_quality=_THUMB_INITIAL_QUALITY,
        )
    except Exception:
        return None
    if compressed is None:
        return None
    jpeg_bytes, jpeg_mime = compressed
    result = (base64.b64encode(jpeg_bytes).decode("ascii"), jpeg_mime)
    _THUMB_CACHE[key] = result
    _THUMB_CACHE.move_to_end(key)
    while len(_THUMB_CACHE) > _THUMB_CACHE_MAX:
        _THUMB_CACHE.popitem(last=False)
    return result


def _reply_media_attachments(media_paths: list) -> list[dict]:
    """Attachments for media the agent produced THIS turn (image_generate /
    screenshot), for delivery over the direct gateway WS — where there is no
    relay/S3 to host a ``cdnUrl``.

    Each local file carries a SMALL inline base64 ``thumbnail`` (~512 px / ~48 KB
    JPEG, reusing the web channel's compressor at a thumbnail preset) so a remote
    client (iOS / desktop) renders the bubble preview immediately with no fetch and
    no auth. ``mediaId`` is always set so the client can pull the full-res original
    on demand via ``GET /api/media?id=…`` (tap to zoom). Keeping the inline payload
    small is what stops an image-heavy history reload from shipping megabytes of
    base64. Remote URLs pass through as ``cdnUrl``. Best-effort per file —
    unreadable entries are skipped.
    """
    out: list[dict] = []
    for mp in media_paths:
        try:
            if not isinstance(mp, str) or not mp:
                continue
            if mp.startswith(("http://", "https://")):
                url_mime, _ = mimetypes.guess_type(mp)
                out.append({
                    "fileName": mp.rsplit("/", 1)[-1] or mp,
                    "mimeType": url_mime or "",
                    "cdnUrl": mp,
                })
                continue
            p = Path(mp)
            if not p.is_file():
                continue
            mime, _ = mimetypes.guess_type(mp)
            att: dict = {"fileName": p.name, "mimeType": mime or "image/png", "mediaId": p.name}
            thumb = _thumbnail_b64(p)  # cached by path+mtime+size
            if thumb is not None:
                att["thumbnail"], att["mimeType"] = thumb
            out.append(att)
        except Exception:
            continue
    return out


class GatewayServer:
    """
    HTTP + WebSocket API server for gateway integrations.

    Provides endpoints for:
    - Health check (GET /health)
    - Voice message handling (POST /api/voice/message)
    - Cron triggers (POST /api/cron/run, /api/cron/reload)
    - Desktop WebSocket chat (GET /ws)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18790,
        on_voice_message: Callable[[str, str, str], Awaitable[str]] | None = None,
        on_cron_run: Callable[[str, bool], Awaitable[bool]] | None = None,
        on_cron_reload: Callable[[], Awaitable[int]] | None = None,
        on_cron_health: Callable[[], dict] | None = None,
        on_chat_message: ChatCallback | None = None,
        sessions: SessionManager | None = None,
        subagent_registry: SubagentRegistry | None = None,
        artifact_store: Any | None = None,
        board_store: Any | None = None,
        board_orchestrator: Any | None = None,
        on_compact: Callable[[str, str | None], Awaitable[dict]] | None = None,
        on_clear: Callable[[str], Awaitable[dict]] | None = None,
        # ``on_retry`` strips the trailing assistant/tool chain from a
        # session and returns the last user message text so the client
        # can re-submit it. ``on_undo`` removes the last user+assistant
        # turn entirely and returns the popped user text (for optional
        # composer pre-fill). Both return ``{"ok": bool, "text": str,
        # "removed": int}``.
        on_retry: Callable[[str], Awaitable[dict]] | None = None,
        on_undo: Callable[[str], Awaitable[dict]] | None = None,
        # ``on_provider_reload`` rebuilds the LLM provider from current
        # config and swaps it on the running agent. Used by the TUI
        # integrations modal after a provider change so the user doesn't
        # have to manually restart the gateway. Returns a dict with
        # ``{"key": "...", "source": "...", "api_base": "..."}`` on
        # success — surfaced back in the modal's status line.
        on_provider_reload: Callable[[], Awaitable[dict]] | None = None,
        on_send: Callable[[str, str], Awaitable[bool]] | None = None,
        control_token: str | None = None,
        # Static auth token for remote/self-hosted desktop clients. Empty/None
        # keeps the legacy "trust localhost" behaviour (no auth) for the
        # locally-spawned gateway. When set, every REST call needs the token
        # (X-Flowly-Token or Authorization: Bearer) and the /ws upgrade needs a
        # single-use ticket minted at POST /api/auth/ws-ticket. See
        # flowly/gateway/auth.py (token + ws-ticket model).
        auth_token: str | None = None,
    ):
        self.host = host
        self.port = port
        # Remote-client authentication. A token ONLY gates remote access: when
        # the gateway is bound to loopback every client is local and trusted, so
        # auth is never enforced there. Without this guard a token persisted
        # from an earlier remote-exposed run (it stays in config after you bind
        # back to 127.0.0.1) would 401 every local client — the desktop's chat
        # WS upgrade and its raw /api/board fetch, plus the TUI — even though
        # nothing is actually exposed. Remote binds (0.0.0.0 / a public IP)
        # still require the token.
        self._auth_token = (auth_token or "").strip()
        self._require_auth = bool(self._auth_token) and not is_loopback_host(host)
        self._ticket_store = WsTicketStore()
        # MCP write-plane control endpoint (Faz 3c). Additive + opt-in:
        # only active when BOTH a send callback and a token are supplied.
        # localhost-only + bearer-token authed. Lets `flowly mcp serve
        # --allow-writes` send messages and resolve approvals.
        self.on_send = on_send
        self._control_token = control_token
        self.on_voice_message = on_voice_message
        self.on_cron_run = on_cron_run
        self.on_cron_health = on_cron_health
        self.on_cron_reload = on_cron_reload
        self.on_provider_reload = on_provider_reload
        self.on_chat_message = on_chat_message
        self.sessions = sessions
        self.subagent_registry = subagent_registry
        self._delegate_tool: Any | None = None
        self._subagent_manager: Any | None = None
        self._coaching_manager: Any | None = None
        self.artifact_store = artifact_store
        self.board_store = board_store
        self.board_orchestrator = board_orchestrator
        self.on_compact = on_compact
        self.on_clear = on_clear
        self.on_retry = on_retry
        self.on_undo = on_undo
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        # Track active WebSocket clients and their running tasks for abort support.
        self._ws_clients: dict[str, web.WebSocketResponse] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}
        # session_key -> the WS that should currently receive this session's live
        # stream (deltas / iteration_step / final). A run streams to the socket
        # that STARTED it, but if the client leaves and re-enters mid-stream it
        # comes back on a NEW socket; without rebinding, forward events keep
        # going to the dead one and the re-entered view freezes at the
        # chat.inflight snapshot. chat.send and chat.inflight both (re)point this
        # at the calling socket, so the live stream follows the latest viewer
        # (transport-rebind, mirroring the reference gateway).
        self._session_ws: dict[str, web.WebSocketResponse] = {}
        # Tick task for periodic health pings to connected clients.
        self._tick_task: asyncio.Task | None = None
        # Browser extension client tracking (supports multiple, uses most recent)
        self._extension_clients: set[str] = set()  # all registered extension client IDs
        self._extension_active: str | None = None  # most recently registered
        self._extension_pending: dict[str, asyncio.Future] = {}  # request_id → Future

    def _create_app(self) -> web.Application:
        """Create the aiohttp application."""
        middlewares = [_cors_middleware]
        if self._require_auth:
            middlewares.append(self._make_auth_middleware())
        app = web.Application(
            client_max_size=_MAX_BODY_SIZE,
            middlewares=middlewares,
        )
        app.router.add_get("/health", self._handle_health)
        # WS-upgrade ticket minter. Active only when auth is engaged; the
        # static token (checked by the auth middleware) gates this route, and
        # it hands back a single-use short-TTL ticket for the /ws upgrade.
        if self._require_auth:
            app.router.add_post("/api/auth/ws-ticket", self._handle_ws_ticket)
        if self.on_voice_message:
            app.router.add_post("/api/voice/message", self._handle_voice_message)
        app.router.add_post("/api/cron/run", self._handle_cron_run)
        app.router.add_post("/api/cron/reload", self._handle_cron_reload)
        app.router.add_get("/api/cron/health", self._handle_cron_health)
        # Serve a saved chat attachment as a base64 data URL so the desktop can
        # render history image previews full-res + lazily (it can't read the
        # gateway's disk). Token-gated by the auth middleware. The
        # /api/media contract: basename-only id, media-dir containment (resolve +
        # symlink-safe), image allowlist, 25 MB cap.
        app.router.add_get("/api/media", self._handle_media)
        if self.on_provider_reload:
            app.router.add_post("/api/provider/reload", self._handle_provider_reload)
            app.router.add_get("/api/provider/active", self._handle_provider_active)
        app.router.add_get("/api/extension/status", self._handle_extension_status)
        # Board HTTP API — cross-channel task board (desktop/web polling).
        if self.board_store is not None:
            app.router.add_get("/api/board", self._handle_board_snapshot)
            app.router.add_post("/api/board/action", self._handle_board_action)
        # Artifact HTTP API
        if self.artifact_store:
            app.router.add_get("/api/artifacts", self._handle_artifacts_list)
            app.router.add_get("/api/artifacts/{id}", self._handle_artifacts_get)
            app.router.add_get("/api/artifacts/{id}/versions", self._handle_artifacts_versions)
        # Desktop WebSocket endpoint — active when chat callback is provided.
        if self.on_chat_message:
            app.router.add_get("/ws", self._handle_ws)
        # MCP write-plane control routes — additive, opt-in, localhost+token.
        if self.on_send and self._control_token:
            try:
                from flowly.mcp.server.control import register_control_routes
                register_control_routes(
                    app, token=self._control_token, on_send=self.on_send,
                )
            except Exception as exc:  # pragma: no cover — never block boot
                logger.warning("MCP control routes unavailable: %s", exc)
        return app

    # ------------------------------------------------------------------
    # HTTP handlers (unchanged)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Authentication (remote / self-hosted clients) — token model
    # ------------------------------------------------------------------

    # Paths reachable without the static token even when auth is engaged.
    # ``/health`` is the public handshake clients probe to discover
    # ``auth_required``; ``/ws`` authenticates via a query-param ticket inside
    # the handler (not a header), so the header middleware skips it.
    _AUTH_PUBLIC_PATHS = frozenset({"/health"})
    _AUTH_SELF_GATED_PREFIXES = ("/ws", "/api/mcp")

    def _make_auth_middleware(self):
        @web.middleware
        async def _auth_middleware(request: web.Request, handler):
            path = request.path
            if (
                path in self._AUTH_PUBLIC_PATHS
                or any(path.startswith(p) for p in self._AUTH_SELF_GATED_PREFIXES)
            ):
                return await handler(request)
            if not token_matches(extract_request_token(request), self._auth_token):
                return web.json_response(
                    {"error": "unauthorized", "detail": "Missing or invalid gateway token."},
                    status=401,
                )
            return await handler(request)

        return _auth_middleware

    async def _handle_ws_ticket(self, request: web.Request) -> web.Response:
        """Mint a single-use, short-TTL ticket for the /ws upgrade. The auth
        middleware has already verified the static token, so reaching here
        means the caller is authenticated."""
        ticket = self._ticket_store.mint()
        return web.json_response(
            {"ticket": ticket, "ttl_seconds": self._ticket_store.ttl_seconds}
        )

    def _ws_credential_ok(self, request: web.Request) -> bool:
        """Authenticate a /ws upgrade. No-op (always True) when auth is off.
        Accepts a single-use ``?ticket=`` (preferred) or the raw ``?token=``
        (back-compat for simple clients) when auth is engaged."""
        if not self._require_auth:
            return True
        q = request.rel_url.query
        if self._ticket_store.consume(q.get("ticket")):
            return True
        if token_matches(q.get("token"), self._auth_token):
            return True
        return False

    async def _handle_health(self, request: web.Request) -> web.Response:
        # Capabilities advertise what this gateway build supports so clients
        # (like the TUI) can detect a stale running process and prompt for
        # restart instead of silently degrading.
        return web.json_response({
            "status": "ok",
            # The public handshake the desktop probes to decide whether to
            # prompt for a token before connecting (the /api/status handshake).
            "auth_required": self._require_auth,
            "capabilities": [
                "tool_events",      # tool.start / tool.complete WS events
                "queue_while_busy", # client-side, but flag for future server queueing
            ],
        })

    async def _handle_cron_run(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            job_id = data.get("job_id", "")
            force = bool(data.get("force", False))
            if not job_id or len(job_id) > 256:
                return web.json_response({"error": "Invalid job_id"}, status=400)
            if not self.on_cron_run:
                return web.json_response({"error": "Cron handler not configured"}, status=500)
            success = await self.on_cron_run(job_id, force)
            if success:
                return web.json_response({"ok": True})
            else:
                return web.json_response({"error": f"Job '{job_id}' not found or disabled"}, status=404)
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            logger.error(f"Error triggering cron job: {e}")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_cron_health(self, request: web.Request) -> web.Response:
        """Return a structured cron-health snapshot.

        Designed for native app polling (desktop Activity tab). Includes
        per-job warnings with severity levels so the UI can render badges:
          * severity=error   → red (requires user attention)
          * severity=warning → yellow (monitor)
          * severity=info    → blue (in-progress recovery, e.g. retrying)
        """
        try:
            if not self.on_cron_health:
                return web.json_response(
                    {"error": "Cron health not configured"}, status=500
                )
            snapshot = self.on_cron_health()
            return web.json_response(snapshot)
        except Exception as e:
            logger.error(f"Error building cron health snapshot: {e}")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_cron_reload(self, request: web.Request) -> web.Response:
        try:
            if not self.on_cron_reload:
                return web.json_response({"error": "Cron reload not configured"}, status=500)
            count = await self.on_cron_reload()
            return web.json_response({"ok": True, "jobs": count})
        except Exception as e:
            logger.error(f"Error reloading cron jobs: {e}")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_provider_reload(self, request: web.Request) -> web.Response:
        """Rebuild the LLM provider from current config and hot-swap it.

        Returns the resolved active provider info so the caller (TUI) can
        confirm "you're now talking to X". On failure (e.g. no usable
        provider in config) we return 422 with a clear error so the UI
        can show what's wrong instead of silently leaving the old
        provider in place.
        """
        try:
            if not self.on_provider_reload:
                return web.json_response(
                    {"error": "Provider reload not configured"}, status=500,
                )
            info = await self.on_provider_reload()
            if not info or not info.get("ok"):
                return web.json_response(
                    {"error": (info or {}).get("error") or "no usable provider"},
                    status=422,
                )
            return web.json_response(info)
        except Exception as e:
            logger.error(f"Error reloading provider: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_media(self, request: web.Request) -> web.Response:
        """Serve a saved chat attachment as a base64 data URL.

        Lets the desktop render history image previews — it can't read the
        gateway host's disk. Auth is handled by the middleware (this path is
        neither public nor self-gated). Security model for /api/media:

          * ``id`` is a BASENAME only — any path separator / ``..`` / leading
            dot is rejected, so the client can't escape the media dir.
          * the resolved target must sit inside the (resolved, symlink-safe)
            media dir — defence in depth against symlink games.
          * images only (allowlist), with a 25 MB ceiling.
        """
        name = (request.rel_url.query.get("id") or "").strip()
        if (
            not name
            or "/" in name
            or "\\" in name
            or ".." in name
            or name.startswith(".")
        ):
            return web.json_response({"error": "invalid id"}, status=400)

        media_dir = (get_flowly_home() / "media").resolve()
        try:
            target = (media_dir / name).resolve()
        except (OSError, RuntimeError):
            return web.json_response({"error": "invalid id"}, status=400)
        if target != media_dir and media_dir not in target.parents:
            return web.json_response({"error": "forbidden"}, status=403)
        if not target.is_file():
            return web.json_response({"error": "not found"}, status=404)

        mime, _ = mimetypes.guess_type(str(target))
        mime = mime or ""
        if not mime.startswith("image/"):
            return web.json_response({"error": "unsupported media type"}, status=415)
        if target.stat().st_size > _MAX_ATTACHMENT_BYTES:
            return web.json_response({"error": "file too large"}, status=413)

        try:
            data = target.read_bytes()
        except OSError:
            return web.json_response({"error": "not found"}, status=404)
        encoded = base64.b64encode(data).decode("ascii")
        return web.json_response({
            "dataUrl": f"data:{mime};base64,{encoded}",
            "fileName": name,
            "mimeType": mime,
        })

    async def _handle_extension_status(self, request: web.Request) -> web.Response:
        """Report whether a Chrome extension is currently connected.

        Used by the TUI's ``/browser`` modal so it can show ``● connected``
        vs ``○ not connected`` without forcing the user to open Chrome
        and check the side panel manually.
        """
        try:
            connected = bool(self._extension_active and
                             self._extension_active in self._ws_clients)
            return web.json_response({
                "ok": True,
                "connected": connected,
                "client_count": len(self._extension_clients),
                "active_client": self._extension_active,
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    # -- Board HTTP API ----------------------------------------------------

    async def _handle_board_snapshot(self, request: web.Request) -> web.Response:
        """Return the full board as columns + counts for native/web clients.

        Mirrors the cron-health polling contract: the desktop polls this
        every few seconds and renders Todo / In Progress / Waiting / Done.
        """
        try:
            if self.board_store is None:
                return web.json_response({"error": "Board not configured"}, status=500)
            return web.json_response(self.board_store.snapshot())
        except Exception as e:
            logger.error(f"Error building board snapshot: {e}")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_board_action(self, request: web.Request) -> web.Response:
        """HTTP entry for board actions (desktop). Delegates to the shared core."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
        result, status = await self._apply_board_action(body)
        return web.json_response(result, status=status)

    async def _apply_board_action(self, body: dict) -> tuple[dict, int]:
        """Apply a board action; return ``(result, http_status)``.

        Shared by the HTTP API (desktop) and the ``board.action`` WS RPC (TUI).
        Body: ``{"action": "add|move|update|note|delete|cancel|run", ...}``.
        All writes go through the single-writer BoardStore.
        """
        from flowly.board.store import BoardError

        if self.board_store is None:
            return {"ok": False, "error": "Board not configured"}, 500
        action = (body.get("action") or "").strip()
        try:
            if action == "add":
                card = self.board_store.add_card(
                    body.get("title") or "",
                    body=body.get("body", "") or "",
                    origin_channel=body.get("originChannel", "desktop") or "desktop",
                    origin_chat_id=body.get("originChatId", "") or "",
                    created_by="user",
                )
                return {"ok": True, "card": card.to_dict()}, 200

            if action in ("clear_done", "clear"):
                # Bulk-remove finished cards (Done by default).
                from flowly.board.store import STATUS_DONE
                target = body.get("status") or STATUS_DONE
                removed = self.board_store.delete_by_status(target)
                return {"ok": True, "removed": removed}, 200

            card_id = body.get("cardId") or body.get("card_id") or ""
            if not card_id:
                return {"ok": False, "error": "cardId required"}, 400

            if action == "move":
                card = self.board_store.set_status(card_id, body.get("status") or "")
                return {"ok": True, "card": card.to_dict()}, 200

            if action == "update":
                card = self.board_store.update_card(
                    card_id, title=body.get("title"), body=body.get("body")
                )
                return {"ok": True, "card": card.to_dict()}, 200

            if action == "note":
                self.board_store.add_note(
                    card_id, author=body.get("author", "user") or "user",
                    text=body.get("text", "") or "",
                )
                card = self.board_store.get_card(card_id)
                return {"ok": True, "card": card.to_dict() if card else None}, 200

            if action == "delete":
                return {"ok": self.board_store.delete_card(card_id)}, 200

            if action == "run":
                if self.board_orchestrator is None:
                    return {"ok": False, "error": "board execution not available"}, 400
                card = self.board_store.get_card(card_id)
                if card is None:
                    return {"ok": False, "error": "card not found"}, 404
                # Fire-and-forget: the board reflects progress via polling. The
                # result lands on the card (deliver=False) — we do NOT relay it
                # into the origin conversation, so running a card from the board
                # UI never posts the answer as a chat message. Errors are logged.
                import asyncio as _asyncio

                def _log_done(t: "_asyncio.Task") -> None:
                    if not t.cancelled() and t.exception() is not None:
                        logger.error(f"[board] run_card {card_id} failed: {t.exception()}")

                _asyncio.ensure_future(
                    self.board_orchestrator.run_card(card_id, deliver=False)
                ).add_done_callback(_log_done)
                return {"ok": True, "status": "started", "card": card.to_dict()}, 200

            if action == "cancel":
                if self.board_orchestrator is not None:
                    await self.board_orchestrator.cancel_card(card_id)
                else:
                    card = self.board_store.get_card(card_id)
                    if card and card.run_id and self._subagent_manager is not None:
                        try:
                            await self._subagent_manager.cancel(card.run_id)
                        except Exception as exc:
                            logger.warning(f"[board] cancel subagent failed: {exc}")
                    from flowly.board.store import STATUS_CANCELLED
                    self.board_store.set_status(card_id, STATUS_CANCELLED)
                card = self.board_store.get_card(card_id)
                return {"ok": True, "card": card.to_dict() if card else None}, 200

            return {"ok": False, "error": f"unknown action: {action}"}, 400
        except BoardError as e:
            return {"ok": False, "error": str(e)}, 400
        except Exception as e:
            logger.error(f"Error applying board action {action!r}: {e}")
            return {"ok": False, "error": "Internal server error"}, 500

    async def _handle_provider_active(self, request: web.Request) -> web.Response:
        """Read-only: report which provider is currently serving requests.

        Lets the TUI surface "★ Flowly (me@gmail.com)" in a status badge
        without making the user open the integrations modal."""
        try:
            from flowly.config.loader import load_config
            from flowly.integrations.active_provider import resolve_active_provider
            active = resolve_active_provider(load_config())
            if active is None:
                return web.json_response({"ok": False, "error": "no provider"})
            return web.json_response({
                "ok": True,
                "key": active.key,
                "source": active.source,
                "api_base": active.api_base,
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _handle_voice_message(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            call_sid = data.get("call_sid", "")
            from_number = data.get("from", "")
            text = data.get("text", "")
            if not call_sid or len(call_sid) > 128:
                return web.json_response({"error": "Invalid call_sid"}, status=400)
            if len(text) > 50000:
                return web.json_response({"error": "Message too large"}, status=413)
            logger.info(f"Voice message from {from_number}: {text[:50]}...")
            if not self.on_voice_message:
                return web.json_response({"error": "Voice handler not configured"}, status=500)
            response = await asyncio.wait_for(
                self.on_voice_message(call_sid, from_number, text),
                timeout=30.0,
            )
            return web.json_response({"response": response})
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        except asyncio.TimeoutError:
            logger.error("Voice message handler timeout")
            return web.json_response({"error": "Handler timeout"}, status=504)
        except Exception as e:
            logger.error(f"Error handling voice message: {e}")
            return web.json_response({"error": "Internal server error"}, status=500)

    # ------------------------------------------------------------------
    # WebSocket handler — desktop chat protocol
    # ------------------------------------------------------------------

    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        """Handle a desktop WebSocket connection."""
        # Auth gate — only when a token is configured (remote/self-hosted). The
        # locally-spawned gateway (loopback, no token) is unchanged: the TUI and
        # local desktop connect exactly as before. When engaged: an anti-DNS-
        # rebinding Host/Origin check + a single-use ws-ticket (or raw token).
        if self._require_auth:
            if not host_origin_allowed(request):
                return web.Response(status=403, text="forbidden host/origin")
            if not self._ws_credential_ok(request):
                return web.Response(status=401, text="unauthorized")
        # 40 MB frame cap: chat.send carries attachments inline as base64 (a
        # 25 MB file inflates to ~33 MB encoded + JSON overhead). This is the
        # direct-gateway equivalent of the relay's CDN upload — base64-over-WS,
        # for the dashboard. The 25 MB per-file limit is enforced
        # client-side and again in _save_attachments.
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=40 * 1024 * 1024)

        # Desktop/extension clients reconnect aggressively — Electron tabs
        # opening/closing, network flips, extension reloads. If the client
        # bails out mid-handshake, aiohttp raises ClientConnectionResetError
        # from `ws.prepare()`. That's expected noise, not an error: the
        # socket is already gone, we just didn't get to finish the upgrade.
        # Swallow it at DEBUG level so real WS errors still surface via
        # the exception handler further down.
        #
        # We must return a *regular* Response here, not the unprepared `ws`.
        # aiohttp's finalizer calls `write_eof()` → `close()` on whatever we
        # return; on an unprepared WebSocketResponse that raises
        # `RuntimeError("Call .prepare() first")`, which surfaces as an
        # unhandled aiohttp error in the logs.
        try:
            await ws.prepare(request)
        except (
            ConnectionResetError,
            ConnectionAbortedError,
            asyncio.CancelledError,
        ) as e:
            logger.debug(
                f"[WS] Client disconnected before handshake completed: "
                f"{type(e).__name__}"
            )
            return web.Response(status=400)
        except Exception as e:
            # aiohttp surfaces ClientConnectionResetError which isn't in
            # the stdlib hierarchy — catch it by duck-typing name so we
            # don't introduce an aiohttp-version-specific import.
            if type(e).__name__ in (
                "ClientConnectionResetError",
                "ClientConnectionError",
            ):
                logger.debug(
                    f"[WS] Client disconnected before handshake completed: "
                    f"{type(e).__name__}"
                )
                return web.Response(status=400)
            raise

        # Honor a stable clientId from the query string when supplied — this
        # lets the desktop reattach to its previous coaching session after a
        # transient disconnect (network blip, sleep/wake, gateway restart)
        # instead of starting a fresh `coaching:{new_uuid}` and losing the
        # in-memory transcript. Validation: must look like a UUID hex string
        # and be reasonably short, to keep this from becoming a vector for
        # arbitrary log-injection or session-id collision attacks.
        requested_id = request.rel_url.query.get("clientId", "").strip()
        # ASCII-only [a-zA-Z0-9_-]{1,64} — Python's str.isalnum is unicode-aware
        # which would let through e.g. "üñıç". Keep it strictly ASCII.
        _id_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if (
            requested_id
            and len(requested_id) <= 64
            and all(c in _id_chars for c in requested_id)
        ):
            client_id = requested_id
            # If two connections claim the same id, keep the newest — the old
            # WS is almost certainly already broken (this is exactly the
            # reconnect case we're trying to support). Drop the stale ref.
            if client_id in self._ws_clients:
                logger.info(
                    f"[WS] Reattaching client_id={client_id}: replacing stale ws"
                )
        else:
            client_id = str(uuid.uuid4())
        self._ws_clients[client_id] = ws
        logger.info(f"[WS] Desktop client connected: {client_id}")

        try:
            async for raw_msg in ws:
                if raw_msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(raw_msg.data)
                    except json.JSONDecodeError:
                        await self._ws_send(ws, {"type": "error", "message": "Invalid JSON"})
                        continue
                    msg_type = data.get("type")
                    if msg_type == "rpc":
                        await self._handle_ws_rpc(ws, client_id, data)
                    elif msg_type == "ping":
                        await self._ws_send(ws, {"type": "pong", "timestamp": data.get("timestamp")})
                    elif msg_type == "tool_result":
                        # Fix #10: Only accept tool_result from registered extension clients
                        if client_id in self._extension_clients:
                            self._handle_extension_tool_result(data)
                        else:
                            logger.warning(f"[WS] tool_result from non-extension client {client_id}, ignoring")
                elif raw_msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        except Exception as e:
            logger.error(f"[WS] Client {client_id} error: {e}")
        finally:
            self._ws_clients.pop(client_id, None)
            # Drop any session→ws bindings that pointed at this closed socket so
            # we don't hold a dead ref (a live re-entry re-binds via chat.inflight
            # anyway; _ws_send already no-ops on a closed socket).
            for _sk in [k for k, v in self._session_ws.items() if v is ws]:
                self._session_ws.pop(_sk, None)
            if client_id in self._extension_clients:
                self._extension_clients.discard(client_id)
                # Fix #7: Cancel all pending futures for this extension
                cancelled = 0
                for req_id, future in list(self._extension_pending.items()):
                    if not future.done():
                        future.set_result({"error": "Extension disconnected"})
                        cancelled += 1
                    self._extension_pending.pop(req_id, None)
                # Fix #6: Fall back to another extension if available
                if self._extension_active == client_id:
                    self._extension_active = next(iter(self._extension_clients), None)
                logger.info(
                    f"[WS] Browser extension disconnected: {client_id} "
                    f"(cancelled {cancelled} pending, remaining: {len(self._extension_clients)})"
                )
            else:
                logger.info(f"[WS] Desktop client disconnected: {client_id}")

            # Auto-stop any coaching session owned by this WS so background
            # finalization still runs (summary, KG, artifact).
            if self._coaching_manager:
                coaching_sid = f"coaching:{client_id}"
                if self._coaching_manager.is_active(coaching_sid):
                    try:
                        await self._coaching_manager.stop(
                            coaching_sid, background_finalize=True
                        )
                        logger.info(
                            f"[WS] auto-stopped coaching session {coaching_sid} "
                            f"on disconnect"
                        )
                    except Exception as e:
                        logger.warning(f"[WS] coaching auto-stop failed: {e}")

        return ws

    async def _handle_ws_rpc(self, ws: web.WebSocketResponse, client_id: str, data: dict) -> None:
        """Dispatch an RPC call to the appropriate handler."""
        method = data.get("method", "")
        rpc_id = data.get("id", "")
        params = data.get("params") or {}

        try:
            if method == "health":
                await self._ws_rpc_reply(ws, rpc_id, {"ok": True})

            # Shared feature surface (connections, config, memory, kg, sessions,
            # audit, persona, provider, skills, assistants, pairing) — the SAME
            # flowly.channels.feature_rpc handlers the relay serves, so every
            # client sees one shape over either transport. Checked first so the
            # unified (superset) shapes win over the legacy native handlers.
            elif method in feature_rpc.FEATURE_METHODS:
                await self._handle_feature_rpc(ws, rpc_id, method, params)

            elif method == "sessions.list":
                await self._ws_rpc_sessions_list(ws, rpc_id, params)

            elif method == "sessions.delete":
                await self._ws_rpc_sessions_delete(ws, rpc_id, params)

            elif method == "chat.history":
                await self._ws_rpc_chat_history(ws, rpc_id, params)

            elif method == "chat.send":
                await self._ws_rpc_chat_send(ws, client_id, rpc_id, params)

            elif method == "chat.abort":
                await self._ws_rpc_chat_abort(ws, rpc_id, params)

            elif method == "subagents.list":
                await self._ws_rpc_subagents_list(ws, rpc_id, params)

            elif method == "subagents.cancel":
                await self._ws_rpc_subagents_cancel(ws, rpc_id, params)

            # P2.8 — Assistant registry (Desktop Agents tab "Your agents" section)
            elif method == "assistants.list":
                await self._ws_rpc_assistants_list(ws, rpc_id, params)

            elif method == "assistants.reload":
                await self._ws_rpc_assistants_reload(ws, rpc_id, params)

            elif method == "exec.approval.resolve":
                await self._ws_rpc_exec_approval_resolve(ws, rpc_id, params)

            elif method == "exec.approval.list":
                await self._ws_rpc_exec_approval_list(ws, rpc_id, params)

            elif method == "agent.clarify.resolve":
                await self._ws_rpc_clarify_resolve(ws, rpc_id, params)

            elif method == "agent.clarify.list":
                await self._ws_rpc_clarify_list(ws, rpc_id, params)

            elif method == "exec.policy.get":
                await self._ws_rpc_exec_policy_get(ws, rpc_id, params)

            elif method == "exec.policy.set":
                await self._ws_rpc_exec_policy_set(ws, rpc_id, params)

            elif method == "exec.policy.allowlist.remove":
                await self._ws_rpc_exec_policy_allowlist_remove(ws, rpc_id, params)

            # Board
            elif method == "board.snapshot":
                await self._ws_rpc_board_snapshot(ws, rpc_id, params)
            elif method == "board.action":
                await self._ws_rpc_board_action(ws, rpc_id, params)

            # Artifacts
            elif method == "artifacts.list":
                await self._ws_rpc_artifacts_list(ws, rpc_id, params)
            elif method == "artifacts.get":
                await self._ws_rpc_artifacts_get(ws, rpc_id, params)
            elif method == "artifacts.create":
                await self._ws_rpc_artifacts_create(ws, rpc_id, params)
            elif method == "artifacts.update":
                await self._ws_rpc_artifacts_update(ws, rpc_id, params)
            elif method == "artifacts.delete":
                await self._ws_rpc_artifacts_delete(ws, rpc_id, params)
            elif method == "artifacts.pin":
                await self._ws_rpc_artifacts_pin(ws, rpc_id, params)
            elif method == "artifacts.versions":
                await self._ws_rpc_artifacts_versions(ws, rpc_id, params)

            # Chat commands (slash commands)
            elif method == "chat.compact":
                await self._ws_rpc_chat_compact(ws, rpc_id, params)
            elif method == "chat.clear":
                await self._ws_rpc_chat_clear(ws, rpc_id, params)
            elif method == "chat.retry":
                await self._ws_rpc_chat_retry(ws, rpc_id, params)
            elif method == "chat.undo":
                await self._ws_rpc_chat_undo(ws, rpc_id, params)

            # Meeting Coach — continuous listening
            elif method == "coaching.start":
                await self._ws_rpc_coaching_start(ws, client_id, rpc_id, params)
            elif method == "coaching.segment":
                await self._ws_rpc_coaching_segment(ws, client_id, rpc_id, params)
            elif method == "coaching.askNow":
                await self._ws_rpc_coaching_ask_now(ws, client_id, rpc_id, params)
            elif method == "coaching.stop":
                await self._ws_rpc_coaching_stop(ws, client_id, rpc_id, params)
            elif method == "coaching.state":
                await self._ws_rpc_coaching_state(ws, client_id, rpc_id, params)
            elif method == "coaching.snapshot":
                await self._ws_rpc_coaching_snapshot(ws, client_id, rpc_id, params)
            elif method == "coaching.update":
                await self._ws_rpc_coaching_update(ws, client_id, rpc_id, params)

            # Audit log — Activity tab history
            elif method == "audit.list":
                await self._ws_rpc_audit_list(ws, rpc_id, params)
            elif method == "audit.stats":
                await self._ws_rpc_audit_stats(ws, rpc_id, params)
            elif method == "audit.clear":
                await self._ws_rpc_audit_clear(ws, rpc_id, params)

            # Slash command catalogue — built-ins + plugin-registered +
            # bundle aliases. Desktop composer reads this on demand to
            # power the ``/`` autocomplete dropdown.
            elif method == "commands.list":
                await self._ws_rpc_commands_list(ws, rpc_id, params)

            # Browser extension registration
            elif method == "extension.register":
                self._extension_clients.add(client_id)
                self._extension_active = client_id
                logger.info(f"[WS] Browser extension registered: {client_id} (total: {len(self._extension_clients)})")
                await self._ws_rpc_reply(ws, rpc_id, {"ok": True})

            else:
                await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", f"Unknown method: {method}")
        except Exception as e:
            logger.error(f"[WS] RPC {method} error: {e}")
            await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", str(e))

    # --- RPC: exec.approval ---

    async def _ws_rpc_exec_approval_resolve(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        approval_id = params.get("id", "")
        decision = params.get("decision", "")
        if decision not in ("allow-once", "allow-always", "deny"):
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "Invalid decision")
            return

        from flowly.exec.approval_manager import get_approval_manager
        manager = get_approval_manager()
        if manager.resolve(approval_id, decision):
            await self._ws_rpc_reply(ws, rpc_id, {"ok": True})
        else:
            await self._ws_rpc_error(ws, rpc_id, "NOT_FOUND", "Approval not found or expired")

    async def _ws_rpc_exec_approval_list(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        from flowly.exec.approval_manager import get_approval_manager
        manager = get_approval_manager()
        pending = manager.list_pending()
        items = [{
            "id": p.id,
            "command": p.request.command,
            "sessionKey": p.session_key,
            "expiresAt": p.expires_at,
            "supportsAlways": getattr(p, "supports_always", True),
        } for p in pending]
        await self._ws_rpc_reply(ws, rpc_id, {"approvals": items})

    # --- RPC: agent.clarify ---

    async def _ws_rpc_clarify_resolve(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        clarify_id = params.get("id", "")
        answer = params.get("answer", "")
        if not clarify_id:
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "Missing id")
            return
        if not isinstance(answer, str):
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "Invalid answer")
            return

        from flowly.clarify.manager import get_clarify_manager
        manager = get_clarify_manager()
        if manager.resolve(clarify_id, answer):
            await self._ws_rpc_reply(ws, rpc_id, {"ok": True})
        else:
            await self._ws_rpc_error(ws, rpc_id, "NOT_FOUND", "Clarify not found or expired")

    async def _ws_rpc_clarify_list(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        from flowly.clarify.manager import get_clarify_manager
        manager = get_clarify_manager()
        items = [{
            "id": p.id,
            "question": p.question,
            "choices": p.choices,
            "sessionKey": p.session_key,
            "expiresAt": p.expires_at,
        } for p in manager.list_pending()]
        await self._ws_rpc_reply(ws, rpc_id, {"clarifies": items})

    async def broadcast_clarify_request(
        self,
        clarify_id: str,
        question: str,
        choices: list[str] | None,
        session_key: str | None,
        expires_at: float,
    ) -> None:
        """Push an agent clarify request to all connected desktop/web clients."""
        event = {
            "type": "event",
            "event": "agent.clarify.requested",
            "data": {
                "id": clarify_id,
                "question": question,
                "choices": choices,
                "sessionKey": session_key,
                "expiresAt": expires_at,
            },
        }
        for ws in list(self._ws_clients.values()):
            await self._ws_send(ws, event)

    # --- RPC: exec.policy (standing approval policy) ---

    @staticmethod
    def _exec_policy_payload(store) -> dict:
        cfg = store.config
        return {
            "security": cfg.security,
            "ask": cfg.ask,
            "allowlist": [
                {
                    "pattern": e.pattern,
                    "command": e.last_used_command,
                    "lastUsedAt": e.last_used_at,
                }
                for e in cfg.allowlist
            ],
        }

    async def _ws_rpc_exec_policy_get(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        from flowly.exec.approvals import ExecApprovalStore
        store = ExecApprovalStore()
        store.load()
        await self._ws_rpc_reply(ws, rpc_id, self._exec_policy_payload(store))

    async def _ws_rpc_exec_policy_set(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        security = params.get("security")
        ask = params.get("ask")
        if security is not None and security not in ("deny", "allowlist", "full"):
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "Invalid security")
            return
        if ask is not None and ask not in ("off", "on-miss", "always"):
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "Invalid ask")
            return
        if security is None and ask is None:
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "Nothing to set")
            return

        from flowly.exec.approvals import ExecApprovalStore
        store = ExecApprovalStore()
        cfg = store.load()
        if security is not None:
            cfg.security = security
        if ask is not None:
            cfg.ask = ask
        store.save()
        await self._ws_rpc_reply(ws, rpc_id, self._exec_policy_payload(store))

    async def _ws_rpc_exec_policy_allowlist_remove(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        pattern = params.get("pattern", "")
        if not pattern:
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "Missing pattern")
            return
        from flowly.exec.approvals import ExecApprovalStore
        store = ExecApprovalStore()
        store.load()
        removed = store.remove_from_allowlist(pattern)
        if removed:
            store.save()
        payload = self._exec_policy_payload(store)
        payload["removed"] = removed
        await self._ws_rpc_reply(ws, rpc_id, payload)

    # --- RPC: audit.* ---

    def _audit_dir(self) -> Path:
        """Return the active profile's audit directory."""
        from flowly.profile import get_flowly_home
        return get_flowly_home() / "audit"

    async def _ws_rpc_audit_list(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        """List audit entries with filter + pagination."""
        from flowly.audit.reader import read_entries

        date = params.get("date") or None
        tool = params.get("tool") or None
        status = params.get("status") or None
        search = params.get("search") or None
        try:
            limit = int(params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = int(params.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0

        result = read_entries(
            self._audit_dir(),
            date=date,
            tool=tool,
            status=status,
            search=search,
            limit=limit,
            offset=offset,
        )
        await self._ws_rpc_reply(ws, rpc_id, result)

    async def _ws_rpc_audit_stats(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        """Folder-level stats for the Activity footer."""
        from flowly.audit.reader import get_stats
        from flowly.config.loader import load_config

        stats = get_stats(self._audit_dir())
        try:
            cfg = load_config()
            stats["retention_days"] = cfg.audit.retention_days
            stats["max_size_mb"] = cfg.audit.max_size_mb
            stats["enabled"] = cfg.audit.enabled
        except Exception:
            # Config loading is best-effort here; stats stand on their own.
            pass
        await self._ws_rpc_reply(ws, rpc_id, stats)

    async def _ws_rpc_audit_clear(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        """Delete every audit file — confirmation lives in the desktop UI."""
        if not params.get("confirm"):
            await self._ws_rpc_error(
                ws, rpc_id, "INVALID_REQUEST",
                "Missing confirm=true",
            )
            return

        from flowly.audit.reader import clear_audit_logs
        result = clear_audit_logs(self._audit_dir())
        await self._ws_rpc_reply(ws, rpc_id, result)

    async def broadcast_approval_request(self, approval_id: str, command: str, session_key: str | None, expires_at: float, supports_always: bool = True) -> None:
        """Push an exec approval request to all connected desktop/web clients."""
        event = {
            "type": "event",
            "event": "exec.approval.requested",
            "data": {
                "id": approval_id,
                "command": command,
                "sessionKey": session_key,
                "expiresAt": expires_at,
                "supportsAlways": supports_always,
            },
        }
        for ws in list(self._ws_clients.values()):
            await self._ws_send(ws, event)

    # --- RPC: commands.list ---

    async def _ws_rpc_commands_list(
        self, ws: web.WebSocketResponse, rpc_id: str, params: dict,
    ) -> None:
        """Catalogue every ``/`` command the user can type in chat.

        Categories, each sorted alphabetically:
          * ``builtin`` — hardcoded handlers in ``agent/loop.py``
            (``/help``, ``/compact``, ``/clear``, ``/new``)
          * ``plugin`` — slash commands registered by plugins via
            ``ctx.register_slash_command(...)``
          * ``bundle`` — user-authored YAML aliases that expand to a
            multi-skill prompt
          * ``skill`` — installed skills invokable as one-turn ``/skill`` prompts

        The desktop composer reads this once per session, caches the
        result, and renders the ``/`` autocomplete dropdown grouped by
        category. Plugin failure (manager not wired, plugin crashed
        during introspection) is non-fatal — the call always returns
        at least the built-ins so the dropdown stays useful.
        """
        from flowly.agent.skill_bundles import build_commands_catalogue
        await self._ws_rpc_reply(ws, rpc_id, build_commands_catalogue())

    # --- RPC: subagents.list ---

    async def _ws_rpc_board_snapshot(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        """Return the board snapshot for the TUI's inline /board view."""
        if self.board_store is None:
            await self._ws_rpc_reply(ws, rpc_id, {"snapshot": None})
            return
        try:
            await self._ws_rpc_reply(ws, rpc_id, {"snapshot": self.board_store.snapshot()})
        except Exception as e:
            await self._ws_rpc_error(ws, rpc_id, "board_error", str(e))

    async def _ws_rpc_board_action(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        """Apply a board action from the TUI (/board run|del|done|cancel|add)."""
        result, _status = await self._apply_board_action(params or {})
        if result.get("ok"):
            await self._ws_rpc_reply(ws, rpc_id, result)
        else:
            await self._ws_rpc_error(ws, rpc_id, "board_error", result.get("error", "board action failed"))

    async def _ws_rpc_subagents_list(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        records = []
        if self.subagent_registry:
            self.subagent_registry._load_from_disk()
            records = self.subagent_registry.all()

        # Also include delegate tool running tasks
        if self._delegate_tool:
            for run_id, info in self._delegate_tool._running.items():
                records.append(type('R', (), {
                    'run_id': run_id,
                    'label': info.get('label', f'@{info.get("agent_id", "?")}'),
                    'task': info.get('task', ''),
                    'model': info.get('model', ''),
                    'outcome': None,
                    'created_at': info.get('started_at', 0),
                    'started_at': info.get('started_at', 0),
                    'ended_at': None,
                    'error': None,
                    'parent_session_key': '',
                })())

        status_filter = params.get("status")
        if status_filter == "running":
            records = [r for r in records if r.ended_at is None]
        elif status_filter == "completed":
            records = [r for r in records if r.outcome == "ok"]
        elif status_filter == "failed":
            records = [r for r in records if r.outcome in ("error", "timeout")]

        import time as _time
        tasks = []
        for r in sorted(records, key=lambda x: x.created_at, reverse=True):
            duration = None
            if r.started_at and r.ended_at:
                duration = round(r.ended_at - r.started_at, 1)
            elif r.started_at:
                duration = round(_time.time() - r.started_at, 1)

            tasks.append({
                "runId": r.run_id,
                "label": r.label,
                "task": r.task,
                "model": r.model,
                "status": "running" if r.ended_at is None else (r.outcome or "unknown"),
                "duration": duration,
                "createdAt": r.created_at,
                "endedAt": r.ended_at,
                "error": r.error,
                "parentSessionKey": r.parent_session_key,
            })

        await self._ws_rpc_reply(ws, rpc_id, {"tasks": tasks})

    # --- RPC: subagents.cancel ---

    async def _ws_rpc_subagents_cancel(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        run_id = params.get("runId", "")
        if not run_id:
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "runId is required")
            return

        if not self._subagent_manager:
            await self._ws_rpc_error(ws, rpc_id, "NOT_AVAILABLE", "Subagent manager not available")
            return

        result_json = await self._subagent_manager.cancel(run_id)
        import json as _json
        result = _json.loads(result_json)
        await self._ws_rpc_reply(ws, rpc_id, result)

    # --- RPC: assistants.list / assistants.reload (P2.8) ---
    # Desktop UI's "Agents" tab reads these to populate the "Your agents"
    # section. Desktop writes the .md files directly via Node fs; when
    # it's done it calls assistants.reload so Python picks up the new
    # file without a gateway restart.

    async def _ws_rpc_assistants_list(
        self, ws: web.WebSocketResponse, rpc_id: str, params: dict,
    ) -> None:
        registry = getattr(self, "_assistant_registry", None)
        if registry is None:
            await self._ws_rpc_error(
                ws, rpc_id, "NOT_AVAILABLE", "Assistant registry not wired",
            )
            return
        assistants = []
        for a in registry.all():
            assistants.append({
                "name": a.name,
                "description": a.description,
                "model": a.model,
                "allowedTools": sorted(a.allowed_tools) if a.allowed_tools else None,
                "autoSaveArtifact": a.auto_save_artifact,
                "artifactType": a.artifact_type,
                "systemPrompt": a.system_prompt,
                "builtin": a.builtin,
                "sourcePath": str(a.source_path) if a.source_path else None,
            })
        await self._ws_rpc_reply(ws, rpc_id, {"assistants": assistants})

    async def _ws_rpc_assistants_reload(
        self, ws: web.WebSocketResponse, rpc_id: str, params: dict,
    ) -> None:
        registry = getattr(self, "_assistant_registry", None)
        if registry is None:
            await self._ws_rpc_error(
                ws, rpc_id, "NOT_AVAILABLE", "Assistant registry not wired",
            )
            return
        report = registry.reload()
        await self._ws_rpc_reply(ws, rpc_id, report.to_dict())

    # --- RPC: coaching.* (meeting coach) ---

    def _coaching_session_id(self, client_id: str, params: dict) -> str:
        """Derive a session id. Default: per-WS-client so each desktop connection
        is its own meeting. Allow override via params.sessionId for flexibility."""
        override = params.get("sessionId") or params.get("session_id")
        if override and isinstance(override, str):
            return override
        return f"coaching:{client_id}"

    async def _ws_rpc_coaching_start(
        self, ws: web.WebSocketResponse, client_id: str, rpc_id: str, params: dict
    ) -> None:
        if not self._coaching_manager:
            await self._ws_rpc_error(ws, rpc_id, "NOT_AVAILABLE", "Coaching not configured")
            return

        session_id = self._coaching_session_id(client_id, params)
        context = str(params.get("context", "") or "")
        language = str(params.get("language", "auto") or "auto")
        frequency = str(params.get("frequency", "moderate") or "moderate")

        # Start (or reconfigure) the session BEFORE registering callbacks,
        # so callback registration targets the now-existing session.
        result = await self._coaching_manager.start(
            session_id=session_id,
            user_context=context,
            language=language,
            frequency=frequency,
        )
        if result.get("status") == "at_capacity":
            await self._ws_rpc_error(
                ws, rpc_id, "CAPACITY",
                f"Max {result.get('limit')} concurrent coaching sessions",
            )
            return

        import time as _time

        # Build callbacks bound to this specific WS. Weak-referencing ws is
        # unnecessary because the session is cleaned up on stop/disconnect.
        # `seq` and `timestamp` are kwargs from the manager (post-snapshot
        # refactor); they propagate into the event payload so clients can
        # deduplicate live events against rehydrated snapshots.
        async def _tip_cb(sid: str, tip_text: str, confidence: float, *, seq: int = -1, timestamp: float | None = None) -> None:
            if ws.closed:
                return
            await self._ws_send(ws, {
                "type": "event",
                "event": "coaching.tip",
                "data": {
                    "sessionId": sid,
                    "text": tip_text,
                    "confidence": confidence,
                    "timestamp": timestamp if timestamp is not None else _time.time(),
                    "seq": seq,
                },
            })

        async def _transcript_cb(sid: str, text: str, source: str, *, seq: int = -1) -> None:
            if ws.closed:
                return
            await self._ws_send(ws, {
                "type": "event",
                "event": "coaching.transcript",
                "data": {"sessionId": sid, "text": text, "source": source, "seq": seq},
            })

        async def _finalized_cb(sid: str, summary: dict) -> None:
            if ws.closed:
                return
            await self._ws_send(ws, {
                "type": "event",
                "event": "coaching.finalized",
                "data": {"sessionId": sid, **summary},
            })

        async def _gate_decision_cb(sid: str, **payload) -> None:
            """Forward gate-decision events to the desktop renderer for the
            Diagnostics panel. Best-effort — never raises."""
            if ws.closed:
                return
            await self._ws_send(ws, {
                "type": "event",
                "event": "coaching.gate_decision",
                "data": {"sessionId": sid, "timestamp": _time.time(), **payload},
            })

        self._coaching_manager.on_tip(session_id, _tip_cb)
        self._coaching_manager.on_transcript(session_id, _transcript_cb)
        self._coaching_manager.on_finalized(session_id, _finalized_cb)
        self._coaching_manager.on_gate_decision(session_id, _gate_decision_cb)

        result["sessionId"] = session_id
        await self._ws_rpc_reply(ws, rpc_id, result)

    async def _ws_rpc_coaching_segment(
        self, ws: web.WebSocketResponse, client_id: str, rpc_id: str, params: dict
    ) -> None:
        if not self._coaching_manager:
            await self._ws_rpc_error(ws, rpc_id, "NOT_AVAILABLE", "Coaching not configured")
            return

        session_id = self._coaching_session_id(client_id, params)
        # Transcript is produced client-side (desktop → web-app STT); gateway
        # only sees text. Accept both "text" (new) and "transcript" (legacy).
        text = params.get("text") or params.get("transcript") or ""
        source = str(params.get("source", "mic") or "mic")
        if source not in ("mic", "system"):
            source = "mic"
        if not text or not isinstance(text, str):
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "text is required")
            return

        # Faz E: optional base64 JPEG of the user's screen at commit
        # time. Desktop's smart-trigger decides when to attach; we
        # forward whatever arrives. Defense-in-depth checks (type +
        # length) so a misbehaving client can't OOM the bot process.
        raw_screenshot = params.get("screenshot")
        screenshot_b64: str | None = raw_screenshot if isinstance(raw_screenshot, str) else None
        if screenshot_b64 is not None and len(screenshot_b64) > 1_500_000:
            # Desktop ships PNG at the 1568 px Anthropic ceiling. UI
            # screenshots compress to 200-500 KB raw → ~270-670 KB
            # base64; 1.5 MB cap leaves headroom for busy / text-heavy
            # screens without admitting genuinely malformed payloads.
            # Bot still receives the transcript on overflow, just no
            # image.
            logger.warning(
                f"[Coach] segment session={session_id} screenshot too large "
                f"({len(screenshot_b64)} bytes); dropping image"
            )
            screenshot_b64 = None
        logger.info(
            f"[Coach] segment.rpc session={session_id} "
            f"text_len={len(text)} source={source} "
            f"screenshot={'present(' + str(len(screenshot_b64)) + 'b)' if screenshot_b64 else 'none'}"
        )

        result = await self._coaching_manager.add_transcript(
            session_id=session_id,
            text=text,
            source=source,
            screenshot_b64=screenshot_b64,
        )
        await self._ws_rpc_reply(ws, rpc_id, result)

    async def _ws_rpc_coaching_ask_now(
        self, ws: web.WebSocketResponse, client_id: str, rpc_id: str, params: dict
    ) -> None:
        """Hotkey-triggered "force a tip" path. Skips gate1; runs gate2
        directly against the session's cached transcript + the
        (optionally fresh) screenshot.

        Defense-in-depth: bound the screenshot length same as the
        regular segment RPC. The backend schema already caps at 500 KB
        but a misbehaving client could OOM the bot before reaching
        backend.
        """
        if not self._coaching_manager:
            await self._ws_rpc_error(ws, rpc_id, "NOT_AVAILABLE", "Coaching not configured")
            return

        session_id = self._coaching_session_id(client_id, params)

        raw_screenshot = params.get("screenshot")
        screenshot_b64: str | None = raw_screenshot if isinstance(raw_screenshot, str) else None
        if screenshot_b64 is not None and len(screenshot_b64) > 1_500_000:
            # See coaching.segment for the rationale on the 1.5 MB cap.
            logger.warning(
                f"[Coach] askNow session={session_id} screenshot too large "
                f"({len(screenshot_b64)} bytes); dropping image"
            )
            screenshot_b64 = None
        logger.info(
            f"[Coach] askNow.rpc session={session_id} "
            f"screenshot={'present(' + str(len(screenshot_b64)) + 'b)' if screenshot_b64 else 'none'}"
        )

        result = await self._coaching_manager.ask_now(
            session_id=session_id,
            screenshot_b64=screenshot_b64,
        )
        await self._ws_rpc_reply(ws, rpc_id, result)

    async def _ws_rpc_coaching_stop(
        self, ws: web.WebSocketResponse, client_id: str, rpc_id: str, params: dict
    ) -> None:
        if not self._coaching_manager:
            await self._ws_rpc_error(ws, rpc_id, "NOT_AVAILABLE", "Coaching not configured")
            return
        session_id = self._coaching_session_id(client_id, params)
        result = await self._coaching_manager.stop(session_id)
        result["sessionId"] = session_id
        await self._ws_rpc_reply(ws, rpc_id, result)

    async def _ws_rpc_coaching_state(
        self, ws: web.WebSocketResponse, client_id: str, rpc_id: str, params: dict
    ) -> None:
        if not self._coaching_manager:
            await self._ws_rpc_reply(ws, rpc_id, {"active": False})
            return
        session_id = self._coaching_session_id(client_id, params)
        info = self._coaching_manager.session_info(session_id)
        if info:
            await self._ws_rpc_reply(ws, rpc_id, {"active": True, **info})
        else:
            await self._ws_rpc_reply(ws, rpc_id, {"active": False})

    async def _ws_rpc_coaching_snapshot(
        self, ws: web.WebSocketResponse, client_id: str, rpc_id: str, params: dict
    ) -> None:
        """Full session snapshot for client rehydration.

        Unlike `coaching.state` (counts only), this returns the actual
        transcript segments and tip records buffered in memory. Clients
        use this on reconnect / page navigation to rebuild the live UI
        without losing what already happened.
        """
        if not self._coaching_manager:
            await self._ws_rpc_reply(ws, rpc_id, {"active": False})
            return
        session_id = self._coaching_session_id(client_id, params)
        snap = self._coaching_manager.session_snapshot(session_id)
        if snap:
            await self._ws_rpc_reply(ws, rpc_id, {"active": True, **snap})
        else:
            await self._ws_rpc_reply(ws, rpc_id, {"active": False, "session_id": session_id})

    async def _ws_rpc_coaching_update(
        self, ws: web.WebSocketResponse, client_id: str, rpc_id: str, params: dict
    ) -> None:
        """Live-update an in-progress coaching session (context/frequency/lang)."""
        if not self._coaching_manager:
            await self._ws_rpc_error(ws, rpc_id, "NOT_AVAILABLE", "Coaching not configured")
            return
        session_id = self._coaching_session_id(client_id, params)
        user_context = params.get("context")
        frequency = params.get("frequency")
        language = params.get("language")
        result = await self._coaching_manager.update_session(
            session_id,
            user_context=user_context if isinstance(user_context, str) else None,
            frequency=frequency if isinstance(frequency, str) else None,
            language=language if isinstance(language, str) else None,
        )
        await self._ws_rpc_reply(ws, rpc_id, result)

    # --- RPC: sessions.list ---

    async def _ws_rpc_sessions_list(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.sessions:
            await self._ws_rpc_reply(ws, rpc_id, {"sessions": []})
            return

        raw_sessions = self.sessions.list_sessions()
        sessions = []
        for s in raw_sessions:
            key = s.get("key", "")
            # Prefer the auto-generated descriptive title (the SAME one CLI and
            # desktop see); fall back to the key suffix only until the session
            # has been titled after its first exchange.
            title = s.get("title")
            sessions.append({
                "key": key,
                "displayName": title or (key.split(":", 1)[-1] if ":" in key else key),
                "title": title,
                "createdAt": s.get("created_at"),
                "updatedAt": s.get("updated_at"),
            })

        limit = params.get("limit")
        if limit and isinstance(limit, int):
            sessions = sessions[:limit]

        await self._ws_rpc_reply(ws, rpc_id, {"sessions": sessions})

    # --- RPC: sessions.delete ---

    async def _ws_rpc_sessions_delete(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        session_key = params.get("sessionKey", "")
        if not session_key or not self.sessions:
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "sessionKey is required")
            return

        deleted = self.sessions.delete(session_key)
        # Drop the per-session cwd pin too — otherwise a future session
        # that happens to reuse this session_key would inherit the
        # deleted conversation's working directory.
        from flowly.runtime_cwd import clear_session_cwd
        clear_session_cwd(session_key)
        await self._ws_rpc_reply(ws, rpc_id, {"deleted": deleted, "sessionKey": session_key})

    # --- RPC: chat.history ---

    async def _ws_rpc_chat_history(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        session_key = params.get("sessionKey", "")
        if not session_key or not self.sessions:
            await self._ws_rpc_reply(ws, rpc_id, {"sessionKey": session_key, "messages": []})
            return

        session = self.sessions.get_or_create(session_key)
        # Return full messages (not just role/content) for richer UI rendering.
        # We also pass the tool-protocol fields (tool_calls on assistant turns,
        # tool_call_id/name on tool results) so a client with no Firestore — the
        # direct gateway desktop — can RECONSTRUCT the live tool-turn panel from
        # history alone, matching what it saw streaming. The relay reads those
        # from its tool_turns/ subcollection instead; here the session jsonl IS
        # the source of truth.
        #
        # Read the append-only DISPLAY transcript, not session.messages: the
        # latter is the compacted LLM working context (just [summary]+recent
        # after a /compact), which would drop the early turns from the chat UI.
        source_messages = self.sessions.get_full_messages(session_key)
        messages = []
        for m in source_messages:
            msg: dict[str, Any] = {"role": m["role"]}
            # Normalise content into array format expected by the iOS/desktop protocol.
            content_raw = m.get("content", "")
            if isinstance(content_raw, str):
                msg["content"] = [{"type": "text", "text": content_raw}]
            elif isinstance(content_raw, list):
                msg["content"] = content_raw
            else:
                msg["content"] = [{"type": "text", "text": str(content_raw)}]
            if m.get("tool_calls"):
                msg["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                msg["tool_call_id"] = m["tool_call_id"]
            if m.get("name"):
                msg["name"] = m["name"]
            # Reconstruct attachment previews from the media paths saved on the
            # user turn. Images get a small base64 JPEG thumbnail (not the full
            # original — that could be 25 MB) so the desktop shows the same
            # bubble preview it did live, with no Firestore and no bloated
            # history payload. Best-effort per file.
            # Media on this turn — user-attached inbound files AND media the agent
            # produced (image_generate / screenshot, saved on the assistant turn).
            # Use the inline-thumbnail shape so a remote client (iOS / desktop)
            # renders the preview from history with no fetch — matching the live
            # reply path. (mediaId is still set for clients that want full-res.)
            media_paths = m.get("media")
            if isinstance(media_paths, list) and media_paths:
                atts = _reply_media_attachments(media_paths)
                if atts:
                    msg["attachments"] = atts
            if "timestamp" in m:
                msg["timestamp"] = m["timestamp"]
            if "usage" in m:
                msg["usage"] = m["usage"]
            messages.append(msg)

        await self._ws_rpc_reply(ws, rpc_id, {
            "sessionKey": session_key,
            "sessionId": session_key,
            "messages": messages,
            "thinkingLevel": session.metadata.get("thinkingLevel"),
        })

    # --- RPC: chat.send ---

    async def _ws_rpc_chat_send(
        self, ws: web.WebSocketResponse, client_id: str, rpc_id: str, params: dict,
    ) -> None:
        message = params.get("message", "")
        attachments = params.get("attachments") or []
        if not message and not attachments:
            await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "Empty message")
            return

        session_key = params.get("sessionKey") or f"desktop:{client_id}"
        idempotency_key = params.get("idempotencyKey") or str(uuid.uuid4())
        run_id = idempotency_key

        # Optional per-session runtime cwd (Desktop/TUI may send the
        # project folder the user opened). Pin it before the run so the
        # agent's exec / codex tools resolve to it via session_key. Omit
        # → falls through to FLOWLY_CWD / config / workspace. Invalid →
        # hard error rather than silently running in the wrong place.
        cwd = params.get("cwd")
        if cwd:
            from flowly.runtime_cwd import set_session_cwd
            try:
                set_session_cwd(session_key, cwd)
                logger.info(
                    f"[GatewayWS] chat.send pinned cwd={cwd} for session={session_key}"
                )
            except ValueError:
                # Defensive: a client may ship a path that doesn't exist
                # on this host (older client without per-kind cwd gating,
                # or a renamed/deleted folder).  Hard-rejecting the chat
                # would lose the user's message; drop the cwd silently
                # and fall through the resolve_runtime_cwd chain
                # (FLOWLY_CWD / config / workspace).
                logger.warning(
                    f"[GatewayWS] chat.send cwd={cwd!r} not a valid "
                    f"directory on this host; ignoring and falling "
                    f"back to workspace (session={session_key})"
                )

        # P4 — opt-in voice mode. iOS push-to-talk sends `voiceMode: true`;
        # everything else omits the field and falls through as text.
        # `.get(..., False)` is strict-validation-free so an old bot
        # ignores this silently (forward compat already verified).
        voice_mode = bool(params.get("voiceMode", False))

        # Save attachments to disk
        media: list[str] = []
        if attachments:
            media_dir = get_flowly_home() / "media"
            media = _save_attachments(attachments, media_dir)

        # ACK immediately with runId so the client can track the run.
        await self._ws_rpc_reply(ws, rpc_id, {"runId": run_id, "status": "accepted"})

        # This socket now owns the session's live stream until a re-entry on a
        # different socket rebinds it (see chat.inflight in _handle_feature_rpc).
        self.bind_session_ws(session_key, ws)

        # Build streaming callback that pushes delta events to the session's
        # CURRENT socket (follows the latest viewer on re-entry).
        async def stream_callback(delta: str) -> None:
            await self._session_send(session_key, ws, {
                "type": "event",
                "event": "agent",
                "data": {
                    "runId": run_id,
                    "stream": "assistant",
                    "data": {"text": delta},
                },
            })

        # Process in background so we don't block the recv loop.
        task = asyncio.create_task(
            self._run_chat(
                ws, client_id, session_key, message, run_id,
                stream_callback, media, voice_mode,
            )
        )
        self._active_tasks[run_id] = task
        task.add_done_callback(lambda _: self._active_tasks.pop(run_id, None))

    async def _run_chat(
        self,
        ws: web.WebSocketResponse,
        client_id: str,
        session_key: str,
        message: str,
        run_id: str,
        stream_callback: Callable[[str], Awaitable[None]],
        media: list[str] | None = None,
        voice_mode: bool = False,
    ) -> None:
        """Execute the chat and send final/error events."""
        accumulated_text = ""

        # Track the in-flight stream so a client that leaves and re-enters
        # mid-run can fetch the partial via the chat.inflight RPC.
        from flowly.agent import inflight
        inflight.begin(session_key, run_id, message)

        # Wrap the stream callback to accumulate full text for the final event.
        async def tracking_callback(delta: str) -> None:
            nonlocal accumulated_text
            accumulated_text += delta
            inflight.append(session_key, run_id, delta)
            await stream_callback(delta)

        # Live per-iteration tool-turn events. The agent loop emits one of these
        # after each assistant_with_tool_calls / tool_result; we forward it as a
        # ``state:"iteration_step"`` chat event — the SAME shape the relay sends
        # — so the desktop's tool-turn panel populates live over the direct
        # gateway. We stamp the gateway's own ``run_id`` (the one the client
        # tracks) so the client accepts the events regardless of any internal
        # run id the loop used.
        async def iteration_callback(event: dict) -> None:
            wrapped = {**event, "state": "iteration_step", "runId": run_id}
            # Persist the tool-turn event so a client that re-enters mid-stream
            # can rebuild the live tool-call panel (chat.inflight returns these),
            # not just the assistant text. Store BEFORE the send so a dropped
            # socket doesn't lose it.
            inflight.append_iteration(session_key, run_id, wrapped)
            await self._session_send(
                session_key, ws, {"type": "event", "event": "chat", "data": wrapped}
            )

        try:
            assert self.on_chat_message is not None
            result = await self.on_chat_message(
                session_key, message, run_id, tracking_callback, media or [], voice_mode,
                iteration_callback,
            )
            # Back-compat: older callbacks returned bare text. Detect the
            # tuple form and fall back to ``{}`` metadata otherwise so
            # any third-party gateway wiring keeps working.
            if isinstance(result, tuple):
                response, metadata = result
            else:
                response, metadata = result, {}
            usage = (metadata or {}).get("usage") or {}
            model = (metadata or {}).get("model") or ""

            # Send final chat event. ``usage`` rides inside ``message`` so
            # the existing TUI client parser (which already looks at
            # ``msg.usage``) sees it without any wire-format change.
            # ``model`` is included at the top level so future clients
            # can size their context-window indicator from the *actual*
            # model the turn ran on (which may differ from the user's
            # pick after fallback/rotation).
            # Reply media (image_generate / screenshot) → attachments built by
            # ``_reply_media_attachments`` — the SAME shape the chat.history RPC
            # emits, so a live reply and a reopened chat render media identically.
            # This is what makes generated media show in the reply over a remote
            # gateway (iOS / desktop direct WS); without it the final event is
            # text-only and the image is lost.
            final_message = {
                "role": "assistant",
                "content": [{"type": "text", "text": response}],
                "usage": usage,
            }
            reply_media = (metadata or {}).get("media") or []
            if reply_media:
                atts = _reply_media_attachments(reply_media)
                if atts:
                    final_message["attachments"] = atts

            await self._session_send(session_key, ws, {
                "type": "event",
                "event": "chat",
                "data": {
                    "state": "final",
                    "runId": run_id,
                    "sessionKey": session_key,
                    "model": model,
                    "message": final_message,
                },
            })
        except asyncio.CancelledError:
            await self._session_send(session_key, ws, {
                "type": "event",
                "event": "chat",
                "data": {"state": "aborted", "runId": run_id, "sessionKey": session_key},
            })
        except Exception as e:
            logger.error(f"[WS] chat.send run {run_id} failed: {e}")
            await self._session_send(session_key, ws, {
                "type": "event",
                "event": "chat",
                "data": {
                    "state": "error",
                    "runId": run_id,
                    "sessionKey": session_key,
                    "errorMessage": str(e),
                },
            })
        finally:
            # Run settled (final / aborted / error) — the partial is no
            # longer needed; the final message carries the full text.
            inflight.finish(session_key, run_id)

    # --- RPC: chat.abort ---

    async def _ws_rpc_chat_abort(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        run_id = params.get("runId", "")
        task = self._active_tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        await self._ws_rpc_reply(ws, rpc_id, {"ok": True})

    # ------------------------------------------------------------------
    # RPC: chat.compact / chat.clear
    # ------------------------------------------------------------------

    async def _ws_rpc_chat_compact(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.on_compact:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Compaction not available")
        session_key = params.get("sessionKey", "desktop:default")
        instructions = params.get("instructions")
        try:
            result = await self.on_compact(session_key, instructions)
            await self._ws_rpc_reply(ws, rpc_id, result)
        except Exception as e:
            logger.error(f"[WS] chat.compact error: {e}")
            await self._ws_rpc_error(ws, rpc_id, "INTERNAL", str(e))

    async def _ws_rpc_chat_clear(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.on_clear:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Clear not available")
        session_key = params.get("sessionKey", "desktop:default")
        try:
            result = await self.on_clear(session_key)
            await self._ws_rpc_reply(ws, rpc_id, result)
        except Exception as e:
            logger.error(f"[WS] chat.clear error: {e}")
            await self._ws_rpc_error(ws, rpc_id, "INTERNAL", str(e))

    async def _ws_rpc_chat_retry(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.on_retry:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Retry not available")
        session_key = params.get("sessionKey", "desktop:default")
        try:
            result = await self.on_retry(session_key)
            await self._ws_rpc_reply(ws, rpc_id, result)
        except Exception as e:
            logger.error(f"[WS] chat.retry error: {e}")
            await self._ws_rpc_error(ws, rpc_id, "INTERNAL", str(e))

    async def _ws_rpc_chat_undo(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.on_undo:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Undo not available")
        session_key = params.get("sessionKey", "desktop:default")
        try:
            result = await self.on_undo(session_key)
            await self._ws_rpc_reply(ws, rpc_id, result)
        except Exception as e:
            logger.error(f"[WS] chat.undo error: {e}")
            await self._ws_rpc_error(ws, rpc_id, "INTERNAL", str(e))

    # ------------------------------------------------------------------
    # WebSocket helpers
    # ------------------------------------------------------------------

    async def _ws_send(self, ws: web.WebSocketResponse, data: dict) -> None:
        """Send JSON to a WebSocket client, silently ignoring closed connections."""
        try:
            if not ws.closed:
                await ws.send_json(data)
        except (ConnectionResetError, RuntimeError):
            pass

    async def _session_send(
        self, session_key: str, fallback_ws: web.WebSocketResponse, data: dict,
    ) -> None:
        """Send a live stream event to the session's CURRENT socket.

        Routes via ``_session_ws`` (the latest socket that ran chat.send or
        called chat.inflight for this session) so a client that re-entered
        mid-stream on a fresh socket keeps receiving forward events. Falls back
        to the originating socket when nothing is registered. ``_ws_send`` no-ops
        on a closed socket, so a stale registration just drops silently."""
        target = self._session_ws.get(session_key) or fallback_ws
        await self._ws_send(target, data)

    def bind_session_ws(self, session_key: str, ws: web.WebSocketResponse) -> None:
        """Point a session's live stream at ``ws`` (transport-rebind)."""
        if session_key:
            self._session_ws[session_key] = ws

    async def _handle_feature_rpc(
        self, ws: web.WebSocketResponse, rpc_id: str, method: str, params: dict,
    ) -> None:
        """Serve a feature RPC via the shared ``feature_rpc`` dispatch — the
        exact handlers the relay channel serves, so the desktop sees identical
        shapes over either transport.

        A structured :class:`feature_rpc.FeatureRpcError` becomes an ``error``
        reply; anything else becomes INTERNAL (logged). A mutation that needs a
        restart is ACKed FIRST, then the gateway is bounced in the background —
        the restart kills THIS connection, so awaiting would cut the socket
        before the reply flushed; the client reconnects on its own.
        """
        try:
            result, needs_restart = await feature_rpc.dispatch(method, params)
        except feature_rpc.FeatureRpcError as e:
            await self._ws_rpc_error(ws, rpc_id, e.code, e.message)
            return
        except Exception as e:
            logger.exception(f"[Gateway] feature rpc {method} failed")
            await self._ws_rpc_error(ws, rpc_id, "INTERNAL", str(e))
            return
        # chat.inflight is the re-entry handshake: the client just (re)opened
        # this session, so rebind its live stream to THIS socket — any run still
        # in flight now streams forward events here instead of the socket that
        # started it (which the client may have already left).
        if method == "chat.inflight":
            self.bind_session_ws(str(params.get("sessionKey") or ""), ws)
        await self._ws_rpc_reply(ws, rpc_id, result)
        if needs_restart:
            self._schedule_feature_restart()

    def _schedule_feature_restart(self) -> None:
        """Bounce the gateway after the ACK frame has flushed."""
        async def _run() -> None:
            await asyncio.sleep(0.5)
            try:
                from flowly.integrations.service_control import restart_gateway
                await restart_gateway(
                    health_check_host=self.host, health_check_port=self.port,
                )
            except Exception:
                logger.exception("[Gateway] feature restart failed")
        asyncio.create_task(_run())

    async def _ws_rpc_reply(self, ws: web.WebSocketResponse, rpc_id: str, result: Any) -> None:
        await self._ws_send(ws, {"type": "rpc", "id": rpc_id, "result": result})

    # ------------------------------------------------------------------
    # Browser extension tool request/response
    # ------------------------------------------------------------------

    def has_extension_client(self) -> bool:
        """Check if a browser extension is connected."""
        if not self._extension_active:
            return False
        ws = self._ws_clients.get(self._extension_active)
        return ws is not None and not ws.closed

    async def send_extension_tool_request(
        self, request_id: str, action: str, params: dict
    ) -> dict:
        """Send a tool request to the extension and wait for the result."""
        if not self.has_extension_client():
            return {"error": "Extension not connected"}

        ws = self._ws_clients.get(self._extension_active)
        if not ws or ws.closed:
            return {"error": "Extension WebSocket closed"}
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._extension_pending[request_id] = future

        try:
            await self._ws_send(ws, {
                "type": "tool_request",
                "id": request_id,
                "action": action,
                "params": params,
            })
            return await future
        finally:
            self._extension_pending.pop(request_id, None)

    def _handle_extension_tool_result(self, data: dict) -> None:
        """Handle a tool_result message from the extension."""
        request_id = data.get("id")
        result = data.get("result", {})
        future = self._extension_pending.get(request_id)
        if future and not future.done():
            future.set_result(result)

    async def push_session_message(self, session_key: str, text: str) -> None:
        """Push a proactive assistant message to connected WS clients.

        Local clients (TUI / desktop) have no channel adapter, so out-of-band
        deliveries (e.g. a finished board card's result) reach them through
        this push — the same role a channel adapter plays for Telegram /
        WhatsApp. It's sent as a normal chat ``final`` event, so the client
        renders it exactly like any assistant reply (no client change needed).
        """
        if not text:
            return
        import uuid as _uuid

        payload = {
            "type": "event",
            "event": "chat",
            "data": {
                "state": "final",
                "proactive": True,
                "runId": "proactive-" + _uuid.uuid4().hex[:8],
                "sessionKey": session_key,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                },
            },
        }
        for ws in list(self._ws_clients.values()):
            try:
                await self._ws_send(ws, payload)
            except Exception:
                pass  # closed socket — best effort

    async def broadcast_agent_state(self, state: str) -> None:
        """Push the agent's turn-level state to every registered extension.

        Called by the agent loop at turn boundaries so extensions can show
        a presence cue (breathing border, tab-group spinner, etc.) for the
        FULL duration of a turn — including LLM reasoning gaps between
        tool calls — instead of only while a tool round-trip is in flight.

        States: "active" | "idle". Best-effort: closed sockets are skipped
        silently and never raise out of this method.
        """
        if not self._extension_clients:
            return
        payload = {
            "type": "event",
            "event": "agent_state",
            "data": {"state": state},
        }
        for client_id in list(self._extension_clients):
            ws = self._ws_clients.get(client_id)
            if ws is None or ws.closed:
                continue
            try:
                await self._ws_send(ws, payload)
            except Exception as e:
                logger.debug(f"agent_state push to {client_id} failed: {e}")

    async def _ws_rpc_error(self, ws: web.WebSocketResponse, rpc_id: str, code: str, message: str) -> None:
        await self._ws_send(ws, {
            "type": "rpc",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        })

    # ------------------------------------------------------------------
    # RPC: artifacts
    # ------------------------------------------------------------------

    async def _ws_rpc_artifacts_list(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.artifact_store:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Artifacts not enabled")
        limit = int(params.get("limit", 50))
        include_internal = bool(params.get("includeInternal", False))
        # Fetch extra rows so we can drop internal artifacts without
        # shrinking the visible page below the caller's limit. Matches
        # the pattern ArtifactTool._list already uses for its own filter.
        fetch_limit = limit if include_internal else max(limit * 5, 100)
        results = self.artifact_store.list(
            type=params.get("type"),
            pinned=params.get("pinned"),
            search=params.get("search"),
            limit=fetch_limit,
            offset=params.get("offset", 0),
        )
        if not include_internal:
            results = [a for a in results if not is_internal_context_artifact(a)]
        results = results[:limit]
        await self._ws_rpc_reply(ws, rpc_id, {"artifacts": results})

    async def _ws_rpc_artifacts_get(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.artifact_store:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Artifacts not enabled")
        artifact = self.artifact_store.get(params.get("id", ""))
        if not artifact:
            return await self._ws_rpc_error(ws, rpc_id, "NOT_FOUND", "Artifact not found")
        await self._ws_rpc_reply(ws, rpc_id, {"artifact": artifact})

    async def _ws_rpc_artifacts_create(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.artifact_store:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Artifacts not enabled")
        art_type = params.get("type", "")
        title = params.get("title", "")
        content = params.get("content", "")
        if not art_type or not title or not content:
            return await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "type, title, content required")
        artifact = self.artifact_store.create(
            type=art_type, title=title, content=content,
            pinned=params.get("pinned", False),
            dashboard_size=params.get("dashboardSize", "medium"),
            tags=params.get("tags"),
        )
        await self._broadcast_artifact_event("artifact.created", artifact)
        await self._ws_rpc_reply(ws, rpc_id, {"artifact": artifact})

    async def _ws_rpc_artifacts_update(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.artifact_store:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Artifacts not enabled")
        artifact_id = params.get("id", "")
        if not artifact_id:
            return await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "id required")
        artifact = self.artifact_store.update(
            artifact_id,
            title=params.get("title"),
            content=params.get("content"),
            pinned=params.get("pinned"),
            dashboard_size=params.get("dashboardSize"),
            tags=params.get("tags"),
        )
        if not artifact:
            return await self._ws_rpc_error(ws, rpc_id, "NOT_FOUND", "Artifact not found")
        await self._broadcast_artifact_event("artifact.updated", artifact)
        await self._ws_rpc_reply(ws, rpc_id, {"artifact": artifact})

    async def _ws_rpc_artifacts_delete(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.artifact_store:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Artifacts not enabled")
        artifact_id = params.get("id", "")
        if not artifact_id:
            return await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "id required")
        deleted = self.artifact_store.delete(artifact_id)
        if not deleted:
            return await self._ws_rpc_error(ws, rpc_id, "NOT_FOUND", "Artifact not found")
        await self._broadcast_artifact_event("artifact.deleted", {"id": artifact_id})
        await self._ws_rpc_reply(ws, rpc_id, {"ok": True})

    async def _ws_rpc_artifacts_pin(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.artifact_store:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Artifacts not enabled")
        artifact_id = params.get("id", "")
        pinned = params.get("pinned", True)
        if not artifact_id:
            return await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "id required")
        artifact = self.artifact_store.pin(artifact_id, pinned)
        if not artifact:
            return await self._ws_rpc_error(ws, rpc_id, "NOT_FOUND", "Artifact not found")
        await self._broadcast_artifact_event("artifact.updated", artifact)
        await self._ws_rpc_reply(ws, rpc_id, {"artifact": artifact})

    async def _ws_rpc_artifacts_versions(self, ws: web.WebSocketResponse, rpc_id: str, params: dict) -> None:
        if not self.artifact_store:
            return await self._ws_rpc_error(ws, rpc_id, "UNAVAILABLE", "Artifacts not enabled")
        artifact_id = params.get("id", "")
        if not artifact_id:
            return await self._ws_rpc_error(ws, rpc_id, "INVALID_REQUEST", "id required")
        versions = self.artifact_store.get_versions(artifact_id)
        await self._ws_rpc_reply(ws, rpc_id, {"versions": versions})

    async def _broadcast_subagent_event(self, event_name: str, data: dict) -> None:
        """Push subagent lifecycle event to all connected WS clients."""
        event = {"type": "event", "event": event_name, "data": data}
        for ws in list(self._ws_clients.values()):
            await self._ws_send(ws, event)

    async def _broadcast_compaction_event(self, data: dict) -> None:
        """Push compaction event to all connected WS clients."""
        event = {"type": "event", "event": "compaction", "data": data}
        for ws in list(self._ws_clients.values()):
            await self._ws_send(ws, event)

    async def broadcast_tool_event(self, event_name: str, data: dict) -> None:
        """Push tool lifecycle event (``tool.start`` / ``tool.complete``)
        to all connected WS clients. Wired into the agent loop via
        ``agent.tool_callback`` by the gateway bootstrapper.
        """
        event = {"type": "event", "event": event_name, "data": data}
        for ws in list(self._ws_clients.values()):
            await self._ws_send(ws, event)

    async def _broadcast_artifact_event(self, event_name: str, data: dict) -> None:
        """Push artifact event to all connected WS clients."""
        event = {"type": "event", "event": event_name, "data": data}
        for ws in list(self._ws_clients.values()):
            await self._ws_send(ws, event)

    async def broadcast_cron_event(self, event_name: str, data: dict) -> None:
        """Push a cron lifecycle event (``cron.completed``) to all connected
        WS clients. Wired into ``CronService.on_complete`` by the gateway
        bootstrapper so desktop clients can surface a native OS notification
        when a scheduled job finishes. Fire-and-forget: a failure here must
        never affect cron execution (the caller wraps it in try/except).
        """
        event = {"type": "event", "event": event_name, "data": data}
        for ws in list(self._ws_clients.values()):
            await self._ws_send(ws, event)

    # ------------------------------------------------------------------
    # HTTP: artifacts
    # ------------------------------------------------------------------

    async def _handle_artifacts_list(self, request: web.Request) -> web.Response:
        params = dict(request.query)
        limit = int(params.get("limit", 50))
        include_internal = params.get("includeInternal") == "true"
        fetch_limit = limit if include_internal else max(limit * 5, 100)
        results = self.artifact_store.list(
            type=params.get("type"),
            pinned=params.get("pinned") == "true" if "pinned" in params else None,
            search=params.get("search"),
            limit=fetch_limit,
            offset=int(params.get("offset", 0)),
        )
        if not include_internal:
            results = [a for a in results if not is_internal_context_artifact(a)]
        results = results[:limit]
        return web.json_response({"artifacts": results})

    async def _handle_artifacts_get(self, request: web.Request) -> web.Response:
        artifact_id = request.match_info["id"]
        artifact = self.artifact_store.get(artifact_id)
        if not artifact:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response({"artifact": artifact})

    async def _handle_artifacts_versions(self, request: web.Request) -> web.Response:
        artifact_id = request.match_info["id"]
        versions = self.artifact_store.get_versions(artifact_id)
        return web.json_response({"versions": versions})

    # ------------------------------------------------------------------
    # Tick loop — periodic health pings to connected WS clients
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        """Send periodic tick events to all connected desktop clients."""
        while True:
            try:
                await asyncio.sleep(10)
                if not self._ws_clients:
                    continue
                tick = {"type": "event", "event": "tick"}
                for ws in list(self._ws_clients.values()):
                    await self._ws_send(ws, tick)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the HTTP + WebSocket server."""
        self._app = self._create_app()
        # tcp_keepalive=False: aiohttp otherwise sets SO_KEEPALIVE on every
        # accepted socket in connection_made, which races on macOS when a client
        # connects then immediately drops (e.g. the reconnect storm after a
        # config-change gateway restart) and spams "OSError: [Errno 22] Invalid
        # argument". We rely on application-level heartbeats, so OS keepalive is
        # unnecessary; disabling it removes the benign-but-noisy tracebacks.
        self._runner = web.AppRunner(self._app, tcp_keepalive=False)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await self._site.start()
        except OSError as e:
            # address already in use — errno differs per OS:
            #   48=macOS, 98=Linux, 10048=Windows (WSAEADDRINUSE). Without the
            #   Windows code, a second gateway on a busy port dumped a raw
            #   aiohttp/asyncio traceback instead of this hint.
            if e.errno in (48, 98, 10048):
                raise SystemExit(
                    f"\nPort {self.port} is already in use.\n"
                    f"The gateway service is probably already running.\n\n"
                    f"  flowly service status    — check running service\n"
                    f"  flowly service stop      — stop it first\n"
                    f"  flowly service restart   — restart it\n"
                ) from None
            raise
        if self.on_chat_message:
            self._tick_task = asyncio.create_task(self._tick_loop())
        # Advertise the MCP control endpoint for `flowly mcp serve`.
        if self.on_send and self._control_token:
            try:
                from flowly.mcp.server.control import write_api_file
                write_api_file(self.host, self.port, self._control_token)
            except Exception as exc:  # pragma: no cover
                logger.debug("MCP control advertise failed: %s", exc)
        logger.info(f"Gateway API listening on http://{self.host}:{self.port}")
        if self.on_chat_message:
            logger.info(f"Desktop WebSocket available at ws://{self.host}:{self.port}/ws")

    async def stop(self) -> None:
        """Stop the server and clean up."""
        # Withdraw the MCP control endpoint advertisement.
        if self.on_send and self._control_token:
            try:
                from flowly.mcp.server.control import remove_api_file
                remove_api_file()
            except Exception:
                pass
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        # Cancel any in-flight chat tasks.
        for task in self._active_tasks.values():
            task.cancel()
        # Close all WebSocket connections.
        for ws in list(self._ws_clients.values()):
            await ws.close()
        self._ws_clients.clear()
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Gateway API stopped")
