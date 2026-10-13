"""The limits on an agent writing first (flowly/agenda/policy.py)."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from flowly.agenda import policy
from flowly.agenda.timeparse import WhenError, parse_when

IST = ZoneInfo("Europe/Istanbul")


def _ms(year, month, day, hour, minute=0, zone=IST) -> int:
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=zone).timestamp() * 1000)


NOON = _ms(2026, 10, 13, 12)


def _facts(**changes) -> policy.Facts:
    base = policy.Facts(
        now_ms=NOON, zone=IST, level="normal", quiet_start="22:00", quiet_end="09:00",
        unanswered=0, last_proactive_ms=None, sent_today_by_agent=0, sent_today_by_all=0,
        session_busy=False,
    )
    return replace(base, **changes)


def test_runs_when_nothing_holds_it_back():
    assert policy.decide(_facts()) == policy.Decision("run")


def test_off_never_writes_first():
    assert policy.decide(_facts(level="off")) == policy.Decision("drop", "proactivity_off")


def test_a_paused_or_closed_goal_is_not_checked_in_on():
    for status in ("paused", "done", "dropped", None):
        decision = policy.decide(_facts(is_objective=True, objective_status=status))
        assert decision == policy.Decision("drop", "goal_not_active")
    assert policy.decide(_facts(is_objective=True, objective_status="active")).action == "run"


def test_quiet_hours_wait_for_the_morning_overnight():
    late = _ms(2026, 10, 13, 23, 30)
    decision = policy.decide(_facts(now_ms=late))
    assert decision.action == "defer" and decision.reason == "quiet_hours"
    assert decision.until_ms == _ms(2026, 10, 14, 9)
    early = _ms(2026, 10, 14, 7, 15)
    assert policy.decide(_facts(now_ms=early)).until_ms == _ms(2026, 10, 14, 9)


def test_quiet_hours_within_one_day():
    afternoon = _ms(2026, 10, 13, 14)
    decision = policy.decide(_facts(now_ms=afternoon, quiet_start="13:00", quiet_end="15:30"))
    assert decision.until_ms == _ms(2026, 10, 13, 15, 30)
    assert policy.decide(_facts(now_ms=afternoon, quiet_start="10:00", quiet_end="10:00")).action == "run"


def test_each_unanswered_message_doubles_the_wait():
    sent = NOON - 6 * 3600 * 1000
    one = policy.decide(_facts(unanswered=1, last_proactive_ms=sent))
    assert one == policy.Decision("defer", "waiting_for_reply", sent + 12 * 3600 * 1000)
    two = policy.decide(_facts(unanswered=2, last_proactive_ms=sent))
    assert two == policy.Decision("defer", "waiting_for_reply", sent + 24 * 3600 * 1000)
    assert policy.decide(_facts(unanswered=2, last_proactive_ms=NOON - 25 * 3600 * 1000)).action == "run"
    later = policy.decide(_facts(unanswered=1, last_proactive_ms=NOON - 13 * 3600 * 1000))
    assert later.action == "run"


def test_three_unanswered_messages_stop_it_until_the_owner_writes():
    assert policy.decide(_facts(unanswered=3, last_proactive_ms=1)) == policy.Decision("drop", "owner_not_answering")


def test_each_level_has_its_daily_limit():
    for level, cap in (("low", 1), ("normal", 2), ("high", 4)):
        assert policy.decide(_facts(level=level, sent_today_by_agent=cap - 1)).action == "run"
        decision = policy.decide(_facts(level=level, sent_today_by_agent=cap))
        assert decision.reason == "agent_daily_limit"
        assert decision.until_ms == _ms(2026, 10, 14, 9)


def test_all_agents_share_one_daily_limit():
    decision = policy.decide(_facts(sent_today_by_all=policy.GLOBAL_DAILY_CAP))
    assert decision.reason == "daily_limit"


def test_a_busy_conversation_is_waited_for_briefly():
    decision = policy.decide(_facts(session_busy=True))
    assert decision == policy.Decision("defer", "conversation_busy", NOON + policy.BUSY_RETRY_MS)


def test_times_are_read_in_the_owner_timezone():
    now = dt.datetime(2026, 10, 13, 12, 0, tzinfo=IST)
    assert parse_when("+2h", now=now) == NOON + 2 * 3600 * 1000
    assert parse_when("tomorrow 09:00", now=now) == _ms(2026, 10, 14, 9)
    assert parse_when("tomorrow", now=now) == _ms(2026, 10, 14, 9)
    assert parse_when("20:00", now=now) == _ms(2026, 10, 13, 20)
    assert parse_when("11:00", now=now) == _ms(2026, 10, 14, 11)  # already past today
    assert parse_when("2026-10-20 18:30", now=now) == _ms(2026, 10, 20, 18, 30)
    assert parse_when("2026-10-20T18:30:00+00:00", now=now) == _ms(2026, 10, 20, 18, 30, dt.timezone.utc)
    assert parse_when("2026-10-20", now=now) == _ms(2026, 10, 20, 9)


@pytest.mark.parametrize("value", ["", "soon", "+0m", "today 08:00", "2026-01-01", "+100d", "25:00", None, True])
def test_unreadable_past_or_far_times_are_refused(value):
    now = dt.datetime(2026, 10, 13, 12, 0, tzinfo=IST)
    with pytest.raises(WhenError):
        parse_when(value, now=now)
