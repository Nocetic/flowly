"""Every request a client sends for an agent's memory reaches that agent.

The envelopes come from tests/fixtures/live_voice_client_requests.json, the
contract Desktop and iOS test their own request builders against. Each one
goes through the real routing: ProfileHost.rpc (identity pins, the
expectedBotId it forwards), the runtime's feature_rpc.dispatch (identity
check, field cleanup) and the strict voice validators. A client that sends a
shape Core does not accept fails here, not in the owner's call.
"""
import json
from pathlib import Path

import pytest

import flowly.profile as profiles
from flowly.channels import feature_rpc
from flowly.live_voice.context import validate_context
from flowly.live_voice.memory_snapshot import validate_snapshot
from flowly.profile_host import ProfileHost, ProfileHostError

CONTRACT = json.loads((Path(__file__).parent / 'fixtures' / 'live_voice_client_requests.json').read_text())


@pytest.fixture
def host(tmp_path, monkeypatch):
    default = tmp_path / '.flowly'
    monkeypatch.setattr(profiles, '_DEFAULT_HOME', default)
    monkeypatch.setattr(profiles, '_PROFILES_ROOT', default / 'profiles')
    monkeypatch.setenv('FLOWLY_HOME', str(default))
    (default / 'workspace').mkdir(parents=True)
    profiles.create_profile('writer', local_runtime=True)
    served: list[tuple[str, str, dict]] = []
    runtime = {'profile': 'default'}
    monkeypatch.setattr(profiles, 'current_profile_name', lambda: runtime['profile'])

    class Snapshot:
        def snapshot(self, params):
            validate_snapshot(params)
            served.append((runtime['profile'], 'snapshot', params))
            return {'scope': {'profile': runtime['profile']}, 'sections': [{'kind': 'user', 'text': 'Hakan'}]}

    class Context:
        async def search(self, params):
            validate_context(params)
            served.append((runtime['profile'], 'recall', params))
            return {'scope': {'profile': runtime['profile']}, 'facts': [{'text': 'Hakan'}]}

    monkeypatch.setattr(feature_rpc, '_voice_snapshot_provider', lambda: Snapshot())
    monkeypatch.setattr(feature_rpc, '_voice_context_provider', lambda: Context())

    async def runtime_dispatch(profile, method, params):
        # The agent's own runtime: its profile identity, its dispatch.
        runtime['profile'] = profile
        try:
            return (await feature_rpc.dispatch(method, params))[0]
        finally:
            runtime['profile'] = 'default'

    profile_host = ProfileHost()
    original = profile_host._target_rpc

    async def target_rpc(target, method, params, timeout, **identity):
        if target == 'default':
            return await original(target, method, params, timeout, **identity)
        return await runtime_dispatch(target, method, params)

    profile_host._target_rpc = target_rpc  # type: ignore[method-assign]
    profile_host._primary_rpc = lambda method, params, timeout: runtime_dispatch('default', method, params)
    return profile_host, served, runtime_dispatch


def envelope(entry: dict, host: ProfileHost, bot_id: str) -> dict:
    text = json.dumps(entry['envelope']).replace('$hostId', host.host_id).replace('$botId', bot_id)
    return json.loads(text)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', CONTRACT['requests'], ids=[entry['id'] for entry in CONTRACT['requests']])
async def test_every_client_request_reaches_the_agents_memory(host, entry):
    profile_host, served, runtime_dispatch = host
    bot_id = profiles.ensure_profile_bot_id(entry['profile']).bot_id
    request = envelope(entry, profile_host, bot_id)
    if request['method'] == 'profiles.rpc':
        result = await profile_host.dispatch('profiles.rpc', request['params'])
    else:
        result = await runtime_dispatch(entry['profile'], request['method'], request['params'])
    assert result.get('sections') or result.get('facts'), entry['id']
    profile, kind, params = served[-1]
    assert profile == entry['profile']
    # The handler sees only its own fields; routing and identity never leak in.
    assert set(params) <= {'knownRevision', 'query', 'limit'}


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', [e for e in CONTRACT['requests'] if 'expectedBotId' in e['envelope']['params']],
                         ids=lambda e: e['id'])
async def test_a_request_pinned_to_another_agent_is_refused(host, entry):
    profile_host, served, _ = host
    request = envelope(entry, profile_host, 'another-agent')
    with pytest.raises((ProfileHostError, feature_rpc.FeatureRpcError)) as refused:
        await profile_host.dispatch('profiles.rpc', request['params'])
    assert refused.value.code == 'PROFILE_IDENTITY_CHANGED'
    assert not served


def test_the_contract_covers_both_clients_both_agents_and_both_reads():
    seen = {(entry['client'], entry['profile'] == 'default', entry['envelope']['params'].get('method')
             or entry['envelope']['method']) for entry in CONTRACT['requests']}
    for client in ('ios', 'desktop'):
        for default in (True, False):
            for method in ('voice.memory.snapshot', 'voice.context'):
                assert (client, default, method) in seen
