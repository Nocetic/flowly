"""Secret requests the agent opened and the person has not answered yet.

A request is task state, not a paused turn: the agent asks (``request_secret``)
and ends its reply; the request waits, durable across restarts, until the
person saves the values, declines, or it expires. Then the conversation gets a
turn of its own that says which (names only), as a screen step does
(``agent/tools/computer_handoff.py``).

Every surface that shows the conversation shows a request as a card
(``secret.requested`` / ``secret.closed``). Never the clarify wire: a surface
that does not know a kind shows it as a question, and what the person typed
into it would reach the agent as a plain answer.

``<FLOWLY_HOME>/secrets/requests.json`` holds what was asked (names, labels,
wording, the conversation); never a value.
"""

from __future__ import annotations

import asyncio
import json
import secrets as _random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

REQUESTS_FILE = "requests.json"
#: How long a request waits for the person.
LIFETIME_SECONDS = 7 * 24 * 3600
MAX_PENDING = 20
MAX_FIELDS = 6
MAX_TITLE_CHARS = 120
MAX_LABEL_CHARS = 80
MAX_DESCRIPTION_CHARS = 500

#: How a request ended. ``superseded``: a later request asked for one of its names.
CLOSE_REASONS = ("saved", "declined", "expired", "superseded")


@dataclass
class SecretRequest:
    id: str
    session_key: str
    channel: str
    chat_id: str
    title: str
    description: str
    fields: list[dict[str, str]]
    replace: bool
    created_at: float
    expires_at: float
    key: dict[str, str] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return [item["name"] for item in self.fields]

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id, "sessionKey": self.session_key, "title": self.title,
            "description": self.description, "fields": [dict(item) for item in self.fields],
            "replace": self.replace, "key": dict(self.key),
            "createdAt": self.created_at, "expiresAt": self.expires_at,
        }


def closed_to_wire(request: SecretRequest, reason: str) -> dict[str, Any]:
    return {"id": request.id, "sessionKey": request.session_key, "reason": reason, "names": request.names}


RequestedCallback = Callable[[SecretRequest], Awaitable[None]]
ClosedCallback = Callable[[SecretRequest, str], Awaitable[None]]
#: (session_key, channel, chat_id, notice): start the conversation's next turn.
Continuation = Callable[[str, str, str, str], Awaitable[None]]


def continue_notice(request: SecretRequest, reason: str) -> str | None:
    """What the agent is told in its conversation when a request ends."""
    names = ", ".join(request.names)
    if reason == "saved":
        return (f"[Secrets] The person saved {names}. Your commands now have "
                + ", ".join(f"${name}" for name in request.names)
                + " in their environment ($env:NAME in PowerShell). Never print, echo or write a value; "
                  "continue the task.")
    if reason == "declined":
        return (f"[Secrets] The person declined to save {names}. Do not ask for it in chat. "
                "Tell them in one short sentence what you cannot do without it, then go on without it.")
    if reason == "expired":
        return (f"[Secrets] Nobody saved {names} within a week; the request expired. "
                "Do not ask for it in chat; ask again with request_secret only when it is needed.")
    return None


class SecretRequests:
    """The pending requests of one home, and the callbacks that show and end them."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.RLock()
        self._requested: list[RequestedCallback] = []
        self._closed: list[ClosedCallback] = []
        self._continuation: Continuation | None = None

    @property
    def path(self) -> Path:
        return self.directory / REQUESTS_FILE

    # ── surfaces ───────────────────────────────────────────────────────

    def add_requested_callback(self, callback: RequestedCallback) -> None:
        self._requested.append(callback)

    def add_closed_callback(self, callback: ClosedCallback) -> None:
        self._closed.append(callback)

    def set_continuation(self, continuation: Continuation | None) -> None:
        self._continuation = continuation

    # ── store ──────────────────────────────────────────────────────────

    def _read(self) -> list[SecretRequest]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("[Secrets] pending requests unreadable ({}); starting empty", type(exc).__name__)
            return []
        items = raw.get("requests") if isinstance(raw, dict) else None
        requests: list[SecretRequest] = []
        for item in items if isinstance(items, list) else []:
            try:
                requests.append(SecretRequest(**item))
            except TypeError:
                continue
        return requests

    def _write(self, requests: list[SecretRequest]) -> None:
        from flowly.secrets.vault import _write_atomic
        from flowly.utils.file_security import secure_dir, secure_file

        self.directory.mkdir(parents=True, exist_ok=True)
        secure_dir(self.directory)
        _write_atomic(self.path, json.dumps({"requests": [asdict(r) for r in requests]}).encode("utf-8"))
        secure_file(self.path)

    def pending(self) -> list[SecretRequest]:
        now = time.time()
        with self._lock:
            return [r for r in self._read() if r.expires_at > now]

    def get(self, request_id: str) -> SecretRequest | None:
        return next((r for r in self.pending() if r.id == request_id), None)

    # ── life of a request ──────────────────────────────────────────────

    async def open(self, *, session_key: str, channel: str, chat_id: str, title: str, description: str,
                   fields: list[dict[str, str]], replace: bool, key: dict[str, str]) -> SecretRequest:
        """Record a request and show it; an earlier one asking for any of its names ends as superseded."""
        now = time.time()
        request = SecretRequest(
            id=f"sr_{_random.token_hex(8)}", session_key=session_key, channel=channel, chat_id=chat_id,
            title=title, description=description, fields=fields, replace=replace,
            created_at=now, expires_at=now + LIFETIME_SECONDS, key=dict(key),
        )
        names = set(request.names)
        with self._lock:
            current = self._read()
            live = [r for r in current if r.expires_at > now]
            expired = [r for r in current if r.expires_at <= now]
            superseded = [r for r in live if names & set(r.names)]
            kept = [r for r in live if r not in superseded]
            if len(kept) >= MAX_PENDING:
                raise ValueError(f"At most {MAX_PENDING} secret requests can wait at once.")
            self._write([*kept, request])
        for old in superseded:
            await self._announce_closed(old, "superseded")
        for old in expired:
            await self._end(old, "expired")
        for callback in list(self._requested):
            try:
                await callback(request)
            except Exception as exc:
                logger.error("[Secrets] request callback failed ({})", type(exc).__name__)
        return request

    async def close(self, request_id: str, reason: str) -> SecretRequest | None:
        """End a pending request once; ``None`` if it was not pending."""
        if reason not in CLOSE_REASONS:
            raise ValueError(f"unknown close reason: {reason}")
        with self._lock:
            current = self._read()
            request = next((r for r in current if r.id == request_id), None)
            if request is None:
                return None
            self._write([r for r in current if r.id != request_id])
        await self._end(request, reason)
        return request

    async def sweep(self) -> int:
        """End the requests whose week is over; returns how many."""
        now = time.time()
        with self._lock:
            current = self._read()
            expired = [r for r in current if r.expires_at <= now]
            if expired:
                self._write([r for r in current if r.expires_at > now])
        for request in expired:
            await self._end(request, "expired")
        return len(expired)

    async def run_expiry(self, interval: float = 3600.0) -> None:
        """Sweep now and then, for as long as the agent runs."""
        while True:
            try:
                await self.sweep()
            except Exception as exc:
                logger.error("[Secrets] expiry sweep failed ({})", type(exc).__name__)
            await asyncio.sleep(interval)

    async def _announce_closed(self, request: SecretRequest, reason: str) -> None:
        for callback in list(self._closed):
            try:
                await callback(request, reason)
            except Exception as exc:
                logger.error("[Secrets] close callback failed ({})", type(exc).__name__)

    async def _end(self, request: SecretRequest, reason: str) -> None:
        await self._announce_closed(request, reason)
        notice = continue_notice(request, reason)
        if notice is None or self._continuation is None:
            return
        try:
            await self._continuation(request.session_key, request.channel, request.chat_id, notice)
        except Exception as exc:
            logger.error("[Secrets] could not continue the conversation ({})", type(exc).__name__)


_managers: dict[str, SecretRequests] = {}
_managers_lock = threading.Lock()


def get_secret_requests() -> SecretRequests:
    """The running agent's requests."""
    from flowly.profile import get_flowly_home

    directory = get_flowly_home() / "secrets"
    with _managers_lock:
        manager = _managers.get(str(directory))
        if manager is None:
            manager = _managers[str(directory)] = SecretRequests(directory)
        return manager


def _reset_for_tests() -> None:
    with _managers_lock:
        _managers.clear()
