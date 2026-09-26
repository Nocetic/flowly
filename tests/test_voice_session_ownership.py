import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.session.manager import Session, SessionManager
from flowly.session.ownership import SessionAccessError

A = RequestOwner('account-a')
B = RequestOwner('account-b')
KEY = 'desktop:voice-work:task-1'


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    manager = SessionManager(tmp_path / 'workspace')
    with request_owner_scope(A):
        manager.reserve_voice_work(KEY)
        session = manager.get_or_create(KEY)
        session.add_message('assistant', 'Account A result', run_id='run-1')
        manager.save(session)
    return manager


def test_worker_owner_survives_cache_eviction_and_process_restart(sessions):
    with request_owner_scope(A):
        assert sessions.read(KEY).metadata['voiceOwner'] == {'kind': 'account', 'uid': A.uid}
        other = SessionManager(sessions.workspace)
        assert other.get_or_create(KEY).messages[0]['content'] == 'Account A result'
        assert other.list_sessions()[0]['key'] == KEY
    raw = sessions._get_session_path(KEY).read_text()
    assert 'voiceAccess' not in raw


@pytest.mark.parametrize('owner', [B, HOST_OWNER])
@pytest.mark.parametrize('operation', ['read', 'cached', 'history', 'archive', 'result', 'save', 'mutate', 'delete', 'flush'])
def test_wrong_owner_cannot_read_or_modify_a_cached_or_persisted_voice_task(sessions, owner, operation):
    cached = sessions._cache[KEY]
    before = {p.name: p.read_bytes() for p in sessions.sessions_dir.glob('*.jsonl')}
    operations = {
        'read': lambda: sessions.read(KEY),
        'cached': lambda: sessions.get_or_create(KEY),
        'history': lambda: sessions.get_full_messages(KEY),
        'archive': lambda: sessions.get_archive_snapshot(KEY),
        'result': lambda: sessions.read_run_result(sessions.sessions_dir.parent, KEY, 'run-1'),
        'save': lambda: sessions.save(cached),
        'mutate': lambda: sessions.mutate(KEY, lambda session: session.metadata.update(title='Changed')),
        'delete': lambda: sessions.delete(KEY),
        'flush': lambda: sessions.flush_full(cached, required=True),
    }
    with request_owner_scope(owner):
        with pytest.raises(SessionAccessError) as error:
            operations[operation]()
        assert error.value.code == 'NOT_FOUND'
        assert sessions.list_sessions() == []
    assert {p.name: p.read_bytes() for p in sessions.sessions_dir.glob('*.jsonl')} == before
    assert sessions._cache[KEY] is cached


def test_host_legacy_voice_and_regular_shared_context_keep_separate_visibility(sessions):
    for key in ('desktop:voice:legacy', 'desktop:ordinary'):
        session = Session(key=key)
        session.add_message('user', key)
        sessions.save(session)
    with request_owner_scope(A):
        assert {row['key'] for row in sessions.list_sessions()} == {KEY, 'desktop:ordinary'}
        with pytest.raises(SessionAccessError):
            sessions.get_or_create('desktop:voice:legacy')
    with request_owner_scope(HOST_OWNER):
        assert {row['key'] for row in sessions.list_sessions()} == {'desktop:voice:legacy', 'desktop:ordinary'}
    assert len(sessions.list_sessions()) == 3


@pytest.mark.parametrize('owner_value', [None, {}, {'kind': 'account'}, {'kind': 'internal'}])
def test_corrupt_owner_cannot_fall_back_to_host_access(sessions, owner_value):
    sessions.mutate(KEY, lambda session: session.metadata.update(voiceOwner=owner_value))
    for owner in (A, B, HOST_OWNER):
        with request_owner_scope(owner):
            with pytest.raises(SessionAccessError):
                sessions.read(KEY)
            assert sessions.list_sessions() == []


def test_owner_cannot_be_changed_removed_or_recovered_from_a_stale_cache(sessions):
    with request_owner_scope(A):
        before = sessions._get_session_path(KEY).read_bytes()
        for update in (lambda m: m.update(voiceOwner={'kind': 'account', 'uid': B.uid}),
                       lambda m: m.pop('voiceOwner')):
            with pytest.raises(SessionAccessError):
                sessions.mutate(KEY, lambda session: update(session.metadata))
        assert sessions._get_session_path(KEY).read_bytes() == before
    # A trusted repair may change disk metadata. A previously cached object
    # must never override the authoritative owner on the next external read.
    fresh = SessionManager(sessions.workspace)
    fresh.mutate(KEY, lambda session: session.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    with request_owner_scope(A):
        with pytest.raises(SessionAccessError):
            sessions.get_or_create(KEY)


def test_alias_and_orphan_archive_cannot_bypass_the_canonical_owner(sessions):
    with request_owner_scope(B):
        with pytest.raises(SessionAccessError):
            sessions.get_full_messages(KEY.replace(':', '_'))
    sessions._get_session_path(KEY).unlink()
    for owner in (A, B, HOST_OWNER):
        with request_owner_scope(owner):
            with pytest.raises(SessionAccessError):
                sessions.get_full_messages(KEY)
            with pytest.raises(SessionAccessError):
                sessions.get_or_create(KEY)


def test_missing_or_corrupt_canonical_metadata_cannot_expose_archive(sessions):
    sessions._get_session_path(KEY).write_text(json.dumps({'_type': 'metadata', 'metadata': None}) + '\n')
    with request_owner_scope(HOST_OWNER):
        with pytest.raises(SessionAccessError):
            sessions.get_full_messages(KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
@pytest.mark.parametrize('owner', [B, HOST_OWNER])
async def test_general_chat_routes_reject_another_owner_before_reads_or_dispatch(sessions, monkeypatch, transport, owner):
    import flowly.profile as profiles
    from flowly.bus.queue import MessageBus
    from flowly.channels import feature_rpc
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig
    from flowly.gateway.server import GatewayServer
    from flowly.live_voice.access import VoiceAccessVerifier
    from tests.test_chat_command_transport import Socket
    from tests.test_voice_access import HOST, fixture

    token, _, keys, _, _ = fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    server = GatewayServer(sessions=sessions, on_chat_message=AsyncMock())
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    channel.bus.publish_inbound = AsyncMock()
    socket = Socket()
    before = sessions._get_session_path(KEY).read_bytes()
    for method in ('sessions.read', 'chat.history', 'chat.inflight', 'chat.send', 'chat.command',
                   'chat.outputs.list', 'chat.requests', 'chat.abort', 'sessions.model.set', 'sessions.delete'):
        params = {'sessionKey': KEY, 'key': KEY, 'message': 'Unauthorized turn', 'idempotencyKey': 'unwanted-run', 'runId': 'run-1'}
        if owner.uid:
            params['voiceAccess'] = token({'sub': owner.uid})
        frame = {'id': method, 'method': method, 'params': params}
        if transport == 'gateway':
            await server._handle_ws_rpc(socket, 'client', frame)
        else:
            from tests.relay_voice_helpers import relay_rpc

            await relay_rpc(channel, socket, {**frame, 'sessionId': 'relay-1'}, uid=owner.uid)
        assert socket.sent[-1]['error']['code'] == 'NOT_FOUND', method
    assert 'Account A result' not in repr(socket.sent)
    assert sessions._get_session_path(KEY).read_bytes() == before
    server.on_chat_message.assert_not_awaited()
    channel.bus.publish_inbound.assert_not_awaited()
    assert server.chat_commands.lookup(KEY, 'unwanted-run') is None
    assert channel.chat_commands.lookup(KEY, 'unwanted-run') is None


@pytest.mark.asyncio
async def test_feature_lists_and_raw_reader_use_the_verified_owner(sessions):
    from flowly.channels import feature_rpc

    ordinary = Session(key='desktop:ordinary')
    sessions.save(ordinary)
    for owner, expected in ((A, {KEY, ordinary.key}), (B, {ordinary.key}), (HOST_OWNER, {ordinary.key})):
        with request_owner_scope(owner):
            listed, _ = await feature_rpc.dispatch('sessions.list', {})
            assert {row['key'] for row in listed['sessions']} == expected
    with request_owner_scope(A):
        result, _ = await feature_rpc.dispatch('sessions.read', {'key': KEY})
        assert any(row.get('content') == 'Account A result' for row in result['messages'])
        for key in ('../outside', KEY + '.full'):
            with pytest.raises(feature_rpc.FeatureRpcError):
                await feature_rpc.dispatch('sessions.read', {'key': key})


def test_history_read_and_owner_replacement_are_one_serialized_operation(sessions, monkeypatch):
    entered, release, changed = threading.Event(), threading.Event(), threading.Event()
    read_rows = sessions._read_full_rows

    def paused_read(key):
        entered.set()
        assert release.wait(3)
        return read_rows(key)

    monkeypatch.setattr(sessions, '_read_full_rows', paused_read)

    def reader():
        with request_owner_scope(A):
            return sessions.get_full_messages(KEY)

    def replace():
        other = SessionManager(sessions.workspace)
        with request_owner_scope(A):
            other.delete(KEY)
        with request_owner_scope(B):
            other.reserve_voice_work(KEY)
            new = other.get_or_create(KEY)
            new.add_message('assistant', 'Account B result')
            other.save(new)
        changed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(reader)
        assert entered.wait(2)
        replacing = pool.submit(replace)
        try:
            assert not changed.wait(0.05)
        finally:
            release.set()
        assert reading.result(timeout=3)[0]['content'] == 'Account A result'
        replacing.result(timeout=3)
    assert changed.is_set()
