"""Verify account identity for one host without receiving its Firebase credential.

This supplements host transport authentication. It does not grant gateway
access, provider credit, or authority outside account-owned voice resources.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ISSUER = 'https://useflowlyapp.com/live-voice'
MAX_ACCESS_SECONDS = 300
KEY_CACHE_SECONDS = 60
_HOST = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')
_KEY_ID = re.compile(r'[A-Za-z0-9_-]{43}')
_JWT = re.compile(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+')


class VoiceAccessError(RuntimeError):
    def __init__(self, *, unavailable: bool = False):
        self.code = 'VOICE_AUTH_UNAVAILABLE' if unavailable else 'VOICE_AUTH_REQUIRED'
        super().__init__('Voice identity verification is unavailable.' if unavailable
                         else 'Voice access could not be verified.')


@dataclass(frozen=True)
class VoicePrincipal:
    uid: str
    host_id: str
    expires_at: int
    credential_id: str


def _public_keys(value: object) -> dict[str, Ed25519PublicKey]:
    if not isinstance(value, dict) or set(value) != {'keys'}:
        raise ValueError('Invalid discovery')
    rows = value['keys']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ValueError('Invalid discovery')
    keys = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'kty', 'crv', 'x', 'kid', 'alg', 'use'}:
            raise ValueError('Invalid public key')
        if (row['kty'], row['crv'], row['alg'], row['use']) != ('OKP', 'Ed25519', 'EdDSA', 'sig'):
            raise ValueError('Invalid public key')
        x, kid = row['x'], row['kid']
        if not isinstance(x, str) or not _KEY_ID.fullmatch(x) or not isinstance(kid, str):
            raise ValueError('Invalid public key')
        raw = base64.urlsafe_b64decode(x + '=')
        if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip('=') != x:
            raise ValueError('Invalid public key')
        canonical = json.dumps({'crv': 'Ed25519', 'kty': 'OKP', 'x': x}, separators=(',', ':'), sort_keys=True)
        expected = base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).decode().rstrip('=')
        if kid != expected or kid in keys:
            raise ValueError('Invalid key identity')
        keys[kid] = Ed25519PublicKey.from_public_bytes(raw)
    return keys


async def _fetch_keys() -> dict:
    # Administrator-controlled base only; tokens cannot select a URL or redirect.
    base = os.environ.get('FLOWLY_API_BASE', 'https://useflowlyapp.com').rstrip('/')
    url = urlsplit(base)
    if (url.username or url.password or url.query or url.fragment or not url.hostname
            or (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'}))):
        raise VoiceAccessError(unavailable=True)
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        async with client.stream('GET', f'{base}/api/live-voice/keys') as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                body.extend(chunk)
                if len(body) > 32768:
                    raise VoiceAccessError(unavailable=True)
            return json.loads(body)


class VoiceAccessVerifier:
    def __init__(self, *, fetch_keys: Callable[[], Awaitable[dict]] = _fetch_keys,
                 monotonic: Callable[[], float] = time.monotonic,
                 now: Callable[[], float] = time.time):
        self._fetch_keys = fetch_keys
        self._monotonic = monotonic
        self._now = now
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._expires = 0.0
        self._unknown_refresh = 0.0
        self._retry_after = 0.0
        self._lock = asyncio.Lock()

    async def _key(self, kid: str) -> Ed25519PublicKey:
        async with self._lock:
            now = self._monotonic()
            if now < self._expires and kid in self._keys:
                return self._keys[kid]
            if now < self._retry_after:
                raise VoiceAccessError(unavailable=True)
            if now < self._expires and now < self._unknown_refresh:
                raise VoiceAccessError()
            try:
                keys = _public_keys(await self._fetch_keys())
            except Exception:
                self._retry_after = self._monotonic() + 10
                raise VoiceAccessError(unavailable=True) from None
            self._keys = keys
            self._expires = self._monotonic() + KEY_CACHE_SECONDS
            self._unknown_refresh = self._monotonic() + 30
            self._retry_after = 0.0
            if kid not in keys:
                raise VoiceAccessError()
            return keys[kid]

    async def verify(self, token: object, host_id: str) -> VoicePrincipal:
        if (not isinstance(token, str) or not 1 <= len(token) <= 2048 or not _JWT.fullmatch(token)
                or not isinstance(host_id, str) or not _HOST.fullmatch(host_id)):
            raise VoiceAccessError()
        try:
            header = jwt.get_unverified_header(token)
            if (set(header) != {'alg', 'typ', 'kid'} or header['alg'] != 'EdDSA'
                    or header['typ'] != 'flowly-live-voice+jwt' or not isinstance(header['kid'], str)
                    or not _KEY_ID.fullmatch(header['kid'])):
                raise ValueError('Invalid header')
        except Exception:
            raise VoiceAccessError() from None
        key = await self._key(header['kid'])
        try:
            claims = jwt.decode(token, key, algorithms=['EdDSA'], issuer=ISSUER,
                                audience=f'flowly-live-voice:{host_id}', options={
                                    'require': ['iss', 'aud', 'sub', 'scope', 'iat', 'exp', 'jti'],
                                    'strict_aud': True, 'verify_exp': False, 'verify_iat': False,
                                })
            if set(claims) != {'iss', 'aud', 'sub', 'scope', 'iat', 'exp', 'jti'} or claims['scope'] != 'voice':
                raise ValueError('Invalid claims')
            uid, iat, exp, credential = claims['sub'], claims['iat'], claims['exp'], claims['jti']
            now = self._now()
            if (not isinstance(uid, str) or not 1 <= len(uid) <= 128
                    or any(ord(char) < 32 or ord(char) == 127 for char in uid)
                    or type(iat) is not int or type(exp) is not int
                    or iat > now + 15 or exp <= now or not 0 < exp - iat <= MAX_ACCESS_SECONDS
                    or not isinstance(credential, str) or not 1 <= len(credential) <= 64
                    or not re.fullmatch(r'[A-Za-z0-9._-]+', credential)):
                raise ValueError('Invalid claims')
            return VoicePrincipal(uid, host_id, exp, credential)
        except Exception:
            raise VoiceAccessError() from None
