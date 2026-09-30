"""Authenticate original event ownership on the private managed-runtime hop."""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from pathlib import Path

from flowly.live_voice.authority import (
    ProfileHopVerifier,
    RequestOwner,
    VoiceAuthorityError,
    sign_profile_hop,
)
from flowly.live_voice.events import EventAccess, ScopedEvent
from flowly.session.control_access import SessionControlScope
from flowly.session.ownership import SessionAccessError, owner_metadata

_FIELD = 'voiceEventAuthority'
_METHOD = 'runtime.voice.event'


def _decode_access(value: object, sessions_dir: Path) -> EventAccess:
    if (not isinstance(value, dict) or set(value) != {'scopes', 'owner', 'blocked', 'canonical'}
            or type(value['blocked']) is not bool or type(value['canonical']) is not bool
            or not isinstance(value['scopes'], list) or len(value['scopes']) > 8):
        raise VoiceAuthorityError()
    scopes = []
    try:
        for item in value['scopes']:
            if not isinstance(item, dict) or set(item) != {'sessionKey', 'owner'}:
                raise VoiceAuthorityError()
            scopes.append(SessionControlScope.bind(item['sessionKey'], item['owner'], sessions_dir=sessions_dir))
        extra = SessionControlScope.bind(None, value['owner']).owner
    except SessionAccessError:
        raise VoiceAuthorityError() from None
    owner = RequestOwner(extra.get('uid')) if extra is not None else None
    return EventAccess(scopes=tuple(scopes), owner=owner, blocked=value['blocked'], canonical=value['canonical'])


def sign_profile_event(key: str, instance_id: str, payload: dict, access: EventAccess, *, sessions_dir: Path) -> dict:
    """Paths stay local. The recipient reconstructs them from its profile root."""
    public = {name: value for name, value in payload.items() if name != _FIELD}
    if public.get('type') != 'event' or not isinstance(public.get('event'), str):
        raise VoiceAuthorityError()
    wire = {'scopes': [{'sessionKey': scope.key, 'owner': scope.owner} for scope in access.scopes],
            'owner': owner_metadata(access.owner) if access.owner is not None else None,
            'blocked': access.blocked, 'canonical': access.canonical}
    normalized = _decode_access(wire, sessions_dir)
    if any(original.path != projected.path for original, projected in zip(access.scopes, normalized.scopes, strict=True)):
        raise VoiceAuthorityError()
    event_id = secrets.token_urlsafe(18)
    proof = sign_profile_hop(key, instance_id, event_id, _METHOD, {'frame': public, 'access': wire}, None)
    return {**public, _FIELD: {'eventId': event_id, 'access': wire, 'proof': proof}}


class ProfileEventVerifier:
    def __init__(self, instance_id: str, *, key: str, now: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        # This cache is separate from incoming RPC IDs. Bursty text streams may
        # contain more events than requests in the same 45-second replay window.
        wall_start, monotonic_start = now(), monotonic()

        def effective_now():
            return max(now(), wall_start + monotonic() - monotonic_start)

        self._proof = ProfileHopVerifier(instance_id, key=key, max_requests=16384,
                                        now=effective_now, monotonic=monotonic)

    def verify(self, frame: dict, *, sessions_dir: Path) -> ScopedEvent:
        authority = frame.get(_FIELD)
        if (frame.get('type') != 'event' or not isinstance(frame.get('event'), str)
                or not isinstance(authority, dict) or set(authority) != {'eventId', 'access', 'proof'}):
            raise VoiceAuthorityError()
        public = {name: value for name, value in frame.items() if name != _FIELD}
        owner = self._proof.verify(authority['eventId'], _METHOD, {'frame': public, 'access': authority['access']}, authority['proof'])
        if owner is not None:
            raise VoiceAuthorityError()
        access = _decode_access(authority['access'], sessions_dir)
        payload = public.get('data')
        session_key = (payload.get('sessionKey') or payload.get('session_key')) if isinstance(payload, dict) else None
        if session_key and any(scope.key is not None and scope.key != session_key for scope in access.scopes):
            raise VoiceAuthorityError()
        return ScopedEvent(public, access)
