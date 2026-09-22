"""A profile event retains its producer's authority across a real JSON boundary."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
from flowly.gateway.server import GatewayServer
from flowly.live_voice.authority import (
    HOST_OWNER,
    RequestOwner,
    VoiceAuthorityError,
    current_request_owner,
    request_owner_scope,
    sign_profile_hop,
)
from flowly.live_voice.event_transport import ProfileEventVerifier, sign_profile_event
from flowly.live_voice.events import EventAccess, current_event_access
from flowly.profile_host import ProfileHost, _Runtime
from flowly.profile_host_contract import ProfileHostError
from flowly.session.control_access import SessionControlScope
from tests import test_voice_event_recipients as recipients
from tests.test_voice_event_recipients import KEY, SHARED, A, B, Socket

setup = recipients.setup

PARENT_KEY = 'a' * 64
INSTANCE = 'runtime-events-1'
CAPABILITIES = frozenset({'voice-owner-hop-v1', 'voice-owner-events-v1'})


def source(setup):
    _, sessions, _, _, _, _, _ = setup
    with request_owner_scope(A):
        access = EventAccess(scopes=(SessionControlScope.capture(KEY),))
    payload = {'type': 'event', 'event': 'chat', 'data': {'sessionKey': KEY, 'runId': 'run-a', 'state': 'final', 'message': 'private'}}
    frame = sign_profile_event(PARENT_KEY, INSTANCE, payload, access, sessions_dir=sessions.sessions_dir)
    return json.loads(json.dumps(frame)), sessions


def test_roundtrip_preserves_original_owner_without_exporting_local_paths(setup):
    frame, sessions = source(setup)
    event = ProfileEventVerifier(INSTANCE, key=PARENT_KEY).verify(frame, sessions_dir=sessions.sessions_dir)
    assert event.access.permits(A)
    assert not event.access.permits(B)
    assert not event.access.permits(HOST_OWNER)
    assert str(sessions.sessions_dir) not in json.dumps(frame)
    assert PARENT_KEY not in json.dumps(frame)
    assert 'voiceEventAuthority' not in json.dumps(event)
    assert A.uid not in json.dumps(event)


@pytest.mark.parametrize('change', ['payload', 'scope', 'instance', 'missing', 'key'])
def test_modified_event_or_authority_is_rejected_before_publication(setup, change):
    frame, sessions = source(setup)
    key, instance = PARENT_KEY, INSTANCE
    if change == 'payload':
        frame['data']['message'] = 'changed'
    elif change == 'scope':
        frame['voiceEventAuthority']['access']['scopes'][0]['owner']['uid'] = B.uid
    elif change == 'instance':
        instance = 'other-runtime'
    elif change == 'missing':
        frame.pop('voiceEventAuthority')
    else:
        key = 'b' * 64
    with pytest.raises(VoiceAuthorityError):
        ProfileEventVerifier(instance, key=key).verify(frame, sessions_dir=sessions.sessions_dir)


def test_replay_does_not_deliver_a_second_terminal(setup):
    frame, sessions = source(setup)
    verifier = ProfileEventVerifier(INSTANCE, key=PARENT_KEY)
    verifier.verify(frame, sessions_dir=sessions.sessions_dir)
    with pytest.raises(VoiceAuthorityError):
        verifier.verify(frame, sessions_dir=sessions.sessions_dir)


def test_expired_event_proof_cannot_be_replayed_on_a_fresh_verifier(setup):
    frame, sessions = source(setup)
    verifier = ProfileEventVerifier(INSTANCE, key=PARENT_KEY, now=lambda: time.time() + 60)
    with pytest.raises(VoiceAuthorityError):
        verifier.verify(frame, sessions_dir=sessions.sessions_dir)


def test_clock_rollback_does_not_make_an_event_valid_after_replay_cache_expiry(setup):
    frame, sessions = source(setup)
    wall, monotonic = [time.time()], [100.0]
    verifier = ProfileEventVerifier(INSTANCE, key=PARENT_KEY, now=lambda: wall[0], monotonic=lambda: monotonic[0])
    verifier.verify(frame, sessions_dir=sessions.sessions_dir)
    monotonic[0] += 60
    with pytest.raises(VoiceAuthorityError):
        verifier.verify(frame, sessions_dir=sessions.sessions_dir)


@pytest.mark.asyncio
async def test_retired_runtime_reader_cannot_publish_or_evict_its_replacement(monkeypatch):
    class Frames:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return SimpleNamespace(type=1, data=json.dumps({'type': 'event', 'event': 'chat', 'data': {}}))

    host = ProfileHost.__new__(ProfileHost)
    runtime = _Runtime('worker', None, SimpleNamespace(), Frames(), INSTANCE)
    replacement = object()
    host._runtimes = {'worker': replacement}
    host._closed = False
    host._capacity_changed = asyncio.Event()
    handle = AsyncMock()
    monkeypatch.setattr(host, '_handle_runtime_event', handle)
    await asyncio.wait_for(host._read_runtime(runtime), 1)
    handle.assert_not_awaited()
    assert host._runtimes['worker'] is replacement


def test_source_cannot_project_another_profiles_scope_into_its_own_root(setup, tmp_path):
    _, sessions = source(setup)
    alien = SessionControlScope.bind(KEY, {'kind': 'account', 'uid': A.uid}, sessions_dir=tmp_path / 'alien')
    with pytest.raises(VoiceAuthorityError):
        sign_profile_event(PARENT_KEY, INSTANCE, {'type': 'event', 'event': 'chat', 'data': {'sessionKey': KEY}},
                           EventAccess(scopes=(alien,)), sessions_dir=sessions.sessions_dir)


@pytest.mark.asyncio
async def test_managed_gateway_signs_only_the_secret_proven_parent_channel(setup):
    _, sessions, store, _, _, _, _ = setup
    server = GatewayServer(sessions=sessions, artifact_store=store, auth_token='g' * 48,
                           require_loopback_auth=True, advertise_control=False)
    server.set_managed_runtime_control(INSTANCE, lambda: None)
    socket = Socket()
    proof = sign_profile_hop(server.voice_parent_key, INSTANCE, 'parent', 'health', {}, None)
    try:
        await server._handle_ws_rpc(socket, 'parent', {'id': 'parent', 'method': 'health', 'params': {}, 'voiceAuthority': proof})
        socket.sent.clear()
        await server._ws_send(socket, {'type': 'event', 'event': 'goal.updated', 'data': {'sessionKey': KEY, 'goal': 'private'}})
        event = ProfileEventVerifier(INSTANCE, key=server.voice_parent_key).verify(socket.sent[0], sessions_dir=sessions.sessions_dir)
        assert event.access.permits(A)
        assert not event.access.permits(B)
        stranger = Socket()
        await server._ws_send(stranger, {'type': 'event', 'event': 'goal.updated', 'data': {'sessionKey': KEY, 'goal': 'private'}})
        assert stranger.sent == []
    finally:
        server.chat_commands.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [False, True])
async def test_host_checks_the_original_scope_before_touching_terminal_state(setup, monkeypatch, replacement):
    frame, sessions = source(setup)
    host = ProfileHost.__new__(ProfileHost)
    runtime = _Runtime('worker', None, SimpleNamespace(), SimpleNamespace(), INSTANCE,
                       capabilities=CAPABILITIES, voice_parent_key=PARENT_KEY)
    seen = []

    async def receive(*_args, **_kwargs):
        seen.append((current_request_owner(), current_event_access()))

    monkeypatch.setattr(host, '_handle_profile_event', receive)
    monkeypatch.setattr(profiles, 'describe_profile', lambda _: SimpleNamespace(path=sessions.sessions_dir.parent))
    if replacement:
        sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    with request_owner_scope(RequestOwner('reader-ambient')):
        await host._handle_runtime_event(runtime, frame)
        assert current_request_owner() == RequestOwner('reader-ambient')
    if replacement:
        assert seen == []
    else:
        assert seen[0][0] == A
        assert seen[0][1].permits(A)


@pytest.mark.asyncio
async def test_missing_or_corrupt_proof_cannot_downgrade_an_updated_runtime(setup, monkeypatch):
    frame, sessions = source(setup)
    host = ProfileHost.__new__(ProfileHost)
    runtime = _Runtime('worker', None, SimpleNamespace(), SimpleNamespace(), INSTANCE,
                       capabilities=CAPABILITIES, voice_parent_key=PARENT_KEY)
    receive = AsyncMock()
    monkeypatch.setattr(host, '_handle_profile_event', receive)
    monkeypatch.setattr(profiles, 'describe_profile', lambda _: SimpleNamespace(path=sessions.sessions_dir.parent))
    missing = {k: v for k, v in frame.items() if k != 'voiceEventAuthority'}
    corrupt = copy.deepcopy(frame)
    corrupt['data']['sessionKey'] = SHARED
    await host._handle_runtime_event(runtime, missing)
    await host._handle_runtime_event(runtime, corrupt)
    receive.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_close_survives_deletion_through_the_managed_transport(setup, monkeypatch):
    _, sessions, _, _, _, _, _ = setup
    access = EventAccess(scopes=(SessionControlScope.capture(KEY),), canonical=False)
    frame = sign_profile_event(PARENT_KEY, INSTANCE, {'type': 'event', 'event': 'exec.approval.closed',
                               'data': {'sessionKey': KEY, 'id': 'closed', 'decision': 'cancelled'}},
                               access, sessions_dir=sessions.sessions_dir)
    sessions.delete(KEY)
    host = ProfileHost.__new__(ProfileHost)
    runtime = _Runtime('worker', None, SimpleNamespace(), SimpleNamespace(), INSTANCE,
                       capabilities=CAPABILITIES, voice_parent_key=PARENT_KEY)
    seen = []

    async def receive(*_args, **_kwargs):
        seen.append(current_event_access())

    monkeypatch.setattr(host, '_handle_profile_event', receive)
    monkeypatch.setattr(profiles, 'describe_profile', lambda _: SimpleNamespace(path=sessions.sessions_dir.parent))
    await host._handle_runtime_event(runtime, json.loads(json.dumps(frame)))
    assert len(seen) == 1
    assert seen[0].permits(A)
    assert not seen[0].permits(B)


@pytest.mark.asyncio
@pytest.mark.parametrize('owner,method', [(A, 'chat.send'), (A, 'runtime.voice.reserve'),
                                         (HOST_OWNER, 'runtime.voice.reserve'), (None, 'runtime.voice.reserve')])
async def test_voice_work_requires_runtime_event_authority_capability(owner, method):
    host = ProfileHost.__new__(ProfileHost)
    host._capacity_changed = asyncio.Event()
    socket = SimpleNamespace(closed=False, send_json=AsyncMock())
    runtime = _Runtime('worker', None, SimpleNamespace(), socket, INSTANCE,
                       capabilities=frozenset({'voice-owner-hop-v1'}), voice_parent_key=PARENT_KEY)
    with request_owner_scope(owner):
        with pytest.raises(ProfileHostError) as error:
            await host._rpc(runtime, method, {'sessionKey': KEY}, 0.01)
    assert error.value.code == 'VOICE_AUTH_UNAVAILABLE'
    socket.send_json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [False, True])
async def test_primary_broker_checks_source_before_caching_or_publishing(setup, monkeypatch, replacement):
    frame, sessions = source(setup)
    event = ProfileEventVerifier(INSTANCE, key=PARENT_KEY).verify(frame, sessions_dir=sessions.sessions_dir)
    host = ProfileHost.__new__(ProfileHost)
    host._default_event_leases = {'subscriber'}
    seen = []

    async def receive(*_args, **_kwargs):
        seen.append(current_request_owner())

    monkeypatch.setattr(host, '_handle_profile_event', receive)
    if replacement:
        sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    with request_owner_scope(B):
        await host.handle_primary_frame(event)
        assert current_request_owner() == B
    assert seen == ([] if replacement else [A])


@pytest.mark.asyncio
async def test_primary_callback_can_make_a_nested_rpc_without_deadlocking(setup):
    server, _, _, _, _, _, _ = setup
    sent = []

    class Internal:
        closed = False

        async def send_json(self, frame):
            sent.append(frame['type'])
            if frame['type'] == 'event':
                await server._ws_rpc_reply(self, 'nested', {'ok': True})

    socket = server._profile_host_socket = Internal()
    await asyncio.wait_for(server._ws_send(socket, {'type': 'event', 'event': 'agent.clarify.requested',
                                                   'data': {'sessionKey': KEY, 'question': 'Which report?'}}), 0.1)
    assert sent == ['event', 'rpc']


@pytest.mark.asyncio
async def test_rejected_primary_terminal_cannot_retire_a_different_active_run(setup):
    server, sessions, _, _, _, _, _ = setup
    frame, _ = source(setup)
    event = ProfileEventVerifier(INSTANCE, key=PARENT_KEY).verify(frame, sessions_dir=sessions.sessions_dir)
    server._profile_host_active_runs.add('run-a')
    sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    await server._handle_profile_host_internal_frame(event)
    assert 'run-a' in server._profile_host_active_runs
    assert 'run-a' not in server._profile_host_terminal_runs
