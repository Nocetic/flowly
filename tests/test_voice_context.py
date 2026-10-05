import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from flowly.channels import feature_rpc
from flowly.live_voice.context import VoiceContext
from flowly.memory.governance import GovernanceStore
from flowly.memory.knowledge_graph import KnowledgeGraph
from flowly.memory.summary import SENTINEL_END, SENTINEL_START
from flowly.profile_host_contract import ProfileHostError, validate_profile_rpc


@pytest.fixture
def context(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'memory').mkdir()
    store = GovernanceStore(tmp_path / 'memory_governance.sqlite3')
    index = SimpleNamespace(search=AsyncMock(return_value=[]))
    context = VoiceContext(workspace, state_db=lambda name: tmp_path / name,
                           profile=lambda: ('creative', 'agent-creative'), index=lambda: index)
    yield context, workspace, store, index
    store.close()


@pytest.mark.asyncio
async def test_recall_has_scoped_provenance_and_revisions(context):
    reader, workspace, store, _ = context
    item = store.add_item(kind='preference', text='Hakan prefers Turkish.', status='active',
                          source_session='desktop:work-1', source_message_ids=['msg-1'])
    (workspace / 'USER.md').write_text('Hakan builds Flowly.')
    result = await reader.search({})
    fact = next(f for f in result['facts'] if 'prefers' in f['text'])
    assert fact['sourceRef'].endswith(item.id)
    assert fact['sourceSession'] == 'desktop:work-1'
    assert fact['sourceMessageIds'] == ['msg-1']
    assert result['scope'] == {'profile': 'creative', 'botId': 'agent-creative'}
    assert not result['partial']
    assert (await reader.search({}))['revision'] == result['revision']
    store.transition(item.id, 'rejected')
    assert (await reader.search({}))['revision'] != result['revision']


@pytest.mark.asyncio
async def test_privacy_and_lifecycle_apply_to_graph_and_manual_mirrors(context, tmp_path):
    reader, workspace, store, _ = context
    graph = KnowledgeGraph(str(tmp_path / 'knowledge_graph.sqlite3'))
    for status, privacy, marker in [('active', 'secret', 'secret-plan'), ('active', 'sensitive', 'private-plan'),
                                     ('superseded', 'normal', 'old-plan'), ('needs_review', 'normal', 'unconfirmed-plan')]:
        triple = graph.add_triple('Hakan', 'plans', marker)
        store.add_item(kind='fact', text=f'Hakan plans {marker}', ref_kind='kg_triple', ref_id=triple,
                       status=status, privacy_level=privacy)
    graph.add_triple('Flowly', 'uses', 'ElevenLabs')
    (workspace / 'memory/MEMORY.md').write_text('Copied note: Hakan plans secret-plan')
    result = await reader.search({})
    text = json.dumps(result)
    assert 'secret-plan' not in text and 'private-plan' not in text and 'old-plan' not in text and 'unconfirmed-plan' not in text
    assert any('ElevenLabs' in fact['text'] for fact in result['facts'])


@pytest.mark.asyncio
async def test_stale_index_generated_regions_and_foreign_paths_are_not_exported(context, tmp_path):
    reader, workspace, _, index = context
    (workspace / 'memory/MEMORY.md').write_text(f'Manual voice note.\n{SENTINEL_START}\nGenerated voice secret.\n{SENTINEL_END}')
    (tmp_path / 'private.md').write_text('Foreign voice secret.')
    (workspace / 'memory/linked.md').symlink_to(tmp_path / 'private.md')
    index.search.return_value = [SimpleNamespace(path=path, snippet=text, start_line=1, end_line=1) for path, text in [
        ('memory/MEMORY.md', 'Stale voice fact.'), ('memory/MEMORY.md', 'Generated voice secret.'),
        ('memory/linked.md', 'Foreign voice secret.'), ('../private.md', 'Foreign voice secret.'),
    ]]
    result = await reader.search({'query': 'voice'})
    assert [f['text'] for f in result['facts']] == ['Manual voice note.']


@pytest.mark.asyncio
async def test_corrupt_governance_does_not_turn_into_unfiltered_recall(context, tmp_path):
    reader, workspace, _, _ = context
    broken = tmp_path / 'corrupt.sqlite3'
    broken.write_text('corrupt database')
    reader.state_db = lambda name: broken
    (workspace / 'USER.md').write_text('Private voice details.')
    result = await reader.search({})
    assert result['facts'] == []
    assert result['partial']
    assert all(s['status'] == 'unavailable' for s in result['sources'].values())


@pytest.mark.asyncio
async def test_index_failure_keeps_available_sources_and_reports_partial(context):
    reader, workspace, _, index = context
    (workspace / 'USER.md').write_text('Hakan speaks Turkish.')
    index.search.side_effect = RuntimeError('token=do-not-export-in-errors')
    result = await reader.search({'query': 'Turkish'})
    assert result['partial']
    assert len(result['facts']) == 1
    assert 'do-not-export' not in json.dumps(result)


@pytest.mark.asyncio
async def test_credentials_redacted_and_results_bounded(context):
    reader, workspace, _, _ = context
    secret = 'sk-' + 'a' * 30
    (workspace / 'USER.md').write_text(f'Flowly configuration: api_key={secret}\n\n' + '\n\n'.join(f'Voice note {i}' for i in range(30)))
    result = await reader.search({'limit': 3})
    assert len(result['facts']) == 3
    assert secret not in json.dumps(result)
    assert '[redacted]' in json.dumps(result)


@pytest.mark.asyncio
async def test_context_rpc_returns_the_same_projection_for_named_profile(context, monkeypatch):
    reader, _, _, _ = context
    monkeypatch.setattr(feature_rpc, '_voice_context_provider', lambda: reader)
    monkeypatch.setattr('flowly.profile.current_profile_name', lambda: 'creative')
    assert 'voice.context' in feature_rpc.system_capabilities()['featureMethods']
    result, restart = await feature_rpc.dispatch(*validate_profile_rpc('voice.context', {'query': 'Flowly'}))
    assert result['scope']['profile'] == 'creative'
    assert not restart


@pytest.mark.parametrize('params', [{'profile': 'other'}, {'path': '../private'}, {'query': []}, {'limit': True}, {'limit': 100}])
def test_profile_host_cannot_expand_context_scope(params):
    with pytest.raises(ProfileHostError):
        validate_profile_rpc('voice.context', params)


@pytest.mark.asyncio
async def test_memory_search_matches_count_even_without_the_query_words(context):
    """Recall uses the agent's own memory search; a semantic match that does
    not repeat the query's words is still a match, as in the agent's chat."""
    reader, workspace, _, index = context
    (workspace / 'memory/2026-10-01.md').write_text('Hakan drinks an Americano every morning.')
    index.search.return_value = [SimpleNamespace(path='memory/2026-10-01.md', snippet='Hakan drinks an Americano every morning.',
                                                 start_line=1, end_line=1)]
    result = await reader.search({'query': 'kahve alışkanlığı'})
    assert any('Americano' in fact['text'] for fact in result['facts'])
