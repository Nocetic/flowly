"""Authenticated relay browser identity, independent of client-supplied fields."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from flowly.live_voice.authority import RequestOwner, VoiceAuthorityError, valid_parent_key

CAPABILITY = 'voice-relay-authority-v1'
_MAX_SEQUENCE = 2**53 - 1


def _identity(value: object, limit: int) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= limit
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


@dataclass(frozen=True)
class RelayPrincipal:
    uid: str
    server_id: str
    session_id: str
    conversation_id: str | None
    expires_at: int
    link_id: str


class RelayMessage(dict):
    def __init__(self, payload: dict, principal: RelayPrincipal, kind: str):
        super().__init__(payload)
        self.principal, self.kind = principal, kind


class RelayBrowserVerifier:
    def __init__(self, handshake: object, *, server_id: str,
                 now: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic):
        if (not isinstance(handshake, dict) or set(handshake) != {'version', 'linkId', 'serverId', 'key'}
                or type(handshake['version']) is not int or handshake['version'] != 1
                or not _identity(handshake['linkId'], 128) or not _identity(server_id, 256)
                or handshake['serverId'] != server_id or not valid_parent_key(handshake['key'])):
            raise VoiceAuthorityError()
        self.link_id = handshake['linkId']
        self.server_id = server_id
        self._key = bytes.fromhex(handshake['key'])
        self._sequence = 0
        self._now, self._monotonic = now, monotonic
        self._wall_start, self._monotonic_start = now(), monotonic()

    def now(self) -> float:
        return max(self._now(), self._wall_start + self._monotonic() - self._monotonic_start)

    def verify(self, frame: object) -> RelayMessage:
        if (not isinstance(frame, dict) or set(frame) != {'type', 'body', 'authority', 'mac'}
                or frame['type'] != 'relay.browser' or not isinstance(frame['body'], str)
                or not valid_parent_key(frame['mac'])):
            raise VoiceAuthorityError()
        authority, body = frame['authority'], frame['body']
        if (not isinstance(authority, dict) or set(authority) != {'version', 'linkId', 'sequence', 'kind',
                'userId', 'serverId', 'sessionId', 'conversationId', 'expiresAt'}
                or type(authority['version']) is not int or authority['version'] != 1
                or authority['linkId'] != self.link_id or authority['serverId'] != self.server_id
                or not isinstance(authority['kind'], str) or authority['kind'] not in {'request', 'connected', 'disconnected'}
                or not _identity(authority['userId'], 128) or not _identity(authority['sessionId'], 256)
                or (authority['conversationId'] is not None and not _identity(authority['conversationId'], 512))
                or type(authority['expiresAt']) is not int
                or not self.now() < authority['expiresAt'] <= self.now() + 305
                or type(authority['sequence']) is not int or not self._sequence < authority['sequence'] <= _MAX_SEQUENCE):
            raise VoiceAuthorityError()
        material = ['flowly-relay-browser-v1', authority['linkId'], str(authority['sequence']),
                    str(authority['expiresAt']), authority['kind'], authority['userId'], authority['serverId'],
                    authority['sessionId'], authority['conversationId'] or '', body]
        try:
            if len(body.encode('utf-8')) > 10 * 1024 * 1024:
                raise VoiceAuthorityError()
            expected = hmac.new(self._key, '\0'.join(material).encode('utf-8'), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, frame['mac']):
                raise VoiceAuthorityError()
            payload = json.loads(body)
        except (UnicodeError, ValueError):
            raise VoiceAuthorityError() from None
        if not isinstance(payload, dict) or payload.get('sessionId') != authority['sessionId']:
            raise VoiceAuthorityError()
        kind = authority['kind']
        expected_types = {'request': {'rpc', 'ping'}, 'connected': {'browser-connected'}, 'disconnected': {'browser-disconnected'}}
        if not isinstance(payload.get('type'), str) or payload['type'] not in expected_types[kind]:
            raise VoiceAuthorityError()
        # Validate account shape using the same identity type as certificate
        # admission. It remains private Python state, not RPC parameters.
        owner = RequestOwner(authority['userId'])
        principal = RelayPrincipal(owner.uid, self.server_id, authority['sessionId'], authority['conversationId'],
                                   authority['expiresAt'], self.link_id)
        self._sequence = authority['sequence']
        return RelayMessage(payload, principal, kind)
