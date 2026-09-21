from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import flowly.profile as profiles
from flowly.cli.gateway_cmd import _publish_cron_lifecycle
from flowly.cron.service import CronService
from flowly.cron.types import CronSchedule
from flowly.gateway.server import GatewayServer
from flowly.profile_host import ProfileHost
from flowly.profile_host_contract import ProfileHostError
from flowly.push import relay_push


@pytest.fixture
def host_setup(tmp_path, monkeypatch):
    home = tmp_path / '.flowly'
    home.mkdir()
    (home / 'workspace').mkdir()
    monkeypatch.setattr(profiles, '_DEFAULT_HOME', home)
    monkeypatch.setattr(profiles, '_PROFILES_ROOT', home / 'profiles')
    monkeypatch.setenv('FLOWLY_HOME', str(home))
    profiles.create_profile('writer', local_runtime=True)
    registry = relay_push.PushRegistry(home / 'push_subs.json')
    registry.register(push_id='ios-device', push_secret='test', gateway_id='ios-gateway', platform='ios')
    registry.register(push_id='android-device', push_secret='test', gateway_id='android-relay', platform='android', kind='relay')
    monkeypatch.setattr(relay_push, '_registry', registry)
    monkeypatch.setattr(relay_push, '_relay_base', lambda: 'https://relay.test')
    sent = []

    def send(base, sub, title, body, data):
        sent.append({'device': sub['pushId'], 'title': title, 'body': body, 'data': data})
        return 200

    monkeypatch.setattr(relay_push, '_send_one', send)
    return home, ProfileHost(), sent


async def finished_run(home, *, deliver=True, response='Result from writer', channel=None, to=None):
    async def execute(job):
        return response

    service = CronService(home / 'profiles/writer/cron/jobs.json', on_job=execute)
    job = service.add_job('Daily report', CronSchedule(kind='at', at_ms=1), 'test',
                          deliver=deliver, channel=channel, to=to)
    assert await service.run_job(job.id, force=True)
    return service, job, {'name': 'writer', 'jobId': job.id, 'runId': job.state.last_run_id}


@pytest.mark.asyncio
async def test_profile_result_uses_primary_devices_and_exact_profile_without_child_credentials(host_setup):
    home, host, sent = host_setup
    service, job, request = await finished_run(home)
    before = service.store_path.read_bytes()
    # No phone sockets or Desktop subscribers are attached to this host.
    assert host._event_subscribers == {}
    await host._handle_profile_event('writer', 'cron.completed', request)
    await asyncio.gather(*tuple(host._background_tasks))
    assert len(sent) == 2
    assert {x['device'] for x in sent} == {'ios-device', 'android-device'}
    profile = profiles.describe_profile('writer')
    for message in sent:
        assert message['body'] == 'Result from writer'
        assert message['data']['profileHostId'] == host.host_id
        assert message['data']['profileBotId'] == profile.bot_id
        assert message['data']['profileName'] == 'writer'
        assert message['data']['jobId'] == job.id
        assert message['data']['runId'] == request['runId']
    assert sent[0]['data']['gatewayId'] == 'ios-gateway'
    assert sent[1]['data']['serverId'] == 'android-relay'
    assert not (profile.path / 'push_subs.json').exists()
    assert service.store_path.read_bytes() == before


@pytest.mark.asyncio
async def test_desktop_and_core_observation_and_host_restart_do_not_duplicate(host_setup):
    home, host, sent = host_setup
    _, _, request = await finished_run(home)
    replies = await asyncio.gather(*[
        host.dispatch('profiles.cron.notify', {**request, 'preview': 'Forged', 'serverId': 'Wrong'})
        for _ in range(4)
    ])
    assert len(sent) == 2
    assert sum(bool(x.get('duplicate')) for x in replies) == 3
    assert (await ProfileHost().dispatch('profiles.cron.notify', request))['duplicate']
    assert len(sent) == 2
    assert sent[0]['body'] != 'Forged'


@pytest.mark.asyncio
@pytest.mark.parametrize('options', [
    {'deliver': False}, {'response': '[SILENT]'},
    {'channel': 'telegram', 'to': 'external-chat'},
    {'channel': 'web', 'to': 'relay-conversation'},
])
async def test_existing_delivery_policy_is_preserved(host_setup, options):
    home, host, sent = host_setup
    _, _, request = await finished_run(home, **options)
    assert not (await host.dispatch('profiles.cron.notify', request))['sent']
    assert sent == []


@pytest.mark.asyncio
async def test_missing_expired_purged_and_forged_results_do_not_send(host_setup):
    home, host, sent = host_setup
    service, job, request = await finished_run(home)
    assert not (await host.dispatch('profiles.cron.notify', {**request, 'runId': 'missing'}))['sent']
    with pytest.raises(ProfileHostError):
        await host.dispatch('profiles.cron.notify', {**request, 'jobId': '../escape'})
    for body in service._job_output_dir(job.id).glob('*.md'):
        body.unlink()
    assert not (await host.dispatch('profiles.cron.notify', request))['sent']
    service.remove_job(job.id, purge=True)
    assert not (await host.dispatch('profiles.cron.notify', request))['sent']
    assert sent == []


@pytest.mark.asyncio
async def test_managed_profile_broadcasts_but_does_not_also_push_from_child(host_setup, monkeypatch):
    home, _, _ = host_setup
    service, job, request = await finished_run(home)
    notify = AsyncMock()
    monkeypatch.setattr(relay_push, 'notify_devices', notify)
    gateway = type('Gateway', (), {'broadcast_cron_event': AsyncMock()})()
    data = {**request, 'outputPersisted': True}
    await _publish_cron_lifecycle(service, gateway, 'cron.completed', data, managed_profile=True)
    await asyncio.sleep(0)
    notify.assert_not_called()
    gateway.broadcast_cron_event.assert_awaited_once()
    await _publish_cron_lifecycle(service, gateway, 'cron.completed', data)
    await asyncio.sleep(0)
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_profile_is_not_forwarded(host_setup):
    _, host, sent = host_setup
    result = await host.dispatch('profiles.cron.notify', {'name': 'default', 'jobId': 'job', 'runId': 'run'})
    assert not result['sent']
    assert sent == []


@pytest.mark.asyncio
async def test_retry_attempt_does_not_notify_but_terminal_failure_does(host_setup):
    home, host, sent = host_setup

    async def fail(job):
        raise RuntimeError('provider unavailable')

    service = CronService(home / 'profiles/writer/cron/jobs.json', on_job=fail)
    job = service.add_job('Report', CronSchedule(kind='at', at_ms=1), 'test', deliver=True)
    job.retry_max_attempts = 1
    service._save_store()
    await service._execute_job(job)
    request = {'name': 'writer', 'jobId': job.id, 'runId': job.state.last_run_id}
    assert not (await host.dispatch('profiles.cron.notify', request))['sent']
    assert sent == []
    await service._execute_job(job)
    assert job.state.retry_attempt == 0
    assert (await host.dispatch('profiles.cron.notify', {**request, 'runId': job.state.last_run_id}))['sent']
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_no_devices_does_not_claim_or_mutate_profile_registry(host_setup):
    home, host, sent = host_setup
    service, job, request = await finished_run(home)
    registry = relay_push.get_push_registry()
    saved = registry.list()
    for sub in saved:
        registry.unregister(sub['pushId'])
    assert not (await host.dispatch('profiles.cron.notify', request))['sent']
    assert list(service._job_output_dir(job.id).glob('*.mobile-push')) == []
    for sub in saved:
        registry.register(push_id=sub['pushId'], push_secret='test')
    assert (await host.dispatch('profiles.cron.notify', request))['sent']
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_desktop_rpc_reaches_host_dispatch_without_exposing_push_credentials(host_setup):
    home, _, sent = host_setup
    _, _, request = await finished_run(home)
    server = GatewayServer(host='127.0.0.1', enable_profile_host=True)
    server._ws_rpc_reply = AsyncMock()
    server._ws_rpc_error = AsyncMock()
    socket = object()
    await server._handle_ws_rpc(socket, 'desktop', {
        'id': 'completion-1', 'method': 'profiles.cron.notify', 'params': request,
    })
    server._ws_rpc_error.assert_not_awaited()
    server._ws_rpc_reply.assert_awaited_once_with(socket, 'completion-1', {'ok': True, 'sent': True})
    assert len(sent) == 2
    assert all('pushSecret' not in message['data'] for message in sent)
