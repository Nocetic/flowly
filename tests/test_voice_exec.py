"""Voice command execution: one durable execution per provider call, the
ordinary exec policy and approvals, and bounded, redacted receipts."""
import asyncio
import sys

import pytest

from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.shell import SecureExecTool
from flowly.exec import ExecConfig
from flowly.exec.approval_manager import get_approval_manager
from flowly.exec.approvals import ExecApprovalStore
from flowly.live_voice.exec import MODEL_OUTPUT_CHARS, STORED_OUTPUT_CHARS, VoiceExec, bound, classify
from flowly.live_voice.service import LiveVoiceService
from flowly.live_voice.sessions import MAX_TOOL_RECORDS, VoiceError, VoiceSessions
from flowly.session.manager import SessionManager

CONVERSATION = 'conversation-1'


@pytest.fixture
def voice(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    sessions = VoiceSessions(SessionManager(tmp_path / 'workspace'))
    sessions.open({'conversationId': CONVERSATION, 'connectionId': 'connection-1', 'language': 'tr'},
                  profile='default', bot_id='agent-1')
    return sessions


def call(**changes):
    return {'conversationId': CONVERSATION, 'connectionId': 'connection-1', 'commandId': 'a' * 64, **changes}


ARGS = {'command': 'git status', 'timeout': 60}


class Recorder:
    def __init__(self, result='On branch main', gate: asyncio.Event | None = None):
        self.calls = []
        self.result = result
        self.gate = gate

    async def __call__(self, params, session_key):
        self.calls.append((params, session_key))
        if self.gate is not None:
            await self.gate.wait()
        return self.result


def test_tool_record_is_reserved_once_and_returned_with_history(voice):
    record, created = voice.begin_tool(call(anchorMessageId='connection-1:message-1'), name='exec', arguments=ARGS)
    again, created_again = voice.begin_tool(call(anchorMessageId='connection-1:message-1'), name='exec', arguments=ARGS)
    assert created and not created_again
    assert again == record and record['status'] == 'running'
    history = voice.history({'conversationId': CONVERSATION})
    assert [row['id'] for row in history['tools']] == ['a' * 64]
    assert history['tools'][0]['anchorMessageId'] == 'connection-1:message-1'


def test_command_identity_cannot_be_reused_for_other_arguments(voice):
    voice.begin_tool(call(), name='exec', arguments=ARGS)
    with pytest.raises(VoiceError) as error:
        voice.begin_tool(call(), name='exec', arguments={**ARGS, 'command': 'rm -rf build'})
    assert error.value.code == 'CONFLICT'


def test_ended_connection_cannot_start_a_command_but_can_read_its_receipt(voice):
    voice.begin_tool(call(), name='exec', arguments=ARGS)
    voice.end({'conversationId': CONVERSATION, 'connectionId': 'connection-1'})
    _, created = voice.begin_tool(call(), name='exec', arguments=ARGS)
    assert not created
    with pytest.raises(VoiceError) as error:
        voice.begin_tool(call(commandId='b' * 64), name='exec', arguments=ARGS)
    assert error.value.code == 'STALE_CONNECTION'


def test_records_are_bounded_and_settle_once(voice):
    voice.begin_tool(call(), name='exec', arguments=ARGS)
    first = voice.finish_tool(CONVERSATION, 'a' * 64, status='completed', output='ok', exit_code=0)
    second = voice.finish_tool(CONVERSATION, 'a' * 64, status='failed', output='late')
    assert first['status'] == second['status'] == 'completed' and second['output'] == 'ok'
    for index in range(1, MAX_TOOL_RECORDS):
        voice.begin_tool(call(commandId=f'c{index}'), name='exec', arguments=ARGS)
    with pytest.raises(VoiceError) as error:
        voice.begin_tool(call(commandId='overflow'), name='exec', arguments=ARGS)
    assert error.value.code == 'LIMIT'


def test_invalid_anchor_does_not_create_a_record(voice):
    with pytest.raises(VoiceError):
        voice.begin_tool(call(anchorMessageId='../escape'), name='exec', arguments=ARGS)
    assert voice.history({'conversationId': CONVERSATION})['tools'] == []


def test_deleting_the_conversation_removes_tool_records(voice):
    voice.begin_tool(call(), name='exec', arguments=ARGS)
    voice.end({'conversationId': CONVERSATION, 'connectionId': 'connection-1'})
    assert voice.delete({'conversationId': CONVERSATION, 'ifEmpty': True})['deleted'] is False
    assert voice.delete({'conversationId': CONVERSATION})['deleted'] is True


@pytest.mark.parametrize('result,status,exit_code', [
    ('On branch main', 'completed', 0),
    ('tests failed\n\nExit code: 1', 'completed', 1),
    ('no matches\n\nExit code: 1 (no matches found — this is normal for search commands, not an error)', 'completed', 1),
    ('❌ Command denied: Approval denied or timed out', 'denied', None),
    ('[blocked: policy]', 'denied', None),
    ('⏰ Command timed out after 60 seconds', 'timed_out', None),
    ("Error: Tool 'exec' is unavailable", 'failed', None),
    ('❌ Error: spawn failed', 'failed', None),
])
def test_exec_results_map_to_record_statuses(result, status, exit_code):
    assert classify(result) == (status, exit_code)


def test_long_output_keeps_its_start_and_end():
    text = 'start\n' + 'x' * 50_000 + '\nfinal error'
    bounded, truncated = bound(text, 1000)
    assert truncated and len(bounded) == 1000
    assert bounded.startswith('start') and bounded.endswith('final error')


@pytest.mark.asyncio
async def test_concurrent_retries_execute_the_command_once(voice):
    gate = asyncio.Event()
    execute = Recorder(gate=gate)
    runner = VoiceExec(execute, lambda: 'default')
    record, created = voice.begin_tool(call(), name='exec', arguments=ARGS)
    first = asyncio.create_task(runner.run(voice, CONVERSATION, record, created))
    await asyncio.sleep(0)
    retry, retry_created = voice.begin_tool(call(), name='exec', arguments=ARGS)
    second = asyncio.create_task(runner.run(voice, CONVERSATION, retry, retry_created))
    await asyncio.sleep(0)
    assert runner.running(CONVERSATION, 'a' * 64)
    gate.set()
    results = await asyncio.gather(first, second)
    assert len(execute.calls) == 1
    assert execute.calls[0] == ({'command': 'git status', 'timeout': 60}, f'desktop:voice:{CONVERSATION}')
    assert [result['status'] for result in results] == ['completed', 'completed']
    replay = await runner.run(voice, CONVERSATION, *voice.begin_tool(call(), name='exec', arguments=ARGS))
    assert replay['replayed'] and replay['output'] == 'On branch main' and len(execute.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_request_does_not_cancel_the_command(voice):
    gate = asyncio.Event()
    execute = Recorder(gate=gate)
    runner = VoiceExec(execute, lambda: 'default')
    request = asyncio.create_task(runner.run(voice, CONVERSATION, *voice.begin_tool(call(), name='exec', arguments=ARGS)))
    await asyncio.sleep(0)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert voice.history({'conversationId': CONVERSATION})['tools'][0]['status'] == 'completed'


@pytest.mark.asyncio
async def test_a_record_lost_by_a_restart_is_interrupted_not_rerun(voice):
    voice.begin_tool(call(), name='exec', arguments=ARGS)
    execute = Recorder()
    restarted = VoiceExec(execute, lambda: 'default')
    result = await restarted.run(voice, CONVERSATION, *voice.begin_tool(call(), name='exec', arguments=ARGS))
    assert result['status'] == 'interrupted' and execute.calls == []


@pytest.mark.asyncio
async def test_output_is_redacted_and_bounded_for_model_and_history(voice):
    secret = 'sk-' + 'A' * 40
    execute = Recorder(result=f'token={secret}\n' + 'y' * (MODEL_OUTPUT_CHARS * 2))
    runner = VoiceExec(execute, lambda: 'default')
    result = await runner.run(voice, CONVERSATION, *voice.begin_tool(call(), name='exec', arguments=ARGS))
    stored = voice.history({'conversationId': CONVERSATION})['tools'][0]
    assert secret not in result['output'] and secret not in stored['output']
    assert len(result['output']) == MODEL_OUTPUT_CHARS and result['truncated']
    assert len(stored['output']) == STORED_OUTPUT_CHARS and stored['truncated']


@pytest.mark.asyncio
async def test_failing_executor_settles_the_record(voice):
    async def broken(params, session_key):
        raise RuntimeError('boom')

    result = await VoiceExec(broken, lambda: 'default').run(
        voice, CONVERSATION, *voice.begin_tool(call(), name='exec', arguments=ARGS))
    assert result['status'] == 'failed' and 'boom' not in result['output']


def service(voice, runner):
    return LiveVoiceService(voice, lambda: (None, None), executor=lambda: runner)


def exec_params(bot_id, **changes):
    return {**call(), 'profile': 'default', 'botId': bot_id, 'command': 'git status', **changes}


@pytest.fixture
def default_bot(monkeypatch):
    from flowly.live_voice import service as service_module

    monkeypatch.setattr(service_module, 'profile_exists', lambda name: name in {'default', 'creative'})
    monkeypatch.setattr(service_module, 'ensure_profile_bot_id',
                        lambda name: type('Info', (), {'bot_id': 'agent-1' if name == 'default' else 'agent-2'})())
    monkeypatch.setattr(service_module, 'validate_profile_name', lambda name: None)


@pytest.mark.asyncio
async def test_service_runs_commands_only_for_the_conversation_agent(voice, default_bot):
    execute = Recorder()
    voice_service = service(voice, VoiceExec(execute, lambda: 'default'))
    result = await voice_service.call('voice.exec', exec_params('agent-1'))
    assert result['status'] == 'completed' and len(execute.calls) == 1
    with pytest.raises(VoiceError) as error:
        await voice_service.call('voice.exec', exec_params('agent-2', profile='creative', commandId='b' * 64))
    assert error.value.code == 'TARGET_CONFLICT'
    assert len(execute.calls) == 1


@pytest.mark.asyncio
async def test_named_profile_conversations_are_explicitly_unsupported(tmp_path, monkeypatch, default_bot):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    sessions = VoiceSessions(SessionManager(tmp_path / 'workspace'))
    sessions.open({'conversationId': CONVERSATION, 'connectionId': 'connection-1', 'language': 'tr'},
                  profile='creative', bot_id='agent-2')
    execute = Recorder()
    with pytest.raises(VoiceError) as error:
        await service(sessions, VoiceExec(execute, lambda: 'default')).call(
            'voice.exec', exec_params('agent-2', profile='creative'))
    assert error.value.code == 'UNSUPPORTED_TARGET'
    assert execute.calls == [] and sessions.history({'conversationId': CONVERSATION})['tools'] == []


@pytest.mark.asyncio
async def test_service_rejects_invalid_arguments_before_reserving(voice, default_bot):
    voice_service = service(voice, VoiceExec(Recorder(), lambda: 'default'))
    for bad in ({'command': ''}, {'command': 'x' * 8001}, {'timeout': 0}, {'timeout': 121}, {'workingDir': ''}):
        with pytest.raises(VoiceError):
            await voice_service.call('voice.exec', exec_params('agent-1', **bad))
    assert voice.history({'conversationId': CONVERSATION})['tools'] == []


@pytest.mark.asyncio
async def test_unavailable_without_an_executor(voice, default_bot):
    with pytest.raises(VoiceError) as error:
        await LiveVoiceService(voice, lambda: (None, None)).call('voice.exec', exec_params('agent-1'))
    assert error.value.code == 'UNAVAILABLE'


def test_history_reports_records_without_a_live_execution_as_interrupted(voice):
    voice.begin_tool(call(), name='exec', arguments=ARGS)
    runner = VoiceExec(Recorder(), lambda: 'default')
    tools = service(voice, runner).call('voice.history', {'conversationId': CONVERSATION})['tools']
    assert tools[0]['status'] == 'interrupted'


@pytest.mark.skipif(sys.platform == 'win32', reason='uses /bin/sh subprocess')
@pytest.mark.asyncio
@pytest.mark.parametrize('decision,expected', [('allow-once', 'completed'), ('deny', 'denied')])
async def test_restricted_policy_asks_on_the_voice_session_like_normal_chat(voice, tmp_path, decision, expected):
    store = ExecApprovalStore()
    policy = store.load()
    policy.security, policy.ask = 'full', 'always'
    store.save()
    manager = get_approval_manager()
    tool = SecureExecTool(ExecConfig(security='full'), approval_callback=manager.request_and_wait,
                          working_dir=str(tmp_path))
    registry = ToolRegistry()
    registry.register(tool)
    requested = []

    async def notify(pending):
        requested.append(pending)
        manager.resolve(pending.id, decision)

    manager.add_notify_callback(notify)
    try:
        async def execute(params, session_key):
            return await registry.execute('exec', {**params, 'session_key': session_key}, session_key=session_key)

        result = await VoiceExec(execute, lambda: 'default').run(
            voice, CONVERSATION, *voice.begin_tool(call(), name='exec', arguments={'command': 'echo voice-ok', 'timeout': 60}))
    finally:
        manager._notify_callbacks.remove(notify)
    assert [pending.session_key for pending in requested] == [f'desktop:voice:{CONVERSATION}']
    assert requested[0].request.command == 'echo voice-ok'
    assert result['status'] == expected
    assert ('voice-ok' in result['output']) is (decision == 'allow-once')


@pytest.mark.skipif(sys.platform == 'win32', reason='uses /bin/sh subprocess')
@pytest.mark.asyncio
async def test_history_returns_this_conversations_pending_approval_for_catch_up(voice, tmp_path):
    store = ExecApprovalStore()
    policy = store.load()
    policy.security, policy.ask = 'full', 'always'
    store.save()
    manager = get_approval_manager()
    tool = SecureExecTool(ExecConfig(security='full'), approval_callback=manager.request_and_wait, working_dir=str(tmp_path))
    registry = ToolRegistry()
    registry.register(tool)

    async def execute(params, session_key):
        return await registry.execute('exec', {**params, 'session_key': session_key}, session_key=session_key)

    runner = VoiceExec(execute, lambda: 'default')
    voice_service = service(voice, runner)
    run = asyncio.create_task(runner.run(voice, CONVERSATION, *voice.begin_tool(
        call(), name='exec', arguments={'command': 'echo later', 'timeout': 60})))
    for _ in range(50):
        pending = voice_service.call('voice.history', {'conversationId': CONVERSATION})['pendingApprovals']
        if pending:
            break
        await asyncio.sleep(0.01)
    assert [row['sessionKey'] for row in pending] == [f'desktop:voice:{CONVERSATION}']
    assert pending[0]['command'] == 'echo later'
    assert voice_service.call('voice.history', {'conversationId': CONVERSATION, 'offset': 1})['pendingApprovals'] == []
    manager.resolve(pending[0]['id'], 'deny')
    assert (await run)['status'] == 'denied'
    assert voice_service.call('voice.history', {'conversationId': CONVERSATION})['pendingApprovals'] == []
