"""Acceptance must not steal the active turn's stream or claim delivery."""
import asyncio

import pytest

from flowly.agent import inflight
from flowly.agent.loop import AgentLoop
from flowly.bus.events import InboundMessage, OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.web import WebChannel
from flowly.config.schema import WebChannelConfig
from flowly.gateway.server import GatewayServer
from tests.test_chat_command_transport import Socket


@pytest.mark.asyncio
async def test_queued_voice_instruction_only_becomes_running_inside_the_session_lock():
    agent = object.__new__(AgentLoop)
    agent.goal_manager = None
    agent._goal_user_epochs = {}
    agent._session_turn_locks = {}
    releases = {key: asyncio.Event() for key in ('first', 'second')}
    started = {key: asyncio.Event() for key in releases}

    async def execute(msg):
        started[msg.content].set()
        await releases[msg.content].wait()
        return OutboundMessage(channel='desktop', chat_id='work', content=msg.content + ' done')

    agent._process_message_unlocked = execute

    async def run(session, message, run_id, _stream, _media, _voice, _iteration, _render, extra):
        result = await agent._process_message(InboundMessage(
            channel='desktop', chat_id='work', sender_id='user', content=message,
            metadata={'run_id': run_id, **extra},
        ))
        return result.content, {'stream_run_id': run_id}

    run.supports_turn_start = True
    server = GatewayServer(on_chat_message=run)
    socket = Socket()
    try:
        for name in ('first', 'second'):
            await server._ws_rpc_chat_send(socket, 'client', name, {
                'sessionKey': 'desktop:work', 'message': name, 'idempotencyKey': name,
                'queueForNextTurn': True,
            })
            if name == 'first':
                await asyncio.wait_for(started[name].wait(), 1)
        await asyncio.sleep(0)
        assert not started['second'].is_set()
        assert inflight.get('desktop:work')['runId'] == 'first'
        assert server.chat_commands.lookup('desktop:work', 'second')['status'] == 'accepted'
        releases['first'].set()
        await asyncio.wait_for(started['second'].wait(), 1)
        assert inflight.get('desktop:work')['runId'] == 'second'
        assert server.chat_commands.lookup('desktop:work', 'second')['status'] == 'running'
        releases['second'].set()
        await asyncio.gather(*list(server._active_tasks.values()))
        assert server.chat_commands.lookup('desktop:work', 'second')['status'] == 'completed'
    finally:
        for release in releases.values():
            release.set()
        await asyncio.gather(*list(server._active_tasks.values()), return_exceptions=True)
        inflight.finish('desktop:work', 'first')
        inflight.finish('desktop:work', 'second')


@pytest.mark.asyncio
async def test_older_runtime_rejects_next_turn_mode_before_acceptance():
    async def old_host(*_args):
        raise AssertionError('Unsupported host must not execute the instruction')
    server = GatewayServer(on_chat_message=old_host)
    socket = Socket()
    await server._ws_rpc_chat_send(socket, 'client', 'rpc', {
        'sessionKey': 'desktop:work', 'message': 'Instruction',
        'idempotencyKey': 'queued', 'queueForNextTurn': True,
    })
    assert socket.sent[-1]['error']['code'] == 'CHAT_QUEUE_UNAVAILABLE'
    assert server.chat_commands.lookup('desktop:work', 'queued') is None


@pytest.mark.asyncio
async def test_relay_acceptance_preserves_the_previous_stream_until_real_turn_start():
    from unittest.mock import AsyncMock

    channel = WebChannel(WebChannelConfig(enabled=True), MessageBus())
    channel.supports_turn_start = True
    channel.bus.publish_inbound = AsyncMock()
    channel._send_or_queue = AsyncMock()
    inflight.begin('desktop:work', 'previous', 'Current work', goal_run=False)
    socket = Socket()
    try:
        await channel._handle_rpc(socket, {
            'id': 'rpc', 'method': 'chat.send', 'sessionId': 'relay', 'params': {
                'sessionKey': 'desktop:work', 'message': 'New instruction',
                'idempotencyKey': 'queued', 'queueForNextTurn': True,
            },
        })
        await asyncio.gather(*list(channel._active_tasks.values()))
        assert channel.chat_commands.lookup('desktop:work', 'queued')['status'] == 'accepted'
        assert inflight.get('desktop:work')['runId'] == 'previous'
        message = channel.bus.publish_inbound.call_args.args[0]
        agent = object.__new__(AgentLoop)
        agent.goal_manager = None
        agent._goal_user_epochs = {}
        agent._session_turn_locks = {}
        response = OutboundMessage(channel='web', chat_id='relay', content='Result', metadata={
            'run_id': 'queued', 'stream_run_id': 'queued', 'session_key': 'desktop:work',
        })
        agent._process_message_unlocked = AsyncMock(return_value=response)
        await agent._process_message(message)
        assert channel.chat_commands.lookup('desktop:work', 'queued')['status'] == 'running'
        assert inflight.get('desktop:work')['runId'] == 'queued'
        assert inflight.get('desktop:work')['goalRun'] is False
        await message.metadata['on_iteration']({'runId': 'wrong', 'role': 'tool', 'content': 'Tool result'})
        assert inflight.get('desktop:work')['iterations'][0]['runId'] == 'queued'
        import json
        tool_event = json.loads(channel._send_or_queue.call_args.args[0])
        assert tool_event['data']['sessionKey'] == 'desktop:work'
        await channel.send(response)
        assert channel.chat_commands.lookup('desktop:work', 'queued')['status'] == 'completed'
    finally:
        inflight.finish('desktop:work', 'previous')
        inflight.finish('desktop:work', 'queued')
