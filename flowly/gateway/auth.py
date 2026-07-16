"""Gateway authentication for remote (self-hosted) desktop clients.

Background
----------
The gateway historically bound to ``127.0.0.1`` only and trusted every
local process — no auth on ``/ws`` or the HTTP API at all. Self-hosting on a
VPS changes that: once the gateway is reachable on a public IP, the socket is
the *only* boundary, so it MUST authenticate.

We use a "token + ws-ticket" model:

* A long-lived **static token** authenticates REST requests
  (``X-Flowly-Token: <token>`` or ``Authorization: Bearer <token>``).
* The WebSocket upgrade is authenticated by a **single-use, short-TTL
  ticket** minted at ``POST /api/auth/ws-ticket`` (which itself requires the
  static token). The ticket — not the long-lived token — travels in the
  ``/ws?ticket=`` query string, so the real credential never lands in proxy
  logs / browser history / the URL bar.

For backwards-compatibility with simple local clients (the TUI, the desktop
in local mode) the ``/ws`` endpoint also accepts the raw ``?token=`` when
auth is enabled; tickets are simply the preferred, safer path.

Auth is engaged whenever a non-empty token is configured. Loopback installs
with no token keep the legacy "trust localhost" behaviour so the desktop's
locally-spawned gateway keeps working unchanged.
"""

from __future__ import annotations

import hmac
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web

# Ticket time-to-live. Long enough to mint-then-connect across a slow link,
# short enough that a leaked ticket is near-useless (30s).
TICKET_TTL_SECONDS = 30

# Header carrying the static token on REST requests.
TOKEN_HEADER = "X-Flowly-Token"

# Hostnames that count as loopback — auth stays optional for these so the
# desktop's locally-spawned gateway is unaffected.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0000:0000:0000:0000:0000:0000:0000:0001"})


def generate_gateway_token() -> str:
    """Mint a fresh long-lived gateway token (URL-safe, ~256 bits)."""
    return secrets.token_urlsafe(32)


def is_loopback_host(host: str) -> bool:
    """True if ``host`` is a loopback address (auth optional)."""
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def token_matches(provided: str | None, expected: str) -> bool:
    """Constant-time compare a provided token against the expected one.

    Returns False if either side is empty so a misconfigured empty expected
    token never silently authenticates every request.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def extract_request_token(request: web.Request) -> str | None:
    """Pull the static token from a REST request.

    Order: ``X-Flowly-Token`` header, then ``Authorization: Bearer <token>``.
    """
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def host_origin_allowed(request: web.Request) -> bool:
    """Anti-DNS-rebinding guard for the WebSocket upgrade.

    The credential is the real boundary, so this is defence-in-depth, kept
    deliberately lenient:

    * Non-browser clients (Electron ``file://`` / ``app://`` / ``null`` /
      no Origin at all — the desktop and TUI) are always allowed: there is no
      browser same-origin model to abuse.
    * Browser clients (http/https Origin) must have an Origin host that
      matches the request's Host header, which blocks a malicious web page
      from scripting a cross-origin WS to a gateway on the user's network.
    """
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return True
    scheme = urlsplit(origin).scheme.lower()
    if scheme not in ("http", "https"):
        # file://, app://, null, chrome-extension:// … not a web page.
        return True
    origin_host = urlsplit(origin).hostname or ""
    request_host = (request.host or "").split(":")[0]
    return bool(origin_host) and origin_host.lower() == request_host.lower()


def loopback_ws_allowed(request: web.Request) -> bool:
    """WS-upgrade gate for the token-less LOOPBACK gateway (defence in depth).

    With no credential to enforce, the socket must still not be reachable from
    a web page the embedded browser (or any browser) visits. Two cheap checks,
    both of which every legitimate local client passes:

    * ``host_origin_allowed`` — a cross-origin web page (Origin host ≠ request
      Host) is rejected; native clients (no Origin) and non-web schemes
      (``chrome-extension://`` / ``file://``) pass.
    * loopback Host — the request's Host must be a loopback name. This defeats
      DNS rebinding, where a rebound ``attacker.com`` sends matching
      Origin+Host (passing the check above) but a non-loopback Host.

    Falls back to the origin check alone when the host can't be read, so a
    legitimate native client is never blocked by a parsing gap.
    """
    if not host_origin_allowed(request):
        return False
    host = request.url.host
    return host is None or is_loopback_host(host)


class WsTicketStore:
    """In-memory single-use ticket store for WS-upgrade authentication.

    Tickets are minted by an authenticated REST call and consumed exactly
    once by the matching ``/ws?ticket=`` upgrade. Expired/reused/unknown
    tickets fail closed.
    """

    def __init__(self, ttl_seconds: int = TICKET_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        # ticket -> expiry monotonic deadline
        self._tickets: dict[str, float] = {}

    def _now(self) -> float:
        return time.monotonic()

    def _purge(self) -> None:
        now = self._now()
        expired = [t for t, deadline in self._tickets.items() if deadline <= now]
        for t in expired:
            self._tickets.pop(t, None)

    def mint(self) -> str:
        """Create a new single-use ticket and return it."""
        self._purge()
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = self._now() + self._ttl
        return ticket

    def consume(self, ticket: str | None) -> bool:
        """Validate and burn a ticket. True only if it was live and unused."""
        if not ticket:
            return False
        self._purge()
        deadline = self._tickets.pop(ticket, None)
        if deadline is None:
            return False
        return deadline > self._now()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl
