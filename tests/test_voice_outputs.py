from __future__ import annotations

import base64
import json
import os
import sqlite3

import pytest

import flowly.profile as profiles
from flowly.artifacts.store import ArtifactStore
from flowly.live_voice.outputs import WorkOutputs
from flowly.live_voice.sessions import VoiceError
from flowly.session.manager import SessionManager


@pytest.fixture
def output_runtime(tmp_path, monkeypatch):
    home = tmp_path / '.flowly'
    monkeypatch.delenv('FLOWLY_HOME', raising=False)
    monkeypatch.setattr(profiles, '_DEFAULT_HOME', home)
    monkeypatch.setattr(profiles, '_PROFILES_ROOT', home / 'profiles')
    workspace = home / 'workspace'
    workspace.mkdir(parents=True)
    sessions = SessionManager(workspace)
    session = sessions.get_or_create('desktop:voice-work:task-1')
    sessions.save(session)
    store = ArtifactStore(tmp_path / 'artifacts.db')
    service = WorkOutputs(workspace, sessions, lambda: store)
    scope = {'sessionKey': session.key, 'expectedBotId': profiles.ensure_profile_bot_id('default').bot_id}
    yield service, workspace, store, scope
    store.close()


def read(service, scope, path, **params):
    return service.read({**scope, 'source': 'file', 'path': path, **params})


def test_file_windows_are_versioned_and_contain_no_other_host_authority(output_runtime):
    service, workspace, _, scope = output_runtime
    (workspace / 'rapor.md').write_text('Merhaba dünya', encoding='utf-8')
    first = read(service, scope, 'rapor.md', length=8)
    rest = read(service, scope, first['path'], offset=8, expectedRevision=first['revision'])
    assert base64.b64decode(first['data']) + base64.b64decode(rest['data']) == 'Merhaba dünya'.encode()
    assert first['sessionKey'] == scope['sessionKey']
    assert first['eof'] is False and rest['eof'] is True
    assert first['mimeType'] == 'text/markdown'
    assert first['revision'] == rest['revision']
    (workspace / 'rapor.md').write_text('Yeni içerik', encoding='utf-8')
    with pytest.raises(VoiceError, match='changed'):
        read(service, scope, 'rapor.md', offset=8, expectedRevision=first['revision'])


@pytest.mark.parametrize('change', [{'sessionKey': 'desktop:default'}, {'sessionKey': 'desktop:voice-work:missing'}, {'expectedBotId': 'other'}])
def test_unknown_conversation_or_replaced_agent_cannot_read(output_runtime, change):
    service, workspace, _, scope = output_runtime
    (workspace / 'report.md').write_text('Private')
    with pytest.raises((VoiceError, ValueError)):
        read(service, {**scope, **change}, 'report.md')


@pytest.mark.parametrize('path', ['../config.json', '.env', 'key.pem', '../profiles/other/config.json'])
def test_private_paths_are_not_a_file_preview_surface(output_runtime, path):
    service, workspace, _, scope = output_runtime
    file = workspace / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('private key')
    with pytest.raises(VoiceError):
        read(service, scope, path)


def test_symlink_escape_and_nonregular_files_are_rejected(output_runtime):
    service, workspace, _, scope = output_runtime
    secret = workspace.parent / 'config.json'
    secret.write_text('private')
    (workspace / 'report.md').symlink_to(secret)
    with pytest.raises(VoiceError):
        read(service, scope, 'report.md')
    os.mkfifo(workspace / 'pipe.txt')
    with pytest.raises(VoiceError):
        read(service, scope, 'pipe.txt')


@pytest.mark.parametrize('params', [{'offset': -1}, {'offset': True}, {'length': 9999999}, {'expectedRevision': 'wrong'}, {'offset': 1}, {'path': '/dev/null'}])
def test_bad_windows_do_not_return_file_content(output_runtime, params):
    service, workspace, _, scope = output_runtime
    (workspace / 'report.txt').write_text('data')
    request = {'path': 'report.txt', **params}
    with pytest.raises(VoiceError):
        read(service, scope, **request)


def test_artifacts_are_filtered_by_session_and_internal_visibility(output_runtime):
    service, _, store, scope = output_runtime
    owned = store.create(type='html', title='Dashboard', content='<h1>Ready</h1>', session_key=scope['sessionKey'])
    foreign = store.create(type='html', title='Another task', content='private', session_key='desktop:voice-work:other')
    internal = store.create(type='markdown', title='Context', content='internal', session_key=scope['sessionKey'], tags=['internal:context'])
    listed = service.list(scope)
    assert [item['id'] for item in listed['artifacts']] == [owned['id']]
    first = service.read({**scope, 'source': 'artifact', 'id': owned['id'], 'length': 6})
    rest = service.read({**scope, 'source': 'artifact', 'id': owned['id'], 'offset': 6, 'expectedRevision': first['revision']})
    assert base64.b64decode(first['data']) + base64.b64decode(rest['data']) == b'<h1>Ready</h1>'
    for item in (foreign, internal):
        with pytest.raises(VoiceError):
            service.read({**scope, 'source': 'artifact', 'id': item['id']})
    store.update(owned['id'], content='Replacement')
    with pytest.raises(VoiceError, match='changed'):
        service.read({**scope, 'source': 'artifact', 'id': owned['id'], 'expectedRevision': first['revision']})


@pytest.mark.parametrize('action', ['update', 'promote', 'export'])
async def test_successful_tool_output_can_belong_to_more_than_one_session(output_runtime, monkeypatch, action):
    from flowly.agent.tools import artifact as artifact_module

    service, workspace, store, scope = output_runtime
    original = 'desktop:voice-work:original'
    item = store.create(type='html', title='Existing report', content='<h1>Before</h1>', session_key=original)
    tool = artifact_module.ArtifactTool(store)
    request = {**scope, 'source': 'artifact', 'id': item['id']}
    # Merely retrieving an old artifact must not claim it as this task's output.
    await tool.execute('get', artifact_id=item['id'], session_key=scope['sessionKey'])
    assert service.list(scope)['artifacts'] == []
    with pytest.raises(VoiceError):
        service.read(request)
    monkeypatch.setattr(artifact_module, '_export_path_allowed', lambda path: path.parent == workspace)
    result = json.loads(await tool.execute(action, artifact_id=item['id'], session_key=scope['sessionKey'],
                                           content='<h1>After</h1>', path=str(workspace / 'export.html')))
    assert 'error' not in result
    assert service.read(request)['id'] == item['id']
    assert [row['id'] for row in service.list(scope)['artifacts']] == [item['id']]
    assert store.get(item['id'])['session_key'] == original
    # Repeat association and re-open the database: membership is durable and deduplicated.
    store.attach_session_output(item['id'], scope['sessionKey'])
    reopened = ArtifactStore(store._db_path)
    try:
        assert reopened.has_session_output(item['id'], original)
        assert reopened.has_session_output(item['id'], scope['sessionKey'])
        assert len(reopened.session_summaries(scope['sessionKey'], 0, 50)) == 1
    finally:
        reopened.close()


async def test_failed_output_and_hidden_artifacts_do_not_become_visible(output_runtime, monkeypatch):
    from flowly.agent.tools import artifact as artifact_module

    service, workspace, store, scope = output_runtime
    tool = artifact_module.ArtifactTool(store)
    item = store.create(type='markdown', title='Private', content='secret', session_key='other')
    monkeypatch.setattr(artifact_module, '_export_path_allowed', lambda path: False)
    result = json.loads(await tool.execute('export', artifact_id=item['id'], session_key=scope['sessionKey'], path=str(workspace / 'out.md')))
    assert 'error' in result
    assert not store.has_session_output(item['id'], scope['sessionKey'])
    await tool.execute('update', artifact_id=item['id'], session_key=scope['sessionKey'], tags=['internal:context'])
    assert store.has_session_output(item['id'], scope['sessionKey'])
    assert service.list(scope)['artifacts'] == []
    with pytest.raises(VoiceError):
        service.read({**scope, 'source': 'artifact', 'id': item['id']})
    assert not store.attach_session_output('missing', scope['sessionKey'])
    store.delete(item['id'])
    assert not store.has_session_output(item['id'], scope['sessionKey'])


def test_artifact_update_and_output_membership_commit_together(output_runtime):
    _, _, store, scope = output_runtime
    item = store.create(type='markdown', title='Report', content='before', session_key='original')
    store._conn.executescript("""
        CREATE TRIGGER reject_output BEFORE INSERT ON artifact_session_outputs
        BEGIN SELECT RAISE(ABORT, 'test write failure'); END;
    """)
    with pytest.raises(sqlite3.IntegrityError):
        store.update(item['id'], content='after', output_session_key=scope['sessionKey'])
    assert store.get(item['id'])['content'] == 'before'
    assert store.get_versions(item['id']) == []
    assert not store.has_session_output(item['id'], scope['sessionKey'])


@pytest.mark.parametrize('kind', ['chart', 'form'])
def test_html_artifact_types_keep_their_format(output_runtime, kind):
    service, _, store, scope = output_runtime
    item = store.create(type=kind, title='Report', content='<h1>Ready</h1>', session_key=scope['sessionKey'])
    result = service.read({**scope, 'source': 'artifact', 'id': item['id']})
    assert result['mimeType'] == 'text/html'
    assert result['fileName'].endswith('.html')


def test_empty_and_oversized_files_have_explicit_outcomes(output_runtime):
    service, workspace, _, scope = output_runtime
    (workspace / 'empty.txt').touch()
    assert read(service, scope, 'empty.txt')['data'] == ''
    assert read(service, scope, 'empty.txt')['eof'] is True
    with (workspace / 'large.txt').open('wb') as stream:
        stream.truncate(33 * 1024 * 1024)
    with pytest.raises(VoiceError) as failure:
        read(service, scope, 'large.txt')
    assert failure.value.code == 'OUTPUT_TOO_LARGE'


def test_parent_link_swap_between_validation_and_open_cannot_escape(output_runtime, monkeypatch, tmp_path):
    service, workspace, _, scope = output_runtime
    folder = workspace / 'output'
    folder.mkdir()
    (folder / 'report.txt').write_text('Allowed')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'report.txt').write_text('Private')
    original = service._file_path

    def swap(raw):
        resolved = original(raw)
        folder.rename(workspace / 'original')
        folder.symlink_to(outside, target_is_directory=True)
        return resolved

    monkeypatch.setattr(service, '_file_path', swap)
    with pytest.raises(VoiceError):
        read(service, scope, 'output/report.txt')


@pytest.mark.asyncio
async def test_output_rpc_is_shared_by_gateway_relay_and_profile_host(output_runtime, monkeypatch):
    from flowly.bus.queue import MessageBus
    from flowly.channels import feature_rpc
    from flowly.channels.web import WebChannel
    from flowly.config.schema import WebChannelConfig
    from flowly.gateway.server import GatewayServer
    from flowly.profile_host import ProfileHost
    from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc
    from tests.test_chat_command_transport import Socket

    service, workspace, _, scope = output_runtime
    (workspace / 'report.txt').write_text('Verified output')
    monkeypatch.setattr(feature_rpc, '_work_output_provider', lambda: service)
    params = {**scope, 'source': 'file', 'path': 'report.txt'}
    gateway = GatewayServer(sessions=service.sessions)
    socket = Socket()
    await gateway._handle_feature_rpc(socket, 'output-1', 'chat.outputs.read', params)
    direct = socket.sent[-1]['result']
    relay = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    await relay._handle_rpc(socket, {'id': 'output-2', 'method': 'chat.outputs.read', 'sessionId': 'relay-1', 'params': params})
    assert socket.sent[-1]['result'] == direct

    async def primary(method, forwarded, _timeout):
        result, _ = await feature_rpc.dispatch(method, forwarded)
        return result

    host = ProfileHost(primary_rpc=primary)
    via_host = await host.rpc('default', 'chat.outputs.read', {k: v for k, v in params.items() if k != 'expectedBotId'}, expected_host_id=host.host_id, expected_bot_id=scope['expectedBotId'])
    assert via_host == direct
    assert base64.b64decode(direct['data']) == b'Verified output'
    with pytest.raises(ProfileHostError):
        validate_profile_rpc('chat.outputs.read', {**params, 'length': 9999999})
    await gateway.stop()
