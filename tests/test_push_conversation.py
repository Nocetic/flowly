"""A tap on an approval, question or plan opens the conversation that asked.

The phone apps already route any push carrying ``conversationId`` (with the
agent's ``serverId`` or ``gatewayId``) to that chat, and restore the waiting
request there. The id must be the one the phone opens the chat by.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from flowly.push import approval_push, relay_push


@pytest.mark.parametrize('session_key,conversation', [
    ('web:6b1f0c', '6b1f0c'),          # relay chat: the bare id, like the relay's chat pushes
    ('3f2a9c', '3f2a9c'),              # gateway chat started on iOS: its own key
    ('android:9d1e', 'android:9d1e'),  # agent-stored chat started on Android
    ('ios:chat-1', 'ios:chat-1'),
    ('desktop:4c2b', ''),              # a computer's session: nothing on the phone
    ('telegram:12345', ''),
    ('cron:job-1', ''),
    ('', ''),
    (None, ''),
])
def test_only_a_chat_the_phone_has_is_named(session_key, conversation):
    assert approval_push.phone_conversation(session_key) == conversation


@dataclass
class _Request:
    command: str = 'git push'


@dataclass
class _Approval:
    session_key: str | None
    id: str = 'a1'
    kind: str = 'exec'
    request: _Request = field(default_factory=_Request)


@dataclass
class _Question:
    session_key: str | None
    id: str = 'q1'
    question: str = 'Which week?'
    choices: list | None = None


@dataclass
class _PlanApproval:
    id: str = 'pa1'


@dataclass
class _Plan:
    sessionKey: str
    title: str = 'Move the blog'
    steps: list = field(default_factory=lambda: [1, 2])


@pytest.fixture
def phones(monkeypatch):
    sent: list[dict] = []

    async def push(title, body, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(relay_push, 'notify_devices', push)
    return sent


@pytest.mark.asyncio
async def test_each_waiting_request_carries_its_conversation(phones):
    from flowly.push import notifications

    await notifications.deliver(approval_push.approval_notice(_Approval('web:conv-a')))
    await notifications.deliver(approval_push.question_notice(_Question('ios:conv-b')))
    await notifications.deliver(approval_push.plan_notice(_PlanApproval(), 'plan-1', lookup=lambda _id: _Plan('conv-c')))
    assert [sent['conversation_id'] for sent in phones] == ['conv-a', 'ios:conv-b', 'conv-c']


@pytest.mark.asyncio
async def test_a_request_from_a_computer_session_still_notifies_without_one(phones):
    from flowly.push import notifications

    await notifications.deliver(approval_push.approval_notice(_Approval('desktop:4c2b')))
    assert phones[0]['conversation_id'] == ''
    assert phones[0]['data']['id'] == 'a1'
