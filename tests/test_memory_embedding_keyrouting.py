"""Embedding credentials and endpoint belong to the same provider."""
from types import SimpleNamespace

import pytest

from flowly.config.schema import Config
from flowly.memory.embeddings import resolve_embedding_settings
from flowly.memory.manager import MemoryIndexManager


@pytest.mark.parametrize('key', ['sk-or-v1-dummy', 'sk-ant-dummy', 'xai-dummy', 'flw_dummy'])
def test_foreign_key_is_not_inferred_as_openai_or_gemini(key):
    assert resolve_embedding_settings('auto', '', key, '', None) == (None, '', '', '')


def test_openrouter_chat_does_not_supply_embedding_credentials(tmp_path):
    from flowly.agent.loop import AgentLoop
    config = Config()
    config.providers.openrouter.api_key = 'sk-or-v1-dummy'
    config.providers.openrouter.api_base = 'https://openrouter.invalid/api/v1'
    agent = SimpleNamespace(_memory_search_config=config.agents.defaults.memory_search,
                            _main_config=config, _state_dir=tmp_path, workspace=tmp_path)
    manager = AgentLoop._build_memory_manager(agent)
    try:
        assert manager.status()['provider'] == 'none'
        assert manager._api_key == manager._api_base == ''
    finally:
        manager._indexer.close()


def test_auto_uses_openai_key_and_base_even_when_chat_uses_openrouter(tmp_path):
    config = Config()
    config.providers.openrouter.api_key = 'sk-or-v1-dummy'
    config.providers.openai.api_key = 'sk-openai-dummy'
    config.providers.openai.api_base = 'https://embeddings.invalid/v1'
    manager = MemoryIndexManager(tmp_path, tmp_path, config=config, model='custom-embedding')
    try:
        assert (manager._emb_provider, manager._emb_model, manager._api_key, manager._api_base) == (
            'openai', 'custom-embedding', 'sk-openai-dummy', 'https://embeddings.invalid/v1')
    finally:
        manager._indexer.close()


def test_explicit_compatible_endpoint_and_key_are_preserved():
    assert resolve_embedding_settings('openai', 'vendor/embedding', 'vendor-key',
                                      'https://vendor.invalid/v1', None) == (
        'openai', 'vendor/embedding', 'vendor-key', 'https://vendor.invalid/v1')
    assert resolve_embedding_settings('none', '', 'sk-dummy', '', None) == (None, '', '', '')
