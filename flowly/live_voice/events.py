"""Original event authority and revocable account leases for live recipients."""
from __future__ import annotations

import asyncio
import time
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable

from flowly.live_voice.access import VoicePrincipal
from flowly.live_voice.authority import (
    HOST_OWNER,
    RequestOwner,
    VoiceAuthorityError,
    current_request_owner,
    request_owner_scope,
)
from flowly.session.control_access import SessionControlScope
from flowly.session.ownership import SessionAccessError, owner_metadata


@dataclass(frozen=True)
class EventAccess:
    scopes: tuple[SessionControlScope, ...] = ()
    owner: RequestOwner | None = None
    blocked: bool = False
    canonical: bool = True

    def producer(self) -> RequestOwner:
        """Restore a verified source without inheriting a reader's authority."""
        owners = {self.owner} if self.owner is not None else set()
        for scope in self.scopes:
            if scope.owner is not None:
                owners.add(RequestOwner(scope.owner.get('uid')))
        if self.blocked or len(owners) > 1:
            raise VoiceAuthorityError()
        return next(iter(owners), HOST_OWNER)

    def permits(self, recipient: RequestOwner) -> bool:
        if self.blocked or (self.owner is not None and self.owner != recipient):
            return False
        with request_owner_scope(recipient):
            for scope in self.scopes:
                if not self.canonical:
                    if scope.owner is not None and scope.owner != owner_metadata(recipient):
                        return False
                    continue
                try:
                    with scope.guard(scope.key) as allowed:
                        if not allowed:
                            return False
                except (SessionAccessError, OSError):
                    return False
        return True

    @classmethod
    def capture(cls, key: str | None = None, *, sessions_dir=None):
        if key:
            try:
                return cls(scopes=(SessionControlScope.capture(key, sessions_dir=sessions_dir),))
            except (SessionAccessError, OSError):
                return cls(blocked=True)
        owner = current_request_owner()
        return cls(owner=owner if owner is not None and owner.uid is not None else None)


class ScopedEvent(dict):
    """Private in-process authority is deliberately absent from JSON fields."""

    def __init__(self, payload: dict, access: EventAccess):
        super().__init__(payload)
        self.access = access


_event_access: ContextVar[EventAccess | None] = ContextVar('flowly_event_access', default=None)


def current_event_access() -> EventAccess | None:
    return _event_access.get()


@contextmanager
def event_access_scope(access: EventAccess | SessionControlScope | None):
    token = _event_access.set(access if access is None or isinstance(access, EventAccess) else EventAccess(scopes=(access,)))
    try:
        yield
    finally:
        _event_access.reset(token)


@dataclass
class Recipient:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: RequestOwner = HOST_OWNER
    expires_at: float = 0
    deadline: float = 0
    incoming: int = 0
    retired: bool = False
    parent: bool = False


class EventRecipients:
    def __init__(self, *, now: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic):
        self.now, self.monotonic = now, monotonic
        self._entries: dict[int, tuple[Callable, Recipient]] = {}

    def get(self, socket) -> Recipient:
        key = id(socket)
        entry = self._entries.get(key)
        if entry is not None and entry[0]() is socket:
            return entry[1]
        state = Recipient()

        def remove(reference):
            if self._entries.get(key, (None,))[0] is reference:
                self._entries.pop(key, None)

        try:
            reference = weakref.ref(socket, remove)
        except TypeError:  # Small test/embedded socket adapters may lack weakrefs.
            def reference():
                return socket
        self._entries[key] = (reference, state)
        return state

    def begin(self, socket) -> int:
        state = self.get(socket)
        state.incoming += 1
        return state.incoming

    async def bind(self, socket, sequence: int, principal: VoicePrincipal | None) -> bool:
        state = self.get(socket)
        async with state.lock:
            if state.retired or sequence != state.incoming:
                return False
            if principal is None:
                state.owner, state.expires_at, state.deadline = HOST_OWNER, 0, 0
                return True
            remaining = principal.expires_at - self.now()
            if remaining <= 0:
                state.owner, state.expires_at, state.deadline = HOST_OWNER, 0, 0
                return False
            state.owner = RequestOwner(principal.uid)
            state.expires_at = principal.expires_at
            state.deadline = self.monotonic() + remaining
            return True

    def owner(self, state: Recipient) -> RequestOwner:
        if (state.owner.uid is not None and
                (self.now() >= state.expires_at or self.monotonic() >= state.deadline)):
            return HOST_OWNER
        return state.owner

    def retire(self, socket) -> None:
        state = self.get(socket)
        state.retired = True
        state.incoming += 1
        state.owner, state.expires_at, state.deadline = HOST_OWNER, 0, 0
