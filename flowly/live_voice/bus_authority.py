"""Private authority travels with queued messages, never in client metadata."""
from __future__ import annotations

from contextlib import contextmanager
from copy import copy

from flowly.live_voice.authority import (
    VoiceAuthorityError,
    current_request_owner,
    request_owner_scope,
)
from flowly.live_voice.events import EventAccess, current_event_access, event_access_scope


def bind_bus_message(message, *, inbound: bool):
    """Make a queue-owned copy so republishing cannot rewrite an earlier source."""
    access = getattr(message, '_flowly_event_access', None)
    if not isinstance(access, EventAccess):
        access = current_event_access()
    if access is None:
        key = message.session_key if inbound else message.metadata.get('session_key')
        access = EventAccess.capture(key)
        try:
            if access.producer().uid is not None and current_request_owner() is None:
                access = EventAccess(blocked=True)
        except VoiceAuthorityError:
            access = EventAccess(blocked=True)
    queued = copy(message)
    # Deliberately not a dataclass field: metadata, asdict and public DTOs do
    # not serialize the original account or its canonical filesystem path.
    queued._flowly_event_access = access
    return queued


@contextmanager
def bus_message_scope(message):
    access = getattr(message, '_flowly_event_access', None)
    if not isinstance(access, EventAccess):
        # Existing direct/internal callers may bypass MessageBus entirely.
        yield True
        return
    try:
        owner = access.producer()
    except VoiceAuthorityError:
        yield False
        return
    if not access.permits(owner):
        yield False
        return
    with request_owner_scope(owner), event_access_scope(access):
        yield True
