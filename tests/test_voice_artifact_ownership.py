"""Artifact IDs, output links and search pages retain original ownership."""
import json
import multiprocessing
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from flowly.artifacts.store import ArtifactStore
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.session.manager import Session, SessionManager
from flowly.session.ownership import SessionAccessError

A, B = RequestOwner('artifact-account-a'), RequestOwner('artifact-account-b')
KEY = 'desktop:voice-work:artifact-a'
OTHER = 'desktop:voice-work:artifact-b'
SHARED = 'desktop:artifact-shared'


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    sessions = SessionManager(tmp_path / 'workspace')
    sessions.save(Session(key=SHARED))
    store = ArtifactStore(sessions.sessions_dir.parent / 'artifacts.sqlite')
    shared = store.create('markdown', 'Shared report', 'report shared', session_key=SHARED)
    with request_owner_scope(A):
        sessions.reserve_voice_work(KEY)
        private = store.create('markdown', 'Private report', 'report secret old', session_key=KEY)
        private = store.update(private['id'], content='report secret current')
        detached = store.create('markdown', 'Detached report', 'report private without session')
    with request_owner_scope(B):
        sessions.reserve_voice_work(OTHER)
        other = store.create('markdown', 'Other report', 'report other account', session_key=OTHER)
    yield sessions, store, shared, private, detached, other
    store.close()


@pytest.mark.parametrize('owner', [B, HOST_OWNER])
def test_artifact_crud_versions_and_output_membership_deny_other_owner(data, owner):
    _, store, _, private, detached, _ = data
    with request_owner_scope(owner):
        for item in (private, detached):
            assert store.get(item['id']) is None
            assert store.update(item['id'], content='overwritten') is None
            assert store.pin(item['id']) is None
            assert store.get_versions(item['id']) == []
            assert store.delete(item['id']) is False
            assert store.has_session_output(item['id'], KEY) is False
            assert store.attach_session_output(item['id'], SHARED) is False
        assert store.session_summaries(KEY, 0, 10) == []
    with request_owner_scope(A):
        assert store.get(private['id'])['content'] == 'report secret current'
        assert [row['content'] for row in store.get_versions(private['id'])] == ['report secret old']


@pytest.mark.parametrize('search', [None, 'report'])
def test_artifact_pages_filter_ownership_before_offset_and_limit(data, search):
    _, store, shared, private, detached, other = data
    for owner, expected in ((A, {shared['id'], private['id'], detached['id']}),
                            (B, {shared['id'], other['id']}), (HOST_OWNER, {shared['id']})):
        with request_owner_scope(owner):
            whole = store.list(search=search, limit=100)
            assert {row['id'] for row in whole} == expected
            pages = [store.list(search=search, offset=i, limit=1)[0]['id'] for i in range(len(expected))]
            assert pages == [row['id'] for row in whole]
            assert store.list(search=search, offset=len(expected), limit=1) == []


@pytest.mark.parametrize('change', ['owner', 'delete', 'corrupt'])
def test_artifact_origin_binding_survives_restart_and_canonical_change(data, change):
    sessions, store, _, private, _, _ = data
    if change == 'owner':
        sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    elif change == 'delete':
        sessions.delete(KEY)
    else:
        sessions._get_session_path(KEY).write_text('{broken')
    reopened = ArtifactStore(store._db_path)
    try:
        for owner in (A, B, HOST_OWNER):
            with request_owner_scope(owner):
                assert reopened.get(private['id']) is None
                assert reopened.get_versions(private['id']) == []
                assert reopened.update(private['id'], content='adopted') is None
                assert reopened.delete(private['id']) is False
                assert private['id'] not in {row['id'] for row in reopened.list()}
    finally:
        reopened.close()


def test_creation_cannot_claim_another_session_or_forge_owner_in_metadata(data):
    _, store, _, _, _, _ = data
    with request_owner_scope(B), pytest.raises(SessionAccessError):
        store.create('markdown', 'Forged', 'content', session_key=KEY,
                     metadata={'voiceOwner': {'kind': 'account', 'uid': B.uid}})
    with request_owner_scope(A):
        forged = store.create('markdown', 'My content', 'private',
                              metadata={'sessionOwner': None, 'voiceOwner': {'kind': 'host'}})
    with request_owner_scope(B):
        assert store.get(forged['id']) is None


def test_output_link_checks_both_resources_and_keeps_its_original_owner(data):
    sessions, store, shared, private, _, _ = data
    with request_owner_scope(A):
        assert store.attach_session_output(shared['id'], KEY)
        assert store.has_session_output(shared['id'], KEY)
        assert store.get(shared['id']) is not None
        assert not store.attach_session_output(private['id'], OTHER)
        assert store.update(private['id'], content='not committed', output_session_key=OTHER) is None
        assert store.get(private['id'])['content'] == 'report secret current'
    sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    with request_owner_scope(B):
        assert store.get(shared['id']) is not None
        assert not store.has_session_output(shared['id'], KEY)
        assert not store.attach_session_output(shared['id'], KEY)
        assert store.session_summaries(KEY, 0, 10) == []


def test_account_owner_is_omitted_from_artifact_and_version_dtos(data):
    _, store, _, private, detached, _ = data
    with request_owner_scope(A):
        assert A.uid not in json.dumps(store.get(private['id']))
        assert A.uid not in json.dumps(store.get_versions(private['id']))
        assert A.uid not in json.dumps(store.get(detached['id']))


@pytest.mark.parametrize('raw', [None, 'null', '{}', '{"kind":"internal"}'])
def test_missing_or_invalid_persisted_owner_cannot_adopt_an_account(data, raw):
    _, store, _, private, _, _ = data
    store._conn.execute('UPDATE artifacts SET session_owner_json = ? WHERE id = ?', (raw, private['id']))
    store._conn.commit()
    with request_owner_scope(A):
        assert store.get(private['id']) is None


@pytest.mark.parametrize('search', [None, 'report', '"report"'])
def test_internal_rows_do_not_starve_or_repeat_visible_pages(data, search):
    _, store, _, private, _, _ = data
    with request_owner_scope(A):
        visible = store.create('markdown', 'Visible report', 'report visible', session_key=KEY)
        for i in range(105):
            store.create('markdown', 'Internal report', 'report internal', session_key=KEY,
                         metadata={'hidden': True}, tags=[str(i)])
        expected = {private['id'], visible['id']}
        full = store.list(search=search, session_key=KEY, limit=100, include_internal=False)
        assert {item['id'] for item in full} == expected
        pages = [store.list(search=search, session_key=KEY, limit=1, offset=i,
                            include_internal=False)[0]['id'] for i in range(2)]
        assert pages == [row['id'] for row in full]
        summary = store.session_summaries(KEY, 0, 10, include_internal=False)
        assert {row['id'] for row in summary} == expected
        assert all('content' not in row for row in summary)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['update', 'promote', 'export'])
async def test_failed_tool_target_never_changes_content_writes_file_or_announces_success(data, monkeypatch, tmp_path, action):
    from flowly.agent.tools import artifact as artifact_module

    _, store, _, private, _, _ = data
    notify = AsyncMock()
    tool = artifact_module.ArtifactTool(store, on_change=notify)
    monkeypatch.setattr(artifact_module, '_export_path_allowed', lambda _: True)
    target = tmp_path / 'unexpected.md'
    with request_owner_scope(A):
        result = json.loads(await tool.execute(action, artifact_id=private['id'], session_key=OTHER,
                                               content='not committed', title='not changed', path=str(target)))
        assert 'error' in result
        assert store.get(private['id'])['content'] == 'report secret current'
        assert len(store.get_versions(private['id'])) == 1
    assert not target.exists()
    notify.assert_not_awaited()


def test_default_store_follows_profile_home_and_keeps_pinned_origin(data, tmp_path, monkeypatch):
    from flowly.artifacts.store import _CACHE, get_store

    _, store, _, private, _, _ = data
    new_home = tmp_path / 'another-profile'
    monkeypatch.setenv('FLOWLY_HOME', str(new_home))
    other = get_store()
    try:
        assert other._db_path.parent == new_home
        assert other.list() == []
        with request_owner_scope(A):
            assert store.get(private['id']) is not None
    finally:
        other.close()
        _CACHE.pop(str(new_home), None)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
async def test_public_artifact_rpc_filters_ids_versions_lists_and_mutations(data, monkeypatch, transport):
    import flowly.profile as profiles
    from flowly.bus.queue import MessageBus
    from flowly.channels import feature_rpc
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig
    from flowly.gateway.server import GatewayServer
    from flowly.live_voice.access import VoiceAccessVerifier
    from tests.test_chat_command_transport import Socket
    from tests.test_voice_access import HOST
    from tests.test_voice_access import fixture as access_fixture

    sessions, store, shared, private, _, other = data
    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    monkeypatch.setattr(feature_rpc, '_artifact_store', lambda: store)
    server = GatewayServer(sessions=sessions, artifact_store=store)
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    socket = Socket()

    async def call(owner, method, **params):
        access = {'voiceAccess': token({'sub': owner.uid})} if owner.uid else {}
        frame = {'id': 'artifact', 'method': method, 'sessionId': 'relay', 'params': {**params, **access}}
        if transport == 'gateway':
            await server._handle_ws_rpc(socket, 'client', frame)
        else:
            from tests.relay_voice_helpers import relay_rpc

            await relay_rpc(channel, socket, frame, uid=owner.uid)
        return socket.sent[-1]

    try:
        for owner, expected in ((B, {shared['id'], other['id']}), (HOST_OWNER, {shared['id']})):
            assert {row['id'] for row in (await call(owner, 'artifacts.list'))['result']['artifacts']} == expected
            assert (await call(owner, 'artifacts.get', id=private['id']))['error']['code'] == 'NOT_FOUND'
            assert (await call(owner, 'artifacts.versions', id=private['id']))['result']['versions'] == []
            for method in ('artifacts.update', 'artifacts.pin', 'artifacts.delete'):
                result = await call(owner, method, id=private['id'], content='not allowed')
                assert 'error' in result or result['result']['ok'] is False
        own = await call(A, 'artifacts.get', id=private['id'])
        assert own['result']['artifact']['content'] == 'report secret current'
    finally:
        server.chat_commands.close()
        channel.chat_commands.close()


@pytest.mark.asyncio
async def test_http_artifact_routes_scope_every_request_and_disable_cache(data, monkeypatch):
    import aiohttp

    import flowly.profile as profiles
    from flowly.channels import feature_rpc
    from flowly.gateway.server import GatewayServer
    from flowly.live_voice.access import VoiceAccessVerifier
    from tests.test_voice_access import HOST
    from tests.test_voice_access import fixture as access_fixture

    sessions, store, shared, private, _, other = data
    token, _, keys, _, _ = access_fixture()
    monkeypatch.setattr(feature_rpc, '_voice_access_verifier', VoiceAccessVerifier(fetch_keys=keys))
    monkeypatch.setattr(profiles, 'get_or_create_profile_host_id', lambda: HOST)
    server = GatewayServer(host='127.0.0.1', port=0, sessions=sessions, artifact_store=store,
                           auth_token='g' * 48, require_loopback_auth=True, advertise_control=False)
    await server.start()
    try:
        async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + 'g' * 48}) as client:
            base = f'http://127.0.0.1:{server.port}/api/artifacts'
            for owner, expected in ((B, {shared['id'], other['id']}), (HOST_OWNER, {shared['id']})):
                headers = {'X-Flowly-Voice-Access': token({'sub': owner.uid})} if owner.uid else {}
                async with client.get(base, headers=headers) as response:
                    assert response.status == 200
                    assert response.headers['Cache-Control'] == 'no-store'
                    assert {row['id'] for row in (await response.json())['artifacts']} == expected
                async with client.get(base + '/' + private['id'], headers=headers) as response:
                    assert response.status == 404
                    assert response.headers['Cache-Control'] == 'no-store'
                async with client.get(base + '/' + private['id'] + '/versions', headers=headers) as response:
                    assert (await response.json())['versions'] == []
            async with client.get(base + '/' + private['id'], headers={'X-Flowly-Voice-Access': token({'sub': A.uid})}) as response:
                assert (await response.json())['artifact']['content'] == 'report secret current'
    finally:
        await server.stop()
        server.chat_commands.close()


def test_work_output_reader_checks_origin_and_link_together(data, monkeypatch, tmp_path):
    from flowly.live_voice import outputs
    from flowly.live_voice.sessions import VoiceError

    sessions, store, shared, private, _, _ = data
    monkeypatch.setattr(outputs, 'validate_chat_target', lambda _: None)
    service = outputs.WorkOutputs(tmp_path / 'workspace', sessions, lambda: store)
    params = {'sessionKey': KEY, 'expectedBotId': 'profile-a'}
    with request_owner_scope(A):
        store.attach_session_output(shared['id'], KEY)
        assert {row['id'] for row in service.list(params)['artifacts']} == {private['id'], shared['id']}
        assert service.read({**params, 'source': 'artifact', 'id': shared['id']})['id'] == shared['id']
    sessions.mutate(KEY, lambda row: row.metadata.update(voiceOwner={'kind': 'account', 'uid': B.uid}))
    with request_owner_scope(B):
        assert service.list(params)['artifacts'] == []
        for item in (private, shared):
            with pytest.raises(VoiceError):
                service.read({**params, 'source': 'artifact', 'id': item['id']})


def test_additive_migration_preserves_shared_data_without_adopting_legacy_voice_links(data, tmp_path):
    from flowly.artifacts.store import _SCHEMA

    sessions, _, _, _, _, _ = data
    path = sessions.sessions_dir.parent / 'legacy-artifacts.sqlite'
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    connection.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    connection.execute('INSERT INTO artifacts (id, title, content, session_key, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 1)',
                       ('legacy', 'Report', 'legacy shared content', SHARED))
    connection.execute('INSERT INTO artifact_versions (id, artifact_id, version, content, created_at) VALUES (?, ?, 1, ?, 1)',
                       ('version', 'legacy', 'earlier shared content'))
    connection.execute('INSERT INTO artifact_session_outputs (artifact_id, session_key) VALUES (?, ?)', ('legacy', KEY))
    connection.commit()
    connection.close()
    store = ArtifactStore(path)
    try:
        with request_owner_scope(A):
            assert store.get('legacy')['content'] == 'legacy shared content'
            assert store.get_versions('legacy')[0]['content'] == 'earlier shared content'
            assert store.get_session_output('legacy', KEY) is None
            assert not store.attach_session_output('legacy', KEY)
        assert store._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == '2'
    finally:
        store.close()


def _artifact_worker(root, artifact_id, target_key, mode, start, results):
    from flowly.live_voice import outputs

    os.environ['FLOWLY_HOME'] = root
    store = ArtifactStore(Path(root) / 'artifacts.sqlite')
    sessions = SessionManager(Path(root) / 'workspace')
    outputs.validate_chat_target = lambda _: None
    service = outputs.WorkOutputs(Path(root) / 'workspace', sessions, lambda: store)
    try:
        if not start.wait(5):
            raise RuntimeError('Start signal missing')
        with request_owner_scope(A):
            for i in range(15):
                if mode == 'read':
                    rows = service.list({'sessionKey': target_key, 'expectedBotId': 'profile-a'})['artifacts']
                    assert all(row['id'] == artifact_id for row in rows)
                else:
                    assert store.update(artifact_id, content=f'{mode}-{i}', output_session_key=target_key) is not None
        results.put(None)
    except BaseException as error:
        results.put(type(error).__name__ + ': ' + str(error))
    finally:
        store.close()


def test_cross_process_updates_and_output_reads_keep_versions_and_lock_order(data):
    sessions, store, _, private, _, _ = data
    target = 'desktop:voice-work:another-artifact-a'
    with request_owner_scope(A):
        sessions.reserve_voice_work(target)
    context = multiprocessing.get_context('spawn')
    start, results = context.Event(), context.Queue()
    processes = [context.Process(target=_artifact_worker, args=(str(store._db_path.parent), private['id'], target,
                                                                mode, start, results))
                 for mode in ('writer-1', 'writer-2', 'read')]
    try:
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(8)
            assert not process.is_alive(), 'Canonical/output lock order deadlocked'
            assert process.exitcode == 0
        assert [results.get(timeout=2) for _ in processes] == [None, None, None]
        with request_owner_scope(A):
            assert store.get(private['id'])['version'] == private['version'] + 30
            assert sorted(row['version'] for row in store.get_versions(private['id'])) == list(range(1, 32))
            assert store.has_session_output(private['id'], target)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(2)
        results.close()
