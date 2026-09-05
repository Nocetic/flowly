"""OAuth 2.1 / PKCE support for remote (HTTP) MCP servers (Faz 2b).

The MCP SDK ships a complete client-side OAuth flow
(``mcp.client.auth.OAuthClientProvider``) that handles PKCE, dynamic
client registration (RFC 7591), and transparent token refresh as an
``httpx.Auth`` flow. Our job is to supply the three pluggable pieces:

1. **Token storage** — :class:`FlowlyTokenStorage` persists the client
   registration and tokens to ``$FLOWLY_HOME/mcp-tokens/{server}.json``
   (mode 0600, profile-aware). The SDK reads/writes through it so
   credentials survive across sessions and refresh transparently.

2. **Redirect handler** — opens the authorization URL in the user's
   browser. Only meaningful interactively (``flowly mcp login`` /
   ``flowly mcp add --auth oauth``).

3. **Callback handler** — runs a one-shot localhost HTTP server to
   capture the ``?code=...&state=...`` redirect and hand it back to the
   SDK.

At agent boot (non-interactive) we still build the provider so the SDK
can use *stored* tokens and refresh them silently; if no tokens exist
and no browser is available, the connect fails and the server is
skipped + logged like any other failure — boot is never blocked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from flowly.mcp.oauth_state import atomic_private_write, read_private, state_lock
from flowly.mcp.schema import sanitize_mcp_name_component

logger = logging.getLogger(__name__)


_OAUTH_AVAILABLE = False
try:
    from mcp.client.auth.oauth2 import TokenStorage  # type: ignore
    from mcp.shared.auth import (  # type: ignore
        AuthorizationCodeResult,
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthToken,
    )

    _OAUTH_AVAILABLE = True
except ImportError:  # pragma: no cover — older SDK without auth module
    TokenStorage = object  # type: ignore
    logger.debug("MCP OAuth support unavailable in this SDK build")


# Default localhost callback port. The redirect URI registered with the
# authorization server must match exactly, so we pin a port rather than
# pick a random one (some servers reject unknown redirect URIs).
_CALLBACK_HOST = "127.0.0.1"
_CALLBACK_PORT = 8765
_CALLBACK_PATH = "/callback"
OAUTH_CALLBACK_TIMEOUT_SECONDS = 300.0


def oauth_available() -> bool:
    return _OAUTH_AVAILABLE


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------


def _tokens_dir() -> Path:
    from flowly.profile import get_flowly_home

    path = get_flowly_home() / "mcp-tokens"
    path.mkdir(parents=True, exist_ok=True)
    try:
        from flowly.utils.file_security import secure_dir

        secure_dir(path)  # POSIX chmod; real owner-only ACL on Windows
    except OSError:
        pass
    return path


def _token_file(server_name: str) -> Path:
    safe = sanitize_mcp_name_component(server_name) or "server"
    return _tokens_dir() / f"{safe}.json"


class OAuthStateChangedError(RuntimeError):
    """A newer login/logout won while a network authorization was in flight."""


_UNGUARDED = object()
_active_login: ContextVar[Any] = ContextVar("oauth_active_login", default=None)


class FlowlyTokenStorage(TokenStorage):  # type: ignore[misc]
    """Persist OAuth client info + tokens to ``$FLOWLY_HOME/mcp-tokens/``.

    One JSON file per server, ``{"client_info": {...}, "tokens": {...}}``,
    written atomically with mode 0600. The SDK calls these async methods
    from the MCP event loop.
    """

    def __init__(self, server_name: str, url: str | None = None) -> None:
        self._server_name = server_name
        self._path = _token_file(server_name)
        self._url = url
        self._closed = False
        self._expected: ContextVar[Any] = ContextVar("oauth_expected_revision", default=_UNGUARDED)

    @property
    def recovery_path(self) -> Path:
        return self._path.with_suffix(".auth.lock")

    def _read(self) -> dict[str, Any]:
        try:
            raw = read_private(self._path)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            if "revision" not in data:
                # Distinguish legacy credentials from an absent file so an
                # in-flight first refresh cannot undo a concurrent logout.
                data["revision"] = "legacy:" + hashlib.sha256(raw).hexdigest()
            return data
        except (FileNotFoundError, ValueError):
            return {}

    def _bound(self, data: dict[str, Any]) -> bool:
        return (
            data.get("server_name", self._server_name) == self._server_name
            and (self._url is None or data.get("server_url", self._url) == self._url)
        )

    def snapshot(self) -> dict[str, Any]:
        data = self._read()
        if not self._bound(data):
            return {"revision": data.get("revision")}
        return data

    @contextmanager
    def guard(self, snapshot: dict[str, Any]):
        token = self._expected.set(snapshot.get("revision"))
        try:
            yield
        finally:
            self._expected.reset(token)

    def _update_sync(self, values: dict[str, Any], expected: Any) -> str:
        if self._closed:
            raise OAuthStateChangedError("OAuth login was cancelled or completed")
        with state_lock(self._path.with_suffix(".lock")):
            if self._closed:
                raise OAuthStateChangedError("OAuth login was cancelled or completed")
            data = self._read()
            if expected is not _UNGUARDED and data.get("revision") != expected:
                raise OAuthStateChangedError("OAuth credentials changed during recovery; retry the request")
            if not self._bound(data):
                data = {}
            data.update(values)
            data["server_name"] = self._server_name
            if self._url is not None:
                data["server_url"] = self._url
            revision = data["revision"] = uuid.uuid4().hex
            atomic_private_write(self._path, json.dumps(data, indent=2).encode("utf-8"))
            return revision

    async def update(self, values: dict[str, Any]) -> None:
        expected = self._expected.get()
        revision = await asyncio.to_thread(self._update_sync, values, expected)
        if expected is not _UNGUARDED:
            self._expected.set(revision)

    async def get_tokens(self) -> Any | None:
        data = self.snapshot().get("tokens")
        if not data:
            return None
        try:
            return OAuthToken.model_validate(data)
        except Exception:
            return None

    async def set_tokens(self, tokens: Any) -> None:
        expires_in = getattr(tokens, "expires_in", None)
        expires_at = None
        if isinstance(expires_in, (int, float)) and math.isfinite(expires_in):
            expires_at = time.time() + max(0, expires_in)
        await self.update({
            "tokens": json.loads(tokens.model_dump_json()), "expires_at": expires_at,
            "token_revision": uuid.uuid4().hex,
            "refresh_rejected": None, "refresh_retry_at": None,
        })

    async def get_client_info(self) -> Any | None:
        data = self.snapshot().get("client_info")
        if not data:
            return None
        try:
            return OAuthClientInformationFull.model_validate(data)
        except Exception:
            return None

    async def set_client_info(self, client_info: Any) -> None:
        await self.update({"client_info": json.loads(client_info.model_dump_json())})


def clear_tokens(server_name: str) -> bool:
    """Delete the stored token file for *server_name*. Returns True if removed."""
    path = _token_file(server_name)
    try:
        with state_lock(path.with_suffix(".lock")):
            if not path.exists():
                return False
            path.unlink()
            return True
    except OSError:
        logger.warning("MCP token clear failed for '%s'", server_name)
    return False


def has_tokens(server_name: str) -> bool:
    """True if a token file exists (does not validate its contents)."""
    return _token_file(server_name).exists()


def backup_tokens(server_name: str) -> tuple[bool, bytes] | None:
    """Capture opaque token-file bytes before a destructive re-auth.

    Returns ``(existed, data)`` or ``None`` if an existing file could not be
    read. Callers must abort re-auth on ``None`` rather than risk destroying
    the last working credentials.
    """
    path = _token_file(server_name)
    if not path.exists():
        return False, b""
    try:
        return True, read_private(path)
    except OSError as exc:
        logger.warning("MCP token backup failed for '%s': %s", server_name, exc)
        return None


def restore_tokens(server_name: str, backup: tuple[bool, bytes]) -> bool:
    """Atomically restore a token backup after failed/cancelled re-auth."""
    existed, data = backup
    path = _token_file(server_name)
    if not existed:
        try:
            with state_lock(path.with_suffix(".lock")):
                path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            logger.warning("MCP partial-token cleanup failed for '%s': %s", server_name, exc)
            return False

    try:
        with state_lock(path.with_suffix(".lock")):
            atomic_private_write(path, data)
        return True
    except OSError as exc:
        logger.warning("MCP token restore failed for '%s': %s", server_name, exc)
        return False


# ---------------------------------------------------------------------------
# Callback server (one-shot localhost capture of the OAuth redirect)
# ---------------------------------------------------------------------------


class _CallbackResult:
    def __init__(self) -> None:
        self.code: str | None = None
        self.state: str | None = None
        self.iss: str | None = None
        self.error: str | None = None
        self.event = threading.Event()


def _run_callback_server(result: _CallbackResult, timeout: float) -> None:
    """Serve a single OAuth redirect on the pinned localhost port."""
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != _CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(parsed.query)
            result.code = (params.get("code") or [None])[0]
            result.state = (params.get("state") or [None])[0]
            result.iss = (params.get("iss") or [None])[0]
            result.error = (params.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = (
                "<html><body><h2>Flowly: authentication "
                + ("complete" if result.code else "failed")
                + "</h2><p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            )
            self.wfile.write(body.encode("utf-8"))
            result.event.set()

        def log_message(self, *args):  # silence default stderr logging
            return

    try:
        server = HTTPServer((_CALLBACK_HOST, _CALLBACK_PORT), _Handler)
    except OSError as exc:
        result.error = f"could not bind localhost callback port {_CALLBACK_PORT}: {exc}"
        result.event.set()
        return

    # Poll at a short interval so the deadline closes the listening socket
    # promptly. The previous single 300 s handle_request timeout re-entered
    # forever after expiry, leaking a daemon thread and keeping port 8765 busy.
    deadline = time.monotonic() + max(0.0, timeout)
    server.timeout = min(0.25, max(0.01, timeout))
    try:
        while not result.event.is_set() and time.monotonic() < deadline:
            server.handle_request()
            if result.code or result.error:
                break
        if not result.event.is_set():
            result.error = "OAuth callback timed out without a code"
            result.event.set()
    finally:
        try:
            server.server_close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Provider builder
# ---------------------------------------------------------------------------


def build_oauth_provider(
    server_name: str,
    url: str,
    *,
    interactive: bool,
    scope: str | None = None,
    callback_timeout: float = OAUTH_CALLBACK_TIMEOUT_SECONDS,
    allow_same_origin_paths: bool = False,
) -> Any | None:
    """Construct an ``OAuthClientProvider`` for *url*, or ``None``.

    Returns ``None`` when the SDK lacks OAuth support. When
    ``interactive`` is False the redirect/callback handlers raise if the
    SDK actually needs a browser round-trip — that surfaces as a connect
    failure for this server and nothing else, which is the desired
    non-interactive behavior (use stored/refreshable tokens only).
    """
    if not _OAUTH_AVAILABLE:
        return None

    redirect_uri = f"http://{_CALLBACK_HOST}:{_CALLBACK_PORT}{_CALLBACK_PATH}"
    client_metadata = OAuthClientMetadata(
        client_name="Flowly",
        redirect_uris=[redirect_uri],  # type: ignore[arg-type]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
    )

    async def _redirect_handler(authorization_url: str) -> None:
        if not interactive:
            raise RuntimeError(
                f"MCP server '{server_name}' needs interactive OAuth; run "
                f"`flowly mcp login {server_name}`"
            )
        import webbrowser

        print(f"\n  Opening browser to authorize MCP server '{server_name}'...")
        print(f"  If it doesn't open, visit:\n    {authorization_url}\n")
        try:
            webbrowser.open(authorization_url)
        except Exception:
            pass

    async def _callback_handler() -> Any:
        if not interactive:
            raise RuntimeError(f"MCP server '{server_name}' needs interactive OAuth callback")
        import asyncio

        result = _CallbackResult()
        thread = threading.Thread(
            target=_run_callback_server,
            args=(result, callback_timeout),
            daemon=True,
        )
        thread.start()
        try:
            completed = await asyncio.to_thread(result.event.wait, callback_timeout)
        except asyncio.CancelledError:
            # Cancelling the MCP probe must also stop the one-shot HTTP server;
            # otherwise it can retain port 8765 until the human-auth deadline
            # and make the next sign-in fail with "address already in use".
            if not result.event.is_set():
                result.error = "OAuth callback cancelled"
                result.event.set()
            await asyncio.to_thread(thread.join, 1.0)
            raise
        if not completed and not result.event.is_set():
            result.error = "OAuth callback timed out without a code"
            result.event.set()
        # The callback server polls at 250 ms and closes its listening socket
        # in finally. Joining here makes successful, failed, and timed-out
        # flows all release the port before the RPC completes.
        await asyncio.to_thread(thread.join, 1.0)
        if result.error:
            raise RuntimeError(f"OAuth callback error: {result.error}")
        if not result.code:
            raise RuntimeError("OAuth callback timed out without a code")
        return AuthorizationCodeResult(code=result.code, state=result.state, iss=result.iss)

    from flowly.mcp.oauth_provider import CoordinatedOAuthProvider

    return CoordinatedOAuthProvider(
        interactive=interactive,
        server_name=server_name,
        allow_same_origin_paths=allow_same_origin_paths,
        server_url=url,
        client_metadata=client_metadata,
        storage=token_storage_for(server_name, url),
        redirect_handler=_redirect_handler,
        callback_handler=_callback_handler,
    )


def token_storage_for(server_name: str, url: str) -> FlowlyTokenStorage:
    """Only a runtime-owned login context may select a staging credential store."""
    staged = _active_login.get()
    if staged is not None and staged._server_name == server_name and staged._url == url:
        return staged
    return FlowlyTokenStorage(server_name, url)


@contextmanager
def oauth_login(server_name: str, url: str):
    """Stage re-authorization; publish only a successfully probed fresh grant.

    Working credentials stay available during login. Failure/cancellation
    needs no rollback and cannot overwrite another process's newer login.
    The context follows the existing thread-safe probe submission to its loop.
    """
    from flowly.mcp.env_loader import load_flowly_dotenv
    from flowly.mcp.security import interpolate_env_vars

    load_flowly_dotenv()
    url = str(interpolate_env_vars({"url": url})["url"]).strip()
    canonical = FlowlyTokenStorage(server_name, url)
    original = canonical.snapshot()
    staged = FlowlyTokenStorage(server_name, url)
    staged._path = canonical._path.with_name(f".login-{uuid.uuid4().hex}.json")

    class Login:
        def commit(self) -> None:
            data = staged.snapshot()
            if not data.get("tokens"):
                raise OAuthStateChangedError("OAuth login produced no credentials")
            canonical._update_sync(data, original.get("revision"))

    context_token = _active_login.set(staged)
    try:
        yield Login()
    finally:
        _active_login.reset(context_token)
        # A cancelled cross-loop probe may still be unwinding. Close first,
        # then synchronize with any short disk write before removing staging.
        staged._closed = True
        with state_lock(staged._path.with_suffix(".lock")):
            staged._path.unlink(missing_ok=True)
        staged._path.with_suffix(".lock").unlink(missing_ok=True)
        staged.recovery_path.unlink(missing_ok=True)
