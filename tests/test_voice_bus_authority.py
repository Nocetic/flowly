"""Exercise actual bus consumers across account contexts and canonical changes."""
import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from flowly.agent.loop import AgentLoop, _AgentGoalDelivery
from flowly.bus.events import InboundMessage, OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.manager import ChannelManager
from flowly.goals.models import GoalState
from flowly.goals.store import GoalStore
from flowly.live_voice.authority import (
    HOST_OWNER,
    RequestOwner,
    current_request_owner,
    request_owner_scope,
)
from flowly.live_voice.bus_authority import bus_message_scope
from flowly.live_voice.events import EventAccess, current_event_access, event_access_scope
from flowly.session.manager import SessionManager

A, B = RequestOwner('account-a'), RequestOwner('account-b')
KEY = 'web:private-bus-session'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('FLOWLY_HOME', str(tmp_path / 'home'))
    sessions = SessionManager(tmp_path / 'workspace')
    with request_owner_scope(A):
        # Mark a normal channel key owned to test canonical ownership beyond
        # the reserved desktop:voice-work naming convention.
        session = sessions.get_or_create(KEY)
        session.metadata['voiceOwner'] = {'kind': 'account', 'uid': A.uid}
        sessions.save(session)
    return MessageBus(), sessions


@pytest.mark.asyncio
async def test_bus_queue_copies_source_authority_without_serializing_it(setup):
    bus, _ = setup
    message = InboundMessage('web', 'sender', 'private-bus-session', 'private')
    with request_owner_scope(A):
        await bus.publish_inbound(message)
    with request_owner_scope(B):
        queued = await bus.consume_inbound()
        assert queued is not message
        with bus_message_scope(queued) as allowed:
            assert allowed
            assert current_request_owner() == A
        assert current_request_owner() == B
    assert A.uid not in repr(asdict(queued))
    assert '_flowly_event_access' not in queued.metadata
    assert not hasattr(message, '_flowly_event_access')


@pytest.mark.asyncio
async def test_real_agent_turn_and_channel_dispatch_restore_original_queue_authority(setup):
    bus, _ = setup
    with request_owner_scope(A):
        await bus.publish_inbound(InboundMessage('web', 'sender', 'private-bus-session', 'private'))
    observed = []
    agent = object.__new__(AgentLoop)
    agent.bus = bus
    agent._notify_agent_state = AsyncMock()
    agent._goal_after_delivery = lambda *args, **kwargs: None

    async def process(message):
        observed.append(('agent', current_request_owner(), current_event_access().producer()))
        return OutboundMessage('web', message.chat_id, 'private response')

    agent._process_message = process
    with request_owner_scope(B):
        await agent._process_turn(await bus.consume_inbound())
        assert current_request_owner() == B
    arrived = asyncio.Event()

    class Channel:
        async def send(self, message):
            observed.append(('channel', current_request_owner(), current_event_access().producer()))
            arrived.set()

    manager = object.__new__(ChannelManager)
    manager.bus, manager.channels = bus, {'web': Channel()}
    with request_owner_scope(B):
        dispatch = asyncio.create_task(manager._dispatch_outbound())
    try:
        await asyncio.wait_for(arrived.wait(), 1)
    finally:
        dispatch.cancel()
        await dispatch
    assert observed == [('agent', A, A), ('channel', A, A)]


@pytest.mark.asyncio
async def test_recreated_session_does_not_execute_a_queued_old_owner_turn(setup):
    bus, sessions = setup
    with request_owner_scope(A):
        await bus.publish_inbound(InboundMessage('web', 'sender', 'private-bus-session', 'private'))
    queued = await bus.consume_inbound()
    path = queued._flowly_event_access.scopes[0].path
    path.write_text(path.read_text().replace(A.uid, B.uid))
    agent = object.__new__(AgentLoop)
    agent._process_scoped_turn = AsyncMock()
    with request_owner_scope(B):
        await agent._process_turn(queued)
    agent._process_scoped_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscriber_dispatch_scopes_each_message_without_inheriting_consumer_account(setup):
    bus, _ = setup
    with event_access_scope(EventAccess(owner=A)):
        await bus.publish_outbound(OutboundMessage('web', 'browser', 'private'))
    await bus.publish_outbound(OutboundMessage('web', 'browser', 'shared', metadata={
        '_flowly_event_access': {'owner': {'uid': A.uid}}}))
    observed = []
    done = asyncio.Event()

    async def callback(message):
        observed.append((message.content, current_request_owner()))
        if len(observed) == 2:
            bus.stop()
            done.set()

    bus.subscribe_outbound('web', callback)
    with request_owner_scope(B):
        task = asyncio.create_task(bus.dispatch_outbound())
    try:
        await asyncio.wait_for(done.wait(), 1)
        await asyncio.wait_for(task, 1)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert observed == [('private', A), ('shared', HOST_OWNER)]


@pytest.mark.asyncio
async def test_republishing_same_object_cannot_overwrite_previously_queued_authority(setup):
    bus, _ = setup
    message = OutboundMessage('web', 'browser', 'content')
    for owner in (A, B):
        with event_access_scope(EventAccess(owner=owner)):
            await bus.publish_outbound(message)
    for owner in (A, B):
        with bus_message_scope(await bus.consume_outbound()) as allowed:
            assert allowed
            assert current_request_owner() == owner


@pytest.mark.asyncio
async def test_goal_snapshot_uses_persisted_original_scope_across_background_delivery(setup):
    bus, sessions = setup
    store = GoalStore(sessions.sessions_dir.parent)
    with request_owner_scope(A):
        goal = store.save(GoalState(session_key=KEY, goal='private objective'))
    agent = object.__new__(AgentLoop)
    agent.bus = bus
    with request_owner_scope(B):
        await agent.publish_goal_snapshot(KEY, 'web', 'private-bus-session', 'progress', goal)
    queued = await bus.consume_outbound()
    with bus_message_scope(queued) as allowed:
        assert allowed
        assert current_request_owner() == A
        assert current_event_access().scopes[0] == goal._session_control_scope
    goal._session_control_scope.path.write_text(goal._session_control_scope.path.read_text().replace(A.uid, B.uid))
    await agent.publish_goal_snapshot(KEY, 'web', 'private-bus-session', 'stale progress', goal)
    assert bus.outbound.empty()


@pytest.mark.asyncio
async def test_unbound_private_history_is_not_attributed_to_current_canonical_owner(setup):
    bus, _ = setup
    await bus.publish_outbound(OutboundMessage('web', 'private-bus-session', 'unknown source', metadata={'session_key': KEY}))
    with bus_message_scope(await bus.consume_outbound()) as allowed:
        assert not allowed


@pytest.mark.asyncio
async def test_restarted_goal_continuation_restores_durable_owner_before_bus_acceptance(setup):
    from types import SimpleNamespace

    bus, sessions = setup
    store = GoalStore(sessions.sessions_dir.parent)
    with request_owner_scope(A):
        goal = store.save(GoalState(session_key=KEY, goal='private objective'))
    agent = object.__new__(AgentLoop)
    agent.bus = bus
    # Reopen the store, as a fresh process would, without carrying caller state.
    reopened = GoalStore(sessions.sessions_dir.parent)
    agent.goal_manager = SimpleNamespace(get=reopened.get)
    delivery = _AgentGoalDelivery(agent, session_key=KEY, channel='web', chat_id='browser-a', direct=False)
    with request_owner_scope(B):
        await delivery.run_continuation(session_key=KEY, goal_id=goal.goal_id, user_epoch=1, kickoff=False)
        assert current_request_owner() == B
    queued = await bus.consume_inbound()
    with bus_message_scope(queued) as allowed:
        assert allowed
        assert current_request_owner() == A
        assert queued.session_key == KEY
    # Neither a superseded goal generation nor a changed canonical owner can
    # create another continuation after the process has restarted.
    await delivery.run_continuation(session_key=KEY, goal_id='old-generation', user_epoch=1, kickoff=False)
    assert bus.inbound.empty()
    path = goal._session_control_scope.path
    path.write_text(path.read_text().replace(A.uid, B.uid))
    await delivery.run_continuation(session_key=KEY, goal_id=goal.goal_id, user_epoch=1, kickoff=False)
    assert bus.inbound.empty()
