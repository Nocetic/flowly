"""A voice command's approval card reaches, and leaves, every surface of its owner."""
import asyncio
import sys

import pytest

from flowly.agent.tools.registry import ToolRegistry
from flowly.agent.tools.shell import SecureExecTool
from flowly.exec import ExecConfig
from flowly.exec.approval_manager import ApprovalManager
from flowly.exec.approvals import ExecApprovalStore
from flowly.live_voice.authority import HOST_OWNER, RequestOwner, request_owner_scope
from flowly.live_voice.exec import VoiceExec
from flowly.live_voice.sessions import VoiceSessions
from tests.test_voice_event_recipients import Socket, rpc
from tests.test_voice_event_recipients import setup as recipients_setup  # noqa: F401

A, B = RequestOwner('event-account-a'), RequestOwner('event-account-b')
CONVERSATION = 'conversation-approval'


@pytest.mark.skipif(sys.platform == 'win32', reason='uses /bin/sh subprocess')
@pytest.mark.asyncio
async def test_settled_voice_approval_retires_the_card_on_every_owner_surface(recipients_setup, tmp_path):
    server, sessions, _, _, sockets, token, _ = recipients_setup
    strip, window = sockets[A], Socket()
    server._ws_clients['window'] = window
    for owner, socket in [(A, strip), (A, window), (B, sockets[B]), (HOST_OWNER, sockets[HOST_OWNER])]:
        await rpc(server, socket, token, owner)
        socket.sent.clear()
    voice = VoiceSessions(sessions).for_owner(A)
    with request_owner_scope(A):
        voice.open({'conversationId': CONVERSATION, 'connectionId': 'connection-1', 'language': 'tr'},
                   profile='default', bot_id='agent-1')
    store = ExecApprovalStore()
    policy = store.load()
    policy.security, policy.ask = 'full', 'always'
    store.save()
    manager = ApprovalManager()
    manager.add_notify_callback(server.broadcast_approval_request)
    manager.add_close_callback(server.broadcast_approval_closed)
    registry = ToolRegistry()
    registry.register(SecureExecTool(ExecConfig(security='full'), approval_callback=manager.request_and_wait,
                                     working_dir=str(tmp_path)))

    async def execute(params, session_key):
        return await registry.execute('exec', {**params, 'session_key': session_key}, session_key=session_key)

    with request_owner_scope(A):
        call = {'conversationId': CONVERSATION, 'connectionId': 'connection-1', 'commandId': 'b' * 64}
        run = asyncio.create_task(VoiceExec(execute, lambda: 'default').run(
            voice, CONVERSATION, *voice.begin_tool(call, name='exec', arguments={'command': 'echo ok', 'timeout': 60})))
    for _ in range(200):
        if manager.list_pending():
            break
        await asyncio.sleep(0.01)
    pending = manager.list_pending()[0]
    requested = lambda socket: [m for m in socket.sent if m.get('event') == 'exec.approval.requested']
    closed = lambda socket: [m for m in socket.sent if m.get('event') == 'exec.approval.closed']
    assert len(requested(strip)) == 1 and len(requested(window)) == 1
    from flowly.exec import approval_manager as approvals
    approvals._manager, previous = manager, getattr(approvals, '_manager', None)
    try:
        await rpc(server, strip, token, A, 'exec.approval.resolve', id=pending.id, decision='allow-once')
    finally:
        approvals._manager = previous
    replies = [m for m in strip.sent if m.get('id') == 'exec.approval.resolve']
    assert replies and replies[-1].get('result') == {'ok': True}, replies
    assert (await run)['status'] == 'completed'
    assert [m['data']['id'] for m in closed(strip)] == [pending.id]
    assert [m['data']['id'] for m in closed(window)] == [pending.id]
    assert sockets[B].sent == [] and sockets[HOST_OWNER].sent == []
