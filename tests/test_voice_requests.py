import asyncio
import time
from types import SimpleNamespace

import pytest

import flowly.clarify.manager as clarify_module
import flowly.exec.approval_manager as approval_module
import flowly.profile as profiles
from flowly.agent.run_abort import CURRENT_RUN_ID, RunAbortController
from flowly.channels import feature_rpc
from flowly.clarify.types import ClarifyRequest
from flowly.exec.types import ExecRequest, PendingApproval
from flowly.live_voice.requests import pending_requests, respond
from flowly.live_voice.sessions import VoiceError


@pytest.fixture
def managers(monkeypatch):
    clarify = clarify_module.ClarifyManager()
    approval = approval_module.ApprovalManager()
    monkeypatch.setattr(clarify_module, '_manager', clarify)
    monkeypatch.setattr(approval_module, '_manager', approval)
    monkeypatch.setattr(profiles, 'ensure_profile_bot_id', lambda _: SimpleNamespace(bot_id='agent-1'))
    return {'clarify': clarify, 'approval': approval}


PARAMS = {'sessionKey': 'desktop:voice-work:c_report', 'runId': 'worker-1', 'expectedBotId': 'agent-1'}


async def start_request(managers, kind, *, run_id='worker-1', request_id='request-1'):
    now = time.time()
    kwargs = {'id': request_id, 'session_key': PARAMS['sessionKey'], 'created_at': now, 'expires_at': now + 30}
    pending = (ClarifyRequest(question='Which report?', choices=['Sales', 'Usage'], **kwargs) if kind == 'clarify'
               else PendingApproval(request=ExecRequest(command='Publish the report'), **kwargs))
    ready = asyncio.Event()

    async def notify(_):
        ready.set()

    manager = managers[kind]
    manager.add_notify_callback(notify)
    task = asyncio.create_task(RunAbortController().run_cancellable(run_id, lambda: manager.request_and_wait(pending)))
    await asyncio.wait_for(ready.wait(), 1)
    return task, pending


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['clarify', 'approval'])
async def test_request_revision_and_worker_scope_guard_resolution(managers, kind):
    task, pending = await start_request(managers, kind)
    assert pending.run_id == 'worker-1'
    assert CURRENT_RUN_ID.get() is None
    wire = pending_requests(PARAMS)['requests'][0]
    answer = {'answer': 'Sales'} if kind == 'clarify' else {'decision': 'allow-once'}
    params = {**PARAMS, 'requestId': wire['id'], 'requestRevision': wire['requestRevision'], 'requestType': kind, **answer}
    try:
        for wrong in ({'runId': 'previous-run'}, {'sessionKey': 'desktop:voice-work:c_other'}, {'requestRevision': 'old-revision'}):
            with pytest.raises(VoiceError) as error:
                respond({**params, **wrong})
            assert error.value.code == 'STALE_REQUEST'
            assert not task.done()
        result, _ = await feature_rpc.dispatch('chat.respond', params)
        assert result['status'] == 'applied'
        assert pending_requests(PARAMS)['requests'] == []  # Future resolved before its coroutine has unwound.
        with pytest.raises(VoiceError):
            respond(params)
        assert await task == ('Sales' if kind == 'clarify' else 'allow-once')
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_another_surface_answer_and_expiry_do_not_become_new_work(managers):
    task, pending = await start_request(managers, 'clarify')
    wire = pending_requests(PARAMS)['requests'][0]
    params = {**PARAMS, 'requestId': pending.id, 'requestRevision': wire['requestRevision'],
              'requestType': 'clarify', 'answer': 'Usage'}
    assert managers['clarify'].resolve(pending.id, 'Sales')
    with pytest.raises(VoiceError, match='already answered'):
        respond(params)
    assert await task == 'Sales'
    task, pending = await start_request(managers, 'clarify', request_id='expired')
    wire = pending_requests(PARAMS)['requests'][0]
    pending.expires_at = time.time() - 1
    assert not managers['clarify'].resolve(pending.id, 'Late answer')
    with pytest.raises(VoiceError):
        respond({**params, 'requestId': pending.id, 'requestRevision': wire['requestRevision']})
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_requests_keep_their_own_run_and_voice_cannot_grant_permanent_permission(managers):
    first, _ = await start_request(managers, 'clarify')
    second, _ = await start_request(managers, 'approval', run_id='worker-2', request_id='approval-2')
    try:
        assert [r['requestType'] for r in pending_requests(PARAMS)['requests']] == ['clarify']
        other = {**PARAMS, 'runId': 'worker-2'}
        wire = pending_requests(other)['requests'][0]
        with pytest.raises(VoiceError, match='allow-once or deny'):
            respond({**other, 'requestId': wire['id'], 'requestRevision': wire['requestRevision'],
                     'requestType': 'approval', 'decision': 'allow-always'})
        assert not second.done()
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
