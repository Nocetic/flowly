"""Questions and plan reviews reach the phone the way approvals do.

Before 2026-10-02 neither pushed at all: with the app closed a question or a
plan timed out unseen while the agent waited on it.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from flowly.clarify.manager import ClarifyManager
from flowly.clarify.types import ClarifyRequest
from flowly.plans.approval import PlanApprovalManager
from flowly.plans.models import PlanApproval
from flowly.push import approval_push, notifications, relay_push


@pytest.fixture
def phones(monkeypatch):
    sent: list[dict] = []

    async def push(title, body, **kwargs):
        sent.append({'title': title, 'body': body, **kwargs})

    monkeypatch.setattr(relay_push, 'notify_devices', push)
    monkeypatch.setattr(notifications, 'APPROVAL_PUSH_DELAY_SECONDS', 0.05)
    return sent


@pytest.fixture
def managers():
    questions, plans = ClarifyManager(), PlanApprovalManager()
    approval_push.wire_waiting_pushes(questions, plans)
    return questions, plans


def question(question_id: str) -> ClarifyRequest:
    now = time.time()
    return ClarifyRequest(id=question_id, question='Send the contract to ayse@example.com?',
                          choices=['Yes', 'No'], session_key='web:1', created_at=now, expires_at=now + 5)


def plan(approval_id: str) -> PlanApproval:
    now = time.time()
    return PlanApproval(id=approval_id, revision=1, createdAt=now, expiresAt=now + 5)


@pytest.mark.asyncio
async def test_an_unanswered_question_is_pushed_once_without_its_text(phones, managers):
    questions, _ = managers
    waiting = asyncio.create_task(questions.request_and_wait(question('q1')))
    while 'q1' not in questions._pending:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.15)
    assert questions.resolve('q1', 'Yes')
    assert await waiting == 'Yes'
    assert len(phones) == 1
    assert phones[0]['title'] == 'Question from Flowly'
    assert phones[0]['data'] == {'type': 'clarify', 'id': 'q1', 'eventKey': 'clarify:q1'}
    assert 'contract' not in repr(phones) and 'ayse' not in repr(phones)


@pytest.mark.asyncio
async def test_a_question_answered_within_the_minute_stays_off_the_phone(phones, managers):
    questions, _ = managers

    async def answer():
        while 'q2' not in questions._pending:
            await asyncio.sleep(0.005)
        assert questions.resolve('q2', 'Yes')

    asyncio.create_task(answer())
    assert await questions.request_and_wait(question('q2')) == 'Yes'
    await asyncio.sleep(0.1)
    assert phones == []


@pytest.mark.asyncio
async def test_an_unreviewed_plan_is_pushed_once(phones, managers):
    _, plans = managers
    waiting = asyncio.create_task(plans.request_and_wait(plan('pa1'), 'plan-7'))
    await asyncio.sleep(0.15)
    assert plans.resolve_future('pa1', 'approve')
    assert (await waiting).approved
    assert len(phones) == 1
    assert phones[0]['title'] == 'Plan ready for review'
    assert phones[0]['data'] == {'type': 'plan', 'id': 'pa1', 'planId': 'plan-7', 'eventKey': 'plan:pa1'}


@pytest.mark.asyncio
async def test_a_plan_decided_within_the_minute_stays_off_the_phone(phones, managers):
    _, plans = managers

    async def approve():
        await asyncio.sleep(0.01)
        assert plans.resolve_future('pa2', 'approve')

    asyncio.create_task(approve())
    assert (await plans.request_and_wait(plan('pa2'), 'plan-8')).approved
    await asyncio.sleep(0.1)
    assert phones == []


@pytest.mark.asyncio
async def test_a_failing_close_callback_never_breaks_the_plan_decision():
    plans = PlanApprovalManager()
    plans.add_close_callback(lambda approval_id, reason: 1 / 0)

    async def reject():
        await asyncio.sleep(0.01)
        plans.resolve_future('pa3', 'reject')

    asyncio.create_task(reject())
    assert (await plans.request_and_wait(plan('pa3'), 'plan-9')).decision == 'reject'


def test_the_gateway_wires_questions_and_plans_to_the_phone():
    import inspect

    from flowly.cli import gateway_cmd

    assert 'wire_waiting_pushes(_clarify_mgr, get_plan_approval_manager())' in inspect.getsource(gateway_cmd)
