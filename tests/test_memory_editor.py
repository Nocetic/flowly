from concurrent.futures import ThreadPoolExecutor

import pytest

from flowly.memory.editor import MemoryEditorError, dispatch_editor, revision
from flowly.memory.governance import GovernanceStore
from flowly.memory.summary import SENTINEL_START, SENTINEL_END


@pytest.fixture
def editor(tmp_path):
    workspace = tmp_path / 'configured-workspace'
    workspace.mkdir()
    db = tmp_path / 'memory.sqlite3'
    def call(action, **params):
        return dispatch_editor(action, params, workspace, db)
    return call, workspace, db


def test_documents_are_isolated_and_conflict_checked(editor):
    call, ws, _ = editor
    first = call('document', document='user')
    saved = call('save', kind='user', text='Name: Jane', revision=first['revision'])
    assert (ws / 'USER.md').read_text() == saved['text'] == 'Name: Jane'
    assert call('document', document='notes')['text'] == ''
    with pytest.raises(MemoryEditorError, match='changed'):
        call('save', kind='user', text='stale draft', revision=first['revision'])
    assert call('document', document='user') == saved


def test_notes_preserve_generated_region(editor):
    call, ws, _ = editor
    (ws / 'memory').mkdir()
    generated = f'{SENTINEL_START}\nAuto content\n{SENTINEL_END}'
    (ws / 'memory/MEMORY.md').write_text(f'Manual\n\n{generated}\n')
    notes = call('document', document='notes')
    assert notes['text'] == 'Manual'
    saved = call('save', kind='notes', text='Edited', revision=notes['revision'])
    assert saved['text'] == 'Edited'
    assert generated in (ws / 'memory/MEMORY.md').read_text()
    with pytest.raises(MemoryEditorError):
        call('save', kind='notes', text=generated, revision=saved['revision'])


@pytest.mark.parametrize('params', [
    {'document': '../secret'}, {'document': []}, {'document': None},
])
def test_document_paths_are_not_user_controlled(editor, params):
    with pytest.raises(MemoryEditorError):
        editor[0]('document', **params)


def test_rejects_symlink(editor, tmp_path):
    call, ws, _ = editor
    outside = tmp_path / 'outside'
    outside.write_text('untouched')
    (ws / 'USER.md').symlink_to(outside)
    with pytest.raises(MemoryEditorError):
        call('save', kind='user', text='changed', revision=revision('untouched'))
    assert outside.read_text() == 'untouched'


@pytest.mark.parametrize('params', [
    {'kind': [], 'text': 'x', 'revision': 'a' * 64},
    {'kind': 'user', 'text': 'x' * 262145, 'revision': 'a' * 64},
    {'kind': 'entry', 'id': 'x', 'text': ' ', 'revision': 'a' * 64},
])
def test_invalid_drafts(editor, params):
    with pytest.raises(MemoryEditorError):
        editor[0]('save', **params)


def test_keyset_pagination_and_literal_search(editor):
    call, _, db = editor
    store = GovernanceStore(db)
    try:
        for i in range(57):
            store.add_item(kind='profile', text=f'User {i}', status='active')
        store.add_item(kind='profile', text='Hidden candidate')
        first = call('list')
        second = call('list', cursor=first['nextCursor'])
        assert len(first['items']) == 50
        assert len(second['items']) == 7
        assert second['nextCursor'] is None
        assert len({r['id'] for r in first['items'] + second['items']}) == 57
        assert call('list', query='%')['items'] == []
    finally:
        store.close()


def test_concurrent_corrections_have_one_winner_and_update_summary(editor):
    call, ws, db = editor
    store = GovernanceStore(db)
    item = store.add_item(kind='profile', text='Before', status='active', source_session='chat:1')
    store.close()
    row = call('list')['items'][0]
    def save(text):
        try:
            return call('save', kind='entry', id=item.id, text=text, revision=row['revision'])
        except MemoryEditorError as error:
            return error.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(save, ['After A', 'After B']))
    assert results.count('MEMORY_CONFLICT') == 1
    winner = next(r for r in results if isinstance(r, dict))
    assert winner['summaryPending'] is False
    assert winner['item']['sourceSession'] == 'chat:1'
    assert winner['item']['text'] in (ws / 'memory/MEMORY.md').read_text()


def test_partial_summary_failure_does_not_report_lost_save(editor, monkeypatch):
    call, _, db = editor
    store = GovernanceStore(db)
    store.add_item(kind='profile', text='Before', status='active')
    store.close()
    row = call('list')['items'][0]
    def fail(*args, **kwargs):
        raise OSError('disk unavailable')
    monkeypatch.setattr('flowly.memory.editor.regenerate_memory_md', fail)
    saved = call('save', kind='entry', id=row['id'], text='After', revision=row['revision'])
    assert saved['summaryPending']
    assert call('list')['items'][0]['text'] == 'After'


def test_correction_keeps_unrelated_graph_summary(editor):
    call, ws, db = editor
    from flowly.agent.memory import MemoryStore
    from flowly.memory.summary import regenerate_memory_md
    store = GovernanceStore(db)
    store.add_item(kind='preference', text='Dark mode', status='active')
    regenerate_memory_md(store, MemoryStore(ws), kg_summary='- Existing graph fact')
    store.close()
    row = call('list')['items'][0]
    call('save', kind='entry', id=row['id'], text='Light mode', revision=row['revision'])
    content = (ws / 'memory/MEMORY.md').read_text()
    assert 'Light mode' in content and 'Dark mode' not in content
    assert '- Existing graph fact' in content


@pytest.mark.asyncio
async def test_feature_rpc_uses_configured_workspace_off_receive_loop(editor, monkeypatch):
    from types import SimpleNamespace
    from flowly.channels import feature_rpc
    _, ws, db = editor
    monkeypatch.setattr('flowly.config.loader.load_config', lambda: SimpleNamespace(workspace_path=ws))
    monkeypatch.setattr(feature_rpc, 'state_db', lambda _: db)
    method = 'memory.editor.document'
    assert method in feature_rpc.LONG_RUNNING_METHODS
    result, restart = await feature_rpc.dispatch(method, {'document': 'user'})
    assert result == {'text': '', 'revision': revision('')}
    assert not restart


@pytest.mark.asyncio
async def test_remote_editor_requires_profile_identity(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    import flowly.profile as profiles
    monkeypatch.setattr(profiles, '_DEFAULT_HOME', tmp_path)
    monkeypatch.setattr(profiles, '_PROFILES_ROOT', tmp_path / 'profiles')
    from flowly.profile_host import ProfileHost
    from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc
    for method in ('memory.editor.list', 'memory.editor.document', 'memory.editor.save'):
        assert validate_profile_rpc(method, {}) == (method, {})
        host = ProfileHost()
        host._target_rpc = AsyncMock()
        with pytest.raises(ProfileHostError) as error:
            await host.rpc('writer', method, {})
        assert error.value.code == 'INVALID_PARAMS'
        host._target_rpc.assert_not_awaited()
