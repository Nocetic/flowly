"""Account identity is signed for one host; transport access remains separate."""
import asyncio
import base64
import hashlib
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from flowly.live_voice.access import VoiceAccessError, VoiceAccessVerifier, _fetch_keys

ISSUER = 'https://useflowlyapp.com/live-voice'
HOST = '3b3a37e1-9340-4a90-9103-71dd49e7b263'


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def fixture():
    key = Ed25519PrivateKey.generate()
    public = {'crv': 'Ed25519', 'kty': 'OKP', 'x': b64(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))}
    kid = b64(hashlib.sha256(json.dumps(public, separators=(',', ':'), sort_keys=True).encode()).digest())
    jwk = {**public, 'kid': kid, 'alg': 'EdDSA', 'use': 'sig'}
    now = int(time.time())
    claims = {'iss': ISSUER, 'aud': f'flowly-live-voice:{HOST}', 'sub': 'account-1',
              'scope': 'voice', 'iat': now, 'exp': now + 300, 'jti': 'test-credential'}

    def token(changes=None, headers=None):
        return jwt.encode({**claims, **(changes or {})}, key, algorithm='EdDSA',
                          headers={'kid': kid, 'typ': 'flowly-live-voice+jwt', **(headers or {})})

    calls = []

    async def fetch():
        calls.append(True)
        return {'keys': [jwk]}

    return token, jwk, fetch, calls, now


@pytest.mark.asyncio
async def test_verifies_identity_without_returning_or_remembering_the_bearer():
    token, _, fetch, calls, _ = fixture()
    verifier = VoiceAccessVerifier(fetch_keys=fetch)
    bearer = token()
    principal = await verifier.verify(bearer, HOST)
    assert principal.uid == 'account-1'
    assert principal.host_id == HOST
    assert principal.credential_id == 'test-credential'
    assert bearer not in repr(principal)
    await verifier.verify(bearer, HOST)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'sub': ''}, {'sub': 'a' * 129}, {'sub': 'a\x00b'}, {'sub': False},
    {'iss': 'https://another-issuer.example/live-voice'}, {'aud': 'another-host'},
    {'aud': [f'flowly-live-voice:{HOST}']}, {'scope': 'admin'}, {'scope': ['voice']},
    {'iat': True}, {'exp': True}, {'exp': '9999999999'}, {'jti': ''},
])
async def test_rejects_wrong_identity_scope_and_claim_types(change):
    token, _, fetch, _, _ = fixture()
    with pytest.raises(VoiceAccessError, match='Voice access could not be verified'):
        await VoiceAccessVerifier(fetch_keys=fetch).verify(token(change), HOST)


@pytest.mark.asyncio
@pytest.mark.parametrize('timing', ['expired', 'future', 'too-long', 'backwards'])
async def test_rejects_invalid_lifetimes(timing):
    token, _, fetch, _, now = fixture()
    changes = {'expired': {'exp': now}, 'future': {'iat': now + 60, 'exp': now + 300},
               'too-long': {'exp': now + 301}, 'backwards': {'iat': now, 'exp': now - 1}}[timing]
    with pytest.raises(VoiceAccessError):
        await VoiceAccessVerifier(fetch_keys=fetch).verify(token(changes), HOST)


@pytest.mark.asyncio
@pytest.mark.parametrize('header', [{'typ': 'JWT'}, {'jku': 'https://attacker.invalid/keys'}, {'crit': ['exp']}, {'kid': '../key'}])
async def test_rejects_header_overrides_without_any_network(header):
    token, _, fetch, calls, _ = fixture()
    with pytest.raises(VoiceAccessError):
        await VoiceAccessVerifier(fetch_keys=fetch).verify(token(headers=header), HOST)
    assert not calls


@pytest.mark.asyncio
async def test_coalesces_concurrent_key_discovery_and_bounds_unknown_key_refreshes():
    token, _, fetch, calls, _ = fixture()
    clock = [100.0]
    verifier = VoiceAccessVerifier(fetch_keys=fetch, monotonic=lambda: clock[0])
    await asyncio.gather(*(verifier.verify(token(), HOST) for _ in range(12)))
    assert len(calls) == 1
    for _ in range(10):
        with pytest.raises(VoiceAccessError):
            await verifier.verify(token(headers={'kid': 'a' * 43}), HOST)
    assert len(calls) == 1
    clock[0] += 31
    with pytest.raises(VoiceAccessError):
        await verifier.verify(token(headers={'kid': 'a' * 43}), HOST)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_expired_key_cache_is_not_used_after_discovery_failure():
    token, jwk, _, _, _ = fixture()
    clock = [100.0]
    calls = []

    async def fetch():
        calls.append(True)
        if len(calls) > 1:
            raise OSError('private transport details')
        return {'keys': [jwk]}

    verifier = VoiceAccessVerifier(fetch_keys=fetch, monotonic=lambda: clock[0])
    await verifier.verify(token(), HOST)
    clock[0] += 61
    for _ in range(3):
        with pytest.raises(VoiceAccessError) as error:
            await verifier.verify(token(), HOST)
        assert error.value.code == 'VOICE_AUTH_UNAVAILABLE'
        assert 'private transport details' not in str(error.value)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'kty': 'RSA'}, {'crv': 'Ed448'}, {'d': 'private'}, {'kid': 'a' * 43}, {'use': 'enc'}])
async def test_rejects_malformed_or_private_discovery_keys(change):
    token, jwk, _, _, _ = fixture()

    async def fetch():
        return {'keys': [{**jwk, **change}]}

    with pytest.raises(VoiceAccessError):
        await VoiceAccessVerifier(fetch_keys=fetch).verify(token(), HOST)


@pytest.mark.asyncio
async def test_cancelled_discovery_can_be_retried_without_a_poisoned_lock():
    token, jwk, _, _, _ = fixture()
    calls = []

    async def fetch():
        calls.append(True)
        if len(calls) == 1:
            raise asyncio.CancelledError
        return {'keys': [jwk]}

    verifier = VoiceAccessVerifier(fetch_keys=fetch)
    with pytest.raises(asyncio.CancelledError):
        await verifier.verify(token(), HOST)
    assert (await verifier.verify(token(), HOST)).uid == 'account-1'


@pytest.mark.asyncio
async def test_rejects_modified_payload_wrong_signature_and_algorithm_confusion():
    token, _, fetch, _, now = fixture()
    bearer = token()
    header, _, signature = bearer.split('.')
    modified = f'{header}.{b64(json.dumps({"sub": "account-2"}).encode())}.{signature}'
    other, _, _, _, _ = fixture()
    attacker = other(headers=jwt.get_unverified_header(bearer))
    hmac_token = jwt.encode({'sub': 'account-1', 'exp': now + 300}, b'not-a-signing-key-but-long-enough!',
                            algorithm='HS256', headers={'kid': jwt.get_unverified_header(bearer)['kid'],
                                                        'typ': 'flowly-live-voice+jwt'})
    for value in (modified, attacker, hmac_token):
        with pytest.raises(VoiceAccessError):
            await VoiceAccessVerifier(fetch_keys=fetch).verify(value, HOST)


@pytest.mark.asyncio
async def test_refreshes_public_keys_for_explicit_rotation():
    old, old_key, _, _, _ = fixture()
    new, new_key, _, _, _ = fixture()
    clock = [100.0]
    keys = [old_key]

    async def fetch():
        return {'keys': keys}

    verifier = VoiceAccessVerifier(fetch_keys=fetch, monotonic=lambda: clock[0])
    await verifier.verify(old(), HOST)
    keys = [new_key, old_key]
    clock[0] += 31
    await verifier.verify(new(), HOST)
    await verifier.verify(old(), HOST)
    keys = [new_key]
    clock[0] += 61
    with pytest.raises(VoiceAccessError):
        await verifier.verify(old(), HOST)
    await verifier.verify(new(), HOST)


@pytest.mark.asyncio
@pytest.mark.parametrize('response_kind', ['valid', 'oversized', 'redirect'])
async def test_discovery_uses_only_fixed_public_endpoint_and_bounded_body(monkeypatch, response_kind):
    _, jwk, _, _, _ = fixture()
    calls = []

    def respond(request):
        calls.append(request)
        if response_kind == 'oversized':
            return httpx.Response(200, content=b' ' * 32769)
        if response_kind == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://private.invalid/keys'})
        return httpx.Response(200, json={'keys': [jwk]})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(respond)))
    monkeypatch.setenv('FLOWLY_API_BASE', 'https://issuer.example')
    if response_kind == 'valid':
        assert await _fetch_keys() == {'keys': [jwk]}
    else:
        with pytest.raises((VoiceAccessError, httpx.HTTPStatusError)):
            await _fetch_keys()
    assert len(calls) == 1
    assert str(calls[0].url) == 'https://issuer.example/api/live-voice/keys'
    assert 'authorization' not in calls[0].headers
    assert not calls[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize('base', ['http://remote.example', 'https://user:password@issuer.example', 'https://issuer.example?redirect=other'])
async def test_rejects_unsafe_issuer_configuration_before_network(monkeypatch, base):
    monkeypatch.setenv('FLOWLY_API_BASE', base)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **_: pytest.fail('Network must not be reached'))
    with pytest.raises(VoiceAccessError):
        await _fetch_keys()
