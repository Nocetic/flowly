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

# Playback tickets live longer than WS tickets: the user may open a clip minutes
# after the reply arrived, and seeking keeps re-requesting bytes for as long as
# the player is on screen. Still short enough that a ticket found in a proxy log
# is already dead.
MEDIA_TICKET_TTL_SECONDS = 15 * 60

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


class MediaTicketStore:
    """Short-lived, media-scoped tickets for byte-range playback.

    A ``<video>`` element and ``AVPlayer`` both fetch by plain URL and cannot
    attach the ``X-Flowly-Token`` header, so playback needs a credential that
    can live in a query string. Putting the long-lived gateway token there is
    not an option — it would be parked in proxy logs, history and referrers.

    So a ticket is minted by an already-authenticated request and is:

    * **bound to one media id** — it unlocks that file and nothing else, so a
      leaked playback URL can't be walked into an arbitrary read;
    * **short-lived** — expiry is checked on every request, not just the first;
    * **reusable, unlike the WS ticket** — a player issues many Range requests
      for a single clip, so burning the ticket on first use would break
      playback the moment the user seeks.

    The store is bounded: a client that mints without ever playing evicts its
    own oldest tickets rather than growing the process forever.
    """

    def __init__(
        self,
        ttl_seconds: int = MEDIA_TICKET_TTL_SECONDS,
        max_entries: int = 512,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max(1, max_entries)
        # ticket -> (media_id, expiry monotonic deadline)
        self._tickets: dict[str, tuple[str, float]] = {}

    def _now(self) -> float:
        return time.monotonic()

    def _purge(self) -> None:
        now = self._now()
        for ticket in [t for t, (_, deadline) in self._tickets.items() if deadline <= now]:
            self._tickets.pop(ticket, None)
        # Insertion-ordered dict: the oldest mints go first when over cap.
        while len(self._tickets) > self._max_entries:
            self._tickets.pop(next(iter(self._tickets)), None)

    def mint(self, media_id: str) -> str:
        """Create a ticket that unlocks ``media_id`` only."""
        if not media_id:
            raise ValueError("media_id is required")
        self._purge()
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (media_id, self._now() + self._ttl)
        return ticket

    def resolve(self, ticket: str | None) -> str | None:
        """Return the media id a live ticket unlocks, else ``None``."""
        if not ticket:
            return None
        self._purge()
        entry = self._tickets.get(str(ticket))
        if entry is None:
            return None
        media_id, deadline = entry
        if deadline <= self._now():
            self._tickets.pop(str(ticket), None)
            return None
        return media_id

    def allows(self, ticket: str | None, media_id: str) -> bool:
        """True when ``ticket`` is live AND scoped to exactly ``media_id``."""
        resolved = self.resolve(ticket)
        if resolved is None or not media_id:
            return False
        return hmac.compare_digest(resolved, media_id)

    def revoke(self, ticket: str | None) -> None:
        if ticket:
            self._tickets.pop(str(ticket), None)

    @property
    def ttl_seconds(self) -> int:
        return self._ttl
