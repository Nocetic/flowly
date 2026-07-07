"""The dreamer must only learn from real user conversation — never from the
agent's own background runs (heartbeat/cron/subagent/system) or stale `.full`
display-mirror twins.
"""

from __future__ import annotations

import sqlite3

from flowly.agent.loop import _is_user_activity_channel
from flowly.memory.dreamer import SessionIndexDeltaSource, is_automation_session


def _make_index(tmp_path):
    path = tmp_path / "session_index.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_key TEXT, role TEXT, content TEXT, timestamp REAL
        );
        """
    )
    rows = [
        ("cli:tui-1", "user", "I prefer dark mode", 1.0),
        ("heartbeat:tick", "user", "Read HEARTBEAT.md and follow tasks", 2.0),
        ("cron:job_42", "user", "run the daily report", 3.0),
        ("subagent:run_9", "user", "internal subtask prompt", 4.0),
        ("system:announce", "assistant", "background announce", 5.0),
        ("web:abc", "user", "my name is Hakan", 6.0),
        ("heartbeat:tick.full", "user", "mirror twin noise", 7.0),
    ]
    conn.executemany(
        "INSERT INTO messages (session_key, role, content, timestamp) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def test_delta_excludes_automation_and_full_twins(tmp_path):
    src = SessionIndexDeltaSource(str(_make_index(tmp_path)))
    got = src.read_since(0, limit=100)
    keys = {r.session_key for r in got}
    assert keys == {"cli:tui-1", "web:abc"}, f"only user sessions, got {keys}"


def test_limit_applies_to_real_messages_not_noise(tmp_path):
    """A tight limit must return real user messages, not get consumed by the
    automation rows the filter discards."""
    src = SessionIndexDeltaSource(str(_make_index(tmp_path)))
    got = src.read_since(0, limit=2)
    keys = sorted(r.session_key for r in got)
    assert keys == ["cli:tui-1", "web:abc"]


def test_is_automation_session():
    assert is_automation_session("heartbeat:tick")
    assert is_automation_session("cron:job_1")
    assert is_automation_session("subagent:run_1")
    assert is_automation_session("system:x")
    assert is_automation_session("web:abc.full")
    assert not is_automation_session("cli:tui-1")
    assert not is_automation_session("web:abc")
    assert not is_automation_session("telegram:123")


def test_user_activity_channel_predicate():
    assert _is_user_activity_channel("cli")
    assert _is_user_activity_channel("web")
    assert _is_user_activity_channel("telegram")
    assert not _is_user_activity_channel("heartbeat")
    assert not _is_user_activity_channel("cron")
    assert not _is_user_activity_channel("system")
