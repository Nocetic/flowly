import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer


class Socket:
    closed = False

    def __init__(self, fail_ack=False):
        self.sent = []
        self.fail_ack = fail_ack

    async def send_json(self, frame):
        if self.fail_ack and "result" in frame:
            raise ConnectionError("Lost acknowledgement")
        self.sent.append(frame)

    async def send(self, frame):
        await self.send_json(json.loads(frame))


REQUEST = {"sessionKey": "desktop:voice-work", "idempotencyKey": "work-1", "message": "Prepare the report"}


@pytest.mark.asyncio
async def test_command_status_is_shared_scoped_and_does_not_execute():
    server = GatewayServer(on_chat_message=AsyncMock())
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    channel.set_chat_commands(server.chat_commands)
    server.chat_commands.accept(REQUEST['sessionKey'], 'work-1', REQUEST)
    server.chat_commands.settle(REQUEST['sessionKey'], 'work-1', 'aborted')
    params = {'sessionKey': REQUEST['sessionKey'], 'runId': 'work-1'}
    direct, relay = Socket(), Socket()
    await server._handle_ws_rpc(direct, 'client', {'id': 'rpc-1', 'method': 'chat.command', 'params': params})
    await channel._handle_rpc(relay, {'id': 'rpc-2', 'method': 'chat.command', 'params': params})
    assert direct.sent[-1]['result'] == relay.sent[-1]['result']
    assert direct.sent[-1]['result']['status'] == 'aborted'
    await channel._handle_rpc(relay, {'id': 'rpc-3', 'method': 'chat.command',
                                     'params': {**params, 'sessionKey': 'other-session'}})
    assert relay.sent[-1]['result']['status'] == 'not_found'
    server.on_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_target_is_rejected_before_acceptance_on_both_transports(monkeypatch):
    from types import SimpleNamespace

    import flowly.profile as profiles

    monkeypatch.setattr(profiles, 'ensure_profile_bot_id', lambda _name: SimpleNamespace(bot_id='current-agent'))
    server = GatewayServer(on_chat_message=AsyncMock())
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    params = {**REQUEST, 'expectedBotId': 'previous-agent'}
    direct, relay = Socket(), Socket()
    await server._ws_rpc_chat_send(direct, 'client', 'rpc-1', params)
    await channel._handle_rpc(relay, {'id': 'rpc-2', 'method': 'chat.send', 'params': params})
    assert direct.sent[-1]['error']['code'] == relay.sent[-1]['error']['code'] == 'TASK_TARGET_CHANGED'
    assert server.chat_commands.lookup(REQUEST['sessionKey'], 'work-1') is None
    assert channel.chat_commands.lookup(REQUEST['sessionKey'], 'work-1') is None


@pytest.mark.asyncio
async def test_gateway_retry_before_and_after_completion_runs_once():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(*args):
        calls.append(args[2])
        started.set()
        await release.wait()
        return "Report ready", {}

    server = GatewayServer(on_chat_message=run)
    first, second = Socket(), Socket()
    await server._ws_rpc_chat_send(first, "a", "rpc-1", REQUEST)
    await started.wait()
    await server._ws_rpc_chat_send(second, "b", "rpc-2", REQUEST)
    assert second.sent[-1]["result"]["replayed"] is True
    assert second.sent[-1]["result"]["status"] == "running"
    release.set()
    await asyncio.gather(*list(server._active_tasks.values()))
    await server._ws_rpc_chat_send(second, "b", "rpc-3", REQUEST)
    assert second.sent[-1]["result"]["status"] == "completed"
    assert calls == ["work-1"]
    await server._ws_rpc_chat_send(second, "b", "rpc-4", {**REQUEST, "message": "Different work"})
    assert second.sent[-1]["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_gateway_losing_ack_keeps_accepted_work():
    run = AsyncMock(return_value=("Report ready", {}))
    server = GatewayServer(on_chat_message=run)
    with pytest.raises(ConnectionError):
        await server._ws_rpc_chat_send(Socket(fail_ack=True), "a", "rpc-1", REQUEST)
    await asyncio.gather(*list(server._active_tasks.values()))
    retry = Socket()
    await server._ws_rpc_chat_send(retry, "b", "rpc-2", REQUEST)
    assert retry.sent[-1]["result"]["status"] == "completed"
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_relay_retry_after_bus_publish_and_terminal_is_not_a_new_turn():
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    channel = WebChannel(WebChannelConfig(enabled=True), bus)
    socket = Socket()
    request = {"type": "rpc", "method": "chat.send", "id": "rpc-1", "sessionId": "relay-1", "params": REQUEST}
    await channel._handle_rpc(socket, request)
    await asyncio.gather(*list(channel._active_tasks.values()))
    # The publish task has finished, but the actual agent turn is still running.
    await channel._handle_rpc(socket, {**request, "id": "rpc-2", "sessionId": "relay-2"})
    assert socket.sent[-1]["result"]["status"] == "running"
    assert bus.publish_inbound.await_count == 1
    channel._send_or_queue = AsyncMock()
    await channel.send(OutboundMessage(
        channel="web", chat_id="relay-2", content="Partial report",
        metadata={"run_id": "work-1", "session_key": REQUEST["sessionKey"], "aborted": True},
    ))
    await channel._handle_rpc(socket, {**request, "id": "rpc-3", "sessionId": "relay-2"})
    assert socket.sent[-1]["result"]["status"] == "aborted"
    assert bus.publish_inbound.await_count == 1


@pytest.mark.asyncio
async def test_gateway_to_relay_retry_uses_the_same_profile_receipt():
    run = AsyncMock(return_value=("Report ready", {}))
    server = GatewayServer(on_chat_message=run)
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    channel.set_chat_commands(server.chat_commands)
    channel.bus.publish_inbound = AsyncMock()
    await server._ws_rpc_chat_send(Socket(), "a", "rpc-1", REQUEST)
    await asyncio.gather(*list(server._active_tasks.values()))
    socket = Socket()
    await channel._handle_rpc(socket, {
        "type": "rpc", "method": "chat.send", "id": "rpc-2",
        "sessionId": "relay-1", "params": REQUEST,
    })
    assert socket.sent[-1]["result"]["status"] == "completed"
    assert channel.bus.publish_inbound.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
async def test_autonomous_receipt_is_durable_before_run_identity_is_announced(tmp_path, transport):
    from flowly.session.commands import ChatCommandStore

    commands = ChatCommandStore(tmp_path / 'commands.sqlite3', owner_id='current')
    reopened = ChatCommandStore(tmp_path / 'commands.sqlite3', owner_id='next-process')
    session_key = 'desktop:voice-work:task'
    run_ids = []

    def announced(run_id):
        run_ids.append(run_id)
        assert commands.lookup(session_key, run_id)['status'] == 'accepted'
        assert reopened.lookup(session_key, run_id)['status'] == 'status_unknown'

    if transport == 'gateway':
        surface = GatewayServer(on_chat_message=AsyncMock(return_value=('Finished', {})))
        surface._session_ws[session_key] = Socket()
        surface.chat_commands.close()
        surface._chat_commands = commands
    else:
        surface = WebChannel(WebChannelConfig(enabled=True), MessageBus())
        surface.set_chat_commands(commands)
        surface._session_key_to_relay_id[session_key] = 'relay-1'
        surface.bus.publish_inbound = AsyncMock()
    try:
        assert await surface.run_autonomous_turn(session_key, {
            '_goal_continuation_goal_id': 'goal-1', 'goal_run': True,
            'on_run_started': announced,
        })
        await asyncio.gather(*list(surface._active_tasks.values()))
        assert len(run_ids) == 1
        assert commands.lookup(session_key, run_ids[0]) is not None
        assert commands.lookup('another-session', run_ids[0]) is None
    finally:
        reopened.close()
        commands.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
async def test_autonomous_announcement_failure_settles_without_execution(transport):
    session_key = 'desktop:voice-work:task'
    run_ids = []

    def failed_announcement(run_id):
        run_ids.append(run_id)
        raise RuntimeError('test announcement failure')

    if transport == 'gateway':
        surface = GatewayServer(on_chat_message=AsyncMock())
        surface._session_ws[session_key] = Socket()
        execute = surface.on_chat_message
    else:
        surface = WebChannel(WebChannelConfig(enabled=True), MessageBus())
        surface._session_key_to_relay_id[session_key] = 'relay-1'
        surface.bus.publish_inbound = execute = AsyncMock()
    try:
        with pytest.raises(RuntimeError, match='test announcement failure'):
            await surface.run_autonomous_turn(session_key, {
                '_goal_continuation_goal_id': 'goal-1', 'goal_run': True,
                'on_run_started': failed_announcement,
            })
        assert surface.chat_commands.lookup(session_key, run_ids[0])['status'] == 'error'
        execute.assert_not_awaited()
        assert not surface._active_tasks
    finally:
        surface.chat_commands.close()


@pytest.mark.asyncio
async def test_superseded_gateway_goal_settles_without_a_false_final_event():
    async def skip(*_args):
        return '', {'_goal_turn_skipped': True}

    skip.supports_turn_start = True
    surface = GatewayServer(on_chat_message=skip)
    socket = Socket()
    session_key = 'desktop:voice-work:task'
    surface._session_ws[session_key] = socket
    run_ids = []
    try:
        assert await surface.run_autonomous_turn(session_key, {
            '_goal_continuation_goal_id': 'superseded', 'goal_run': True,
            'on_run_started': run_ids.append,
        })
        assert surface.chat_commands.lookup(session_key, run_ids[0])['status'] == 'aborted'
        assert not [frame for frame in socket.sent if frame.get('data', {}).get('state') == 'final']
    finally:
        surface.chat_commands.close()


@pytest.mark.asyncio
async def test_relay_publish_failure_is_durable_and_safe_for_the_client():
    bus = MessageBus()
    bus.publish_inbound = AsyncMock(side_effect=RuntimeError('private test internals'))
    channel = WebChannel(WebChannelConfig(enabled=True), bus)
    channel._send_or_queue = AsyncMock()
    channel._emit_local_event = AsyncMock()
    channel.supports_turn_start = True
    socket = Socket()
    await channel._handle_rpc(socket, {
        'type': 'rpc', 'method': 'chat.send', 'id': 'rpc-1', 'sessionId': 'relay-1', 'params': REQUEST,
    })
    await asyncio.gather(*list(channel._active_tasks.values()))
    await asyncio.sleep(0)
    assert channel.chat_commands.lookup(REQUEST['sessionKey'], 'work-1')['status'] == 'error'
    assert not channel._active_tasks
    sent = [json.loads(call.args[0]) for call in channel._send_or_queue.await_args_list]
    final = next(frame['data'] for frame in sent if frame.get('event') == 'chat')
    assert final['runId'] == 'work-1'
    assert final['sessionKey'] == REQUEST['sessionKey']
    assert final['failed'] is True
    assert 'private test internals' not in json.dumps(sent)
    await channel._handle_rpc(socket, {
        'type': 'rpc', 'method': 'chat.send', 'id': 'retry', 'sessionId': 'relay-1', 'params': REQUEST,
    })
    assert socket.sent[-1]['result']['status'] == 'error'
    assert bus.publish_inbound.await_count == 1


@pytest.mark.asyncio
async def test_relay_cancel_before_publish_does_not_leave_an_accepted_command():
    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    channel.supports_turn_start = True
    channel.bus.publish_inbound = AsyncMock()
    await channel._handle_rpc(Socket(), {
        'type': 'rpc', 'method': 'chat.send', 'id': 'rpc-1', 'sessionId': 'relay-1', 'params': REQUEST,
    })
    pending = list(channel._active_tasks.values())
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)
    assert channel.chat_commands.lookup(REQUEST['sessionKey'], 'work-1')['status'] == 'aborted'
    channel.bus.publish_inbound.assert_not_awaited()
    assert not channel._active_tasks


@pytest.mark.asyncio
async def test_gateway_cancel_before_turn_start_settles_the_accepted_command():
    run = AsyncMock()
    run.supports_turn_start = True
    server = GatewayServer(on_chat_message=run)
    await server._ws_rpc_chat_send(Socket(), 'client', 'rpc-1', REQUEST)
    tasks = list(server._active_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
    assert server.chat_commands.lookup(REQUEST['sessionKey'], 'work-1')['status'] == 'aborted'
    run.assert_not_awaited()
    assert not server._active_tasks


@pytest.mark.asyncio
async def test_gateway_shutdown_waits_for_accepted_turns_to_unwind():
    started, unwound = asyncio.Event(), asyncio.Event()

    async def run(*_args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            unwound.set()

    server = GatewayServer(on_chat_message=run)
    await server._ws_rpc_chat_send(Socket(), 'client', 'rpc-1', REQUEST)
    await started.wait()
    await server.stop()
    assert unwound.is_set()
    assert not server._active_tasks
    assert server.chat_commands.lookup(REQUEST['sessionKey'], 'work-1')['status'] == 'aborted'


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['gateway', 'relay'])
async def test_attachment_write_failure_has_a_durable_receipt_and_cannot_reexecute(monkeypatch, transport):
    from unittest.mock import Mock

    import flowly.channels.web as relay
    import flowly.gateway.server as gateway

    save = Mock(side_effect=OSError('private storage details'))
    params = {**REQUEST, 'attachments': [{'fileName': 'report.txt', 'content': 'eA=='}]}
    socket = Socket()
    if transport == 'gateway':
        monkeypatch.setattr(gateway, '_save_attachments', save)
        surface = GatewayServer(on_chat_message=AsyncMock())

        async def send():
            await surface._ws_rpc_chat_send(socket, 'client', 'rpc-1', params)
    else:
        monkeypatch.setattr(relay, '_save_attachments', save)
        surface = WebChannel(WebChannelConfig(enabled=True), MessageBus())
        surface.bus.publish_inbound = AsyncMock()

        async def send():
            await surface._handle_rpc(socket, {'type': 'rpc', 'method': 'chat.send', 'id': 'rpc-1',
                                              'sessionId': 'relay-1', 'params': params})
    await send()
    assert surface.chat_commands.lookup(REQUEST['sessionKey'], 'work-1')['status'] == 'error'
    assert socket.sent[-1]['error']['code'] == 'CHAT_INPUT_FAILED'
    assert 'private storage details' not in json.dumps(socket.sent)
    await send()
    assert socket.sent[-1]['result']['status'] == 'error'
    assert save.call_count == 1
    assert not surface._active_tasks


@pytest.mark.asyncio
async def test_relay_shutdown_cancels_only_messages_not_yet_published():
    from flowly.agent import inflight

    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    channel.bus.publish_inbound = AsyncMock()
    request = {'type': 'rpc', 'method': 'chat.send', 'id': 'rpc-1', 'sessionId': 'relay-1', 'params': REQUEST}
    await channel._handle_rpc(Socket(), request)
    await channel.stop()
    assert not channel._active_tasks
    assert channel.chat_commands.lookup(REQUEST['sessionKey'], 'work-1')['status'] == 'aborted'
    channel.bus.publish_inbound.assert_not_awaited()

    published = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    published.bus.publish_inbound = AsyncMock()
    await published._handle_rpc(Socket(), request)
    await asyncio.gather(*list(published._active_tasks.values()))
    await published.stop()
    assert published.chat_commands.lookup(REQUEST['sessionKey'], 'work-1')['status'] == 'running'
    assert published.bus.publish_inbound.await_count == 1
    inflight.finish(REQUEST['sessionKey'], 'work-1')
