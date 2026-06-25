"""End-to-end disk round-trip test for tool-protocol persistence.

The other tests in this directory cover the in-memory contract:
``add_message`` stores the right fields, ``get_history`` projects them
correctly. This file proves the full SessionManager save → load cycle
also preserves them, because the real-world failure mode is "saved to
disk, looks fine in memory, but next turn after a restart the LLM
sees nothing useful."

If this test breaks, multi-turn tool reasoning is broken regardless
of what the unit tests say — sessions on disk are the source of
truth across gateway restarts. We use a temp FLOWLY_HOME so the
test doesn't touch the user's real sessions.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from flowly.session.manager import Session, SessionManager


@pytest.fixture
def temp_flowly_home(tmp_path, monkeypatch):
    """Redirect FLOWLY_HOME so SessionManager writes into tmp_path."""
    home = tmp_path / "flowly-home"
    home.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(home))
    from flowly import profile
    if hasattr(profile, "_cached_home"):
        profile._cached_home = None
    return home


def _tc(call_id: str, name: str = "web_search", args: str = "{}") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def test_tool_structure_survives_disk_roundtrip(temp_flowly_home):
    """The full tool-protocol message sequence (assistant_with_tool_calls
    → tool_result → assistant_final) must survive save + load + a fresh
    SessionManager instance — simulating a gateway restart between
    turns. Without this, every restart would erase prior tool work
    from the LLM's perspective."""
    mgr = SessionManager(workspace=Path("/tmp"))
    session = mgr.get_or_create("web:disk-roundtrip-1")

    # Simulate one tool-using turn the way loop.py persists it.
    session.extend_with_turn_messages(
        user_content="search for X",
        new_messages=[
            {
                "role": "assistant",
                "content": "Let me look.",
                "tool_calls": [_tc("c1", "web_search", '{"query":"X"}')],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "web_search",
                "content": "result for X",
            },
            {"role": "assistant", "content": "Found it."},
        ],
        final_content="Found it.",
    )
    mgr.save(session)

    # Brand-new manager — simulates the next turn after a restart,
    # cache is empty, must load from disk.
    mgr2 = SessionManager(workspace=Path("/tmp"))
    resumed = mgr2.get_or_create("web:disk-roundtrip-1")

    # Stored messages match what we wrote.
    roles = [m["role"] for m in resumed.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]

    # tool_calls survived save + load.
    assert resumed.messages[1]["tool_calls"][0]["id"] == "c1"
    assert (
        resumed.messages[1]["tool_calls"][0]["function"]["name"]
        == "web_search"
    )

    # tool_call_id + name survived on the tool result.
    assert resumed.messages[2]["tool_call_id"] == "c1"
    assert resumed.messages[2]["name"] == "web_search"

    # get_history (what the LLM sees on next turn) projects correctly.
    h = resumed.get_history()
    assert len(h) == 4
    assert h[1]["tool_calls"][0]["id"] == "c1"
    assert h[2]["tool_call_id"] == "c1"
    assert h[2]["name"] == "web_search"
    # Internal timestamp field stripped from history.
    for m in h:
        assert "timestamp" not in m


def test_multi_turn_disk_roundtrip(temp_flowly_home):
    """Realistic two-turn conversation: user asks, agent uses two tools,
    replies. User asks follow-up. We save after turn 1, reload (simulate
    gateway restart), and verify turn 2's history contains turn 1's
    full tool structure — the LLM can answer 'what did you search?'
    only if this works."""
    mgr = SessionManager(workspace=Path("/tmp"))
    session = mgr.get_or_create("web:multi-turn-1")

    # Turn 1: user → agent uses 2 tools → reply
    session.extend_with_turn_messages(
        user_content="research foo and bar",
        new_messages=[
            {
                "role": "assistant",
                "content": "Searching both topics.",
                "tool_calls": [
                    _tc("a", "web_search", '{"query":"foo"}'),
                    _tc("b", "web_search", '{"query":"bar"}'),
                ],
            },
            {"role": "tool", "tool_call_id": "a", "name": "web_search", "content": "foo data"},
            {"role": "tool", "tool_call_id": "b", "name": "web_search", "content": "bar data"},
            {"role": "assistant", "content": "Here's what I found about foo and bar."},
        ],
        final_content="Here's what I found about foo and bar.",
    )
    mgr.save(session)

    # Simulate restart.
    mgr2 = SessionManager(workspace=Path("/tmp"))

    # Turn 2 begins: load session, fetch history.
    resumed = mgr2.get_or_create("web:multi-turn-1")
    history = resumed.get_history()

    # The LLM must see the two web_search calls and their results.
    search_calls = [
        m for m in history
        if m["role"] == "assistant" and m.get("tool_calls")
    ]
    assert len(search_calls) == 1
    issued_ids = {tc["id"] for tc in search_calls[0]["tool_calls"]}
    assert issued_ids == {"a", "b"}

    tool_results = [m for m in history if m["role"] == "tool"]
    assert len(tool_results) == 2
    result_ids = {m["tool_call_id"] for m in tool_results}
    assert result_ids == {"a", "b"}

    # Querying for specific content: model can recall what was searched.
    queries = [
        tc["function"]["arguments"]
        for tc in search_calls[0]["tool_calls"]
    ]
    assert '"query":"foo"' in queries[0] or '"query":"foo"' in queries[1]
    assert '"query":"bar"' in queries[0] or '"query":"bar"' in queries[1]


def test_crashed_mid_turn_session_repairs_on_reload(temp_flowly_home):
    """Pathological case: gateway crashed AFTER an assistant emitted
    tool_calls but BEFORE the tool results were written. The on-disk
    session ends mid-sequence. get_history's orphan repair must drop
    the unfinished bits so the next chat call doesn't 400 on a
    malformed protocol sequence.

    We construct the broken state directly (bypassing the helper that
    appends a capstone) — that's how a real crash would leave it."""
    mgr = SessionManager(workspace=Path("/tmp"))
    session = mgr.get_or_create("web:crashed-mid-turn")

    # Direct add_message to skip the capstone the helper would add.
    session.add_message("user", "do a thing")
    session.add_message(
        "assistant",
        "running it",
        tool_calls=[_tc("crash_x", "exec", '{"command":"sleep 9999"}')],
    )
    # Crash: tool result was never written. Save anyway.
    mgr.save(session)

    # Simulate restart.
    mgr2 = SessionManager(workspace=Path("/tmp"))
    resumed = mgr2.get_or_create("web:crashed-mid-turn")

    # The orphan assistant_with_tool_calls IS still in messages
    # (audit-log fidelity — we don't destroy disk evidence).
    roles_on_disk = [m["role"] for m in resumed.messages]
    assert roles_on_disk == ["user", "assistant"]

    # But get_history (what the LLM gets) is repaired:
    # the orphan assistant is trimmed so the next provider call works.
    h = resumed.get_history()
    assert len(h) == 1
    assert h[0] == {"role": "user", "content": "do a thing"}


def test_load_old_format_session_works(temp_flowly_home):
    """Backward compat: pre-Phase-1 sessions only have plain user +
    assistant text entries — no tool_calls, no tool messages. Loading
    such a session must work without errors, get_history must return
    a clean two-message list."""
    import json
    sessions_dir = Path(os.environ["FLOWLY_HOME"]) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Write the old format directly.
    path = sessions_dir / "web_legacy.jsonl"
    with path.open("w") as f:
        f.write(json.dumps({
            "_type": "metadata",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "metadata": {},
        }) + "\n")
        f.write(json.dumps({"role": "user", "content": "hi"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "hello"}) + "\n")

    mgr = SessionManager(workspace=Path("/tmp"))
    session = mgr.get_or_create("web:legacy")
    h = session.get_history()

    assert h == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_list_sessions_excludes_full_display_mirror(temp_flowly_home):
    """The ``<key>.full.jsonl`` display mirror shares the ``*.jsonl`` glob with
    the canonical session file; list_sessions must NOT surface it as a phantom
    ``<key>.full`` conversation (the duplicate that showed up mid-stream)."""
    import json
    sessions_dir = Path(os.environ["FLOWLY_HOME"]) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    meta = json.dumps({
        "_type": "metadata", "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00", "metadata": {"title": "Chat"},
    }) + "\n"
    (sessions_dir / "desktop_abc.jsonl").write_text(meta)
    (sessions_dir / "desktop_abc.full.jsonl").write_text(meta)  # mirror — ignore

    mgr = SessionManager(workspace=Path("/tmp"))
    keys = [s["key"] for s in mgr.list_sessions()]
    assert keys == ["desktop:abc"]
    assert not any(".full" in k for k in keys)


def test_iter_session_files_skips_full_mirror(tmp_path):
    """The single shared helper every consumer routes through: canonical files
    only, never the `.full.jsonl` mirrors."""
    from flowly.session.manager import iter_session_files
    (tmp_path / "a.jsonl").write_text("{}\n")
    (tmp_path / "a.full.jsonl").write_text("{}\n")
    (tmp_path / "b.jsonl").write_text("{}\n")
    names = sorted(p.name for p in iter_session_files(tmp_path))
    assert names == ["a.jsonl", "b.jsonl"]
