"""Request-local identity and authenticated hops to managed profile runtimes."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator


class VoiceAuthorityError(RuntimeError):
    def __init__(self, *, unavailable: bool = False):
        self.code = 'VOICE_AUTH_UNAVAILABLE' if unavailable else 'VOICE_AUTH_REQUIRED'
        super().__init__('Profile request authority is unavailable.' if unavailable
                         else 'Profile request authority could not be verified.')


@dataclass(frozen=True)
class RequestOwner:
    uid: str | None = None

    def __post_init__(self):
        if self.uid is not None and (not isinstance(self.uid, str) or not 1 <= len(self.uid) <= 128
                                     or any(ord(char) < 32 or ord(char) == 127 for char in self.uid)):
            raise VoiceAuthorityError()


HOST_OWNER = RequestOwner()
# None is trusted in-process work. Every external ingress must explicitly enter
# HOST_OWNER or a verified account scope; a missing bearer is never internal.
_owner: ContextVar[RequestOwner | None] = ContextVar('flowly_voice_request_owner', default=None)


def current_request_owner() -> RequestOwner | None:
    return _owner.get()


@contextmanager
def request_owner_scope(owner: RequestOwner | None) -> Iterator[None]:
    token = _owner.set(owner)
    try:
        yield
    finally:
        _owner.reset(token)


def _owner_wire(owner: RequestOwner | None) -> dict:
    return {'kind': 'internal'} if owner is None else (
        {'kind': 'account', 'uid': owner.uid} if owner.uid is not None else {'kind': 'host'})


def _read_owner(value: object) -> RequestOwner | None:
    if value == {'kind': 'internal'}:
        return None
    if value == {'kind': 'host'}:
        return HOST_OWNER
    if isinstance(value, dict) and set(value) == {'kind', 'uid'} and value['kind'] == 'account' and value['uid'] is not None:
        return RequestOwner(value['uid'])
    raise VoiceAuthorityError()


def valid_parent_key(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _identity(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value) is not None


def _mac(key: str, request_id: str, method: str, params: dict, proof: dict) -> str:
    if not valid_parent_key(key) or not _identity(request_id) or not _identity(method) or not isinstance(params, dict):
        raise VoiceAuthorityError()
    digest = hmac.new(bytes.fromhex(key), digestmod=hashlib.sha256)
    material = {'requestId': request_id, 'method': method, 'params': params, 'authority': proof}
    size = 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False,
                                      separators=(',', ':')).iterencode(material):
            encoded = chunk.encode('utf-8')
            size += len(encoded)
            if size > 40 * 1024 * 1024:
                raise VoiceAuthorityError()
            digest.update(encoded)
    except (TypeError, ValueError, UnicodeError):
        raise VoiceAuthorityError() from None
    return digest.hexdigest()


def sign_profile_hop(key: str, instance_id: str, request_id: str, method: str,
                     params: dict, owner: RequestOwner | None, *, now: int | None = None) -> dict:
    if not _identity(instance_id):
        raise VoiceAuthorityError()
    issued_at = int(time.time()) if now is None else now
    if type(issued_at) is not int:
        raise VoiceAuthorityError()
    proof = {'version': 1, 'instanceId': instance_id, 'issuedAt': issued_at, 'owner': _owner_wire(owner)}
    return {**proof, 'mac': _mac(key, request_id, method, params, proof)}


class ProfileHopVerifier:
    def __init__(self, instance_id: str, *, key: str | None = None,
                 now: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, max_requests: int = 4096):
        if not _identity(instance_id) or (key is not None and not valid_parent_key(key)):
            raise VoiceAuthorityError()
        self.instance_id = instance_id
        self.key = key if key is not None else secrets.token_hex(32)
        self._now = now
        self._monotonic = monotonic
        self._seen: dict[str, float] = {}
        self._max_requests = max_requests

    def verify(self, request_id: str, method: str, params: dict, proof: object) -> RequestOwner | None:
        if not isinstance(proof, dict) or set(proof) != {'version', 'instanceId', 'issuedAt', 'owner', 'mac'}:
            raise VoiceAuthorityError()
        issued = proof['issuedAt']
        now = self._now()
        if (type(proof['version']) is not int or proof['version'] != 1 or proof['instanceId'] != self.instance_id
                or type(issued) is not int or issued < now - 30 or issued > now + 5
                or not valid_parent_key(proof['mac'])):
            raise VoiceAuthorityError()
        unsigned = {key: value for key, value in proof.items() if key != 'mac'}
        expected = _mac(self.key, request_id, method, params, unsigned)
        if not hmac.compare_digest(expected, proof['mac']):
            raise VoiceAuthorityError()
        owner = _read_owner(proof['owner'])
        monotonic = self._monotonic()
        self._seen = {key: deadline for key, deadline in self._seen.items() if deadline > monotonic}
        if request_id in self._seen:
            raise VoiceAuthorityError()
        if len(self._seen) >= self._max_requests:
            raise VoiceAuthorityError(unavailable=True)
        self._seen[request_id] = monotonic + 45
        return owner
