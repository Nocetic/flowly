"""Real SQLite search remains usable without a responsive embedding service."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from flowly.agent.tools.memory_search import MemorySearchTool
from flowly.live_voice.context import VoiceContext
from flowly.memory.manager import MemoryIndexManager
from flowly.memory.summary import SENTINEL_END, SENTINEL_START


@pytest.fixture
def memory(tmp_path):
    workspace = tmp_path / 'workspace'
    (workspace / 'memory').mkdir(parents=True)
    path = workspace / 'memory/note.md'
    path.write_text('Nora drinks chamomile tea every evening.')
    manager = MemoryIndexManager(workspace, tmp_path / 'state', provider='openai', api_key='sk-dummy')
    try:
        yield manager, path
    finally:
        manager._indexer.close()


def reader(manager):
    return VoiceContext(manager._workspace, state_db=lambda name: manager._workspace / name,
                        profile=lambda: ('default', 'agent-id'), index=lambda: manager)


@pytest.mark.asyncio
async def test_failed_embeddings_keep_keyword_results_and_do_not_reindex_unchanged_files(memory, monkeypatch):
    manager, path = memory
    embed = AsyncMock(return_value=None)
    query = AsyncMock(return_value=None)
    monkeypatch.setattr('flowly.memory.manager.embed_texts', embed)
    monkeypatch.setattr('flowly.memory.manager.embed_single', query)
    for _ in range(2):
        manager._last_sync = 0
        result = json.loads(await MemorySearchTool(manager).execute('chamomile'))
        assert len(result['results']) == 1
    assert not manager._indexer.needs_reindex(path, manager._workspace)
    embed.assert_awaited_once()
    query.assert_not_awaited()
    assert len(await manager.search('"chamomile"')) == 1
    path.write_text('Nora now drinks peppermint.')
    manager._last_sync = 0
    assert not await manager.search('chamomile')
    assert len(await manager.search('peppermint')) == 1
    path.write_text('')
    manager._last_sync = 0
    assert not await manager.search('peppermint')


@pytest.mark.asyncio
@pytest.mark.parametrize('slow_stage', ['index', 'query'])
async def test_voice_preserves_lexical_hits_when_embeddings_stall(memory, monkeypatch, slow_stage):
    manager, _ = memory
    cancelled = asyncio.Event()
    async def stall(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr('flowly.memory.manager.embed_texts',
                        stall if slow_stage == 'index' else AsyncMock(return_value=[[1.0, 0.0]]))
    monkeypatch.setattr('flowly.memory.manager.embed_single', stall)
    result = await asyncio.wait_for(reader(manager).search({'query': 'chamomile'}), 1.4)
    assert any('chamomile' in fact['text'] for fact in result['facts'])
    assert result['sources']['search']['status'] == 'ok'
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_embeddings_recover_after_backoff_without_reindexing_files(memory, monkeypatch):
    manager, _ = memory
    embed = AsyncMock(return_value=None)
    monkeypatch.setattr('flowly.memory.manager.embed_texts', embed)
    monkeypatch.setattr('flowly.memory.manager.embed_single', AsyncMock(return_value=[1.0, 0.0]))
    assert await manager.search('chamomile')
    manager._embedding_retry_at = 0
    embed.return_value = [[1.0, 0.0]]
    result = await manager.search('unrelated-query')
    assert result[0].vector_score == 1.0
    await manager.search('another-query')
    assert embed.await_count == 2


@pytest.mark.asyncio
async def test_late_embedding_cannot_replace_changed_text(memory, monkeypatch):
    manager, path = memory
    started, release = asyncio.Event(), asyncio.Event()
    async def embed(*args, **kwargs):
        started.set()
        await release.wait()
        return [[1.0, 0.0]]
    monkeypatch.setattr('flowly.memory.manager.embed_texts', embed)
    monkeypatch.setattr('flowly.memory.manager.embed_single', AsyncMock(return_value=[1.0, 0.0]))
    task = asyncio.create_task(manager.search('chamomile'))
    await started.wait()
    path.write_text('Nora now drinks peppermint.')
    manager._last_sync = 0
    await manager.search('peppermint', embedding_timeout=0)
    release.set()
    await task
    chunks = manager._indexer.get_all_chunks()
    assert chunks[0]['text'] == 'Nora now drinks peppermint.'
    assert chunks[0]['embedding'] is None


@pytest.mark.asyncio
async def test_full_source_evidence_survives_display_truncation_but_not_privacy_filter(memory):
    manager, path = memory
    manager._emb_provider = None
    text = 'UniqueMarker ' + 'kelime ' * 140
    path.write_text(text)
    hits = await manager.search('UniqueMarker')
    assert len(hits[0].snippet) == 700 and hits[0].snippet.endswith('...')
    result = await reader(manager).search({'query': 'UniqueMarker'})
    assert len(result['facts']) == 1
    assert result['facts'][0]['text'] == text[:900]
    path.write_text(f'{SENTINEL_START}\n{text}\n{SENTINEL_END}')
    manager._last_sync = 0
    assert not (await reader(manager).search({'query': 'UniqueMarker'}))['facts']
