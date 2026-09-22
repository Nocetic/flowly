"""Profile hops preserve verified identity without forwarding a login token."""
import asyncio

import pytest

from flowly.live_voice.authority import (
    HOST_OWNER,
    ProfileHopVerifier,
    RequestOwner,
    VoiceAuthorityError,
    current_request_owner,
    request_owner_scope,
    sign_profile_hop,
)


def test_scopes_restore_after_failures_and_do_not_convert_host_to_internal():
    assert current_request_owner() is None
    with request_owner_scope(HOST_OWNER):
        assert current_request_owner() == HOST_OWNER
        with pytest.raises(RuntimeError):
            with request_owner_scope(RequestOwner('account-a')):
                assert current_request_owner().uid == 'account-a'
                raise RuntimeError('failed turn')
        assert current_request_owner() == HOST_OWNER
    assert current_request_owner() is None


@pytest.mark.asyncio
async def test_concurrent_accounts_and_accepted_child_work_keep_their_identity():
    release = asyncio.Event()

    async def work(uid):
        with request_owner_scope(RequestOwner(uid)):
            async def accepted():
                await release.wait()
                return current_request_owner().uid
            child = asyncio.create_task(accepted())
        return child

    a, b = await asyncio.gather(work('account-a'), work('account-b'))
    release.set()
    assert await asyncio.gather(a, b) == ['account-a', 'account-b']
    assert current_request_owner() is None


@pytest.mark.parametrize('owner', [None, HOST_OWNER, RequestOwner('account-a')])
def test_profile_hop_preserves_internal_host_and_account_authority(owner):
    verifier = ProfileHopVerifier('runtime-1', now=lambda: 100)
    params = {'sessionKey': 'desktop:voice-work:task-1', 'message': 'İşi sürdür', 'attachments': []}
    proof = sign_profile_hop(verifier.key, 'runtime-1', 'rpc-1', 'chat.send', params, owner, now=100)
    assert verifier.verify('rpc-1', 'chat.send', params, proof) == owner
    assert verifier.key not in repr(proof)
    assert verifier.key not in repr(verifier)


@pytest.mark.parametrize('change', ['method', 'params', 'request', 'owner', 'runtime', 'mac'])
def test_profile_hop_cannot_be_rebound_or_forged(change):
    verifier = ProfileHopVerifier('runtime-1', now=lambda: 100)
    params = {'sessionKey': 'desktop:voice-work:task-1', 'message': 'Original'}
    proof = sign_profile_hop(verifier.key, 'runtime-1', 'rpc-1', 'chat.send', params, RequestOwner('account-a'), now=100)
    method, request = 'chat.send', 'rpc-1'
    if change == 'method':
        method = 'chat.abort'
    elif change == 'params':
        params['message'] = 'Changed'
    elif change == 'request':
        request = 'rpc-2'
    elif change == 'owner':
        proof['owner'] = {'kind': 'internal'}
    elif change == 'runtime':
        proof['instanceId'] = 'runtime-2'
    else:
        proof['mac'] = '0' * 64
    with pytest.raises(VoiceAuthorityError):
        verifier.verify(request, method, params, proof)


def test_replayed_expired_future_or_wrong_runtime_hops_are_rejected():
    clock = [100]
    verifier = ProfileHopVerifier('runtime-1', now=lambda: clock[0])
    proof = sign_profile_hop(verifier.key, 'runtime-1', 'rpc-1', 'chat.inflight', {}, None, now=100)
    verifier.verify('rpc-1', 'chat.inflight', {}, proof)
    with pytest.raises(VoiceAuthorityError):
        verifier.verify('rpc-1', 'chat.inflight', {}, proof)
    for now in (69, 106):
        other = sign_profile_hop(verifier.key, 'runtime-1', 'rpc-2', 'chat.inflight', {}, None, now=now)
        with pytest.raises(VoiceAuthorityError):
            verifier.verify('rpc-2', 'chat.inflight', {}, other)
    replacement = ProfileHopVerifier('runtime-2', key=verifier.key, now=lambda: 100)
    with pytest.raises(VoiceAuthorityError):
        replacement.verify('rpc-1', 'chat.inflight', {}, proof)


def test_valid_hops_are_not_evicted_to_make_room_for_replays():
    verifier = ProfileHopVerifier('runtime-1', now=lambda: 100, max_requests=2)
    for request in ('rpc-1', 'rpc-2'):
        proof = sign_profile_hop(verifier.key, 'runtime-1', request, 'chat.inflight', {}, None, now=100)
        verifier.verify(request, 'chat.inflight', {}, proof)
    proof = sign_profile_hop(verifier.key, 'runtime-1', 'rpc-3', 'chat.inflight', {}, None, now=100)
    with pytest.raises(VoiceAuthorityError) as error:
        verifier.verify('rpc-3', 'chat.inflight', {}, proof)
    assert error.value.code == 'VOICE_AUTH_UNAVAILABLE'


@pytest.mark.parametrize('uid', ['', 'a' * 129, 'account\x00a', True])
def test_bad_owner_claims_cannot_be_constructed(uid):
    with pytest.raises(VoiceAuthorityError):
        RequestOwner(uid)
