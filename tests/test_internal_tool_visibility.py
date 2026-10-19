"""The agent's own bookkeeping tools never show on a product surface.

``objective`` and ``follow_up`` calls (and their results) are kept out of the
chat's tool panel, the history a reopened chat renders, the relay copy, the
live tool progress and the Activity journal. Every other tool shows as before.
"""

from __future__ import annotations

import pytest

from flowly.activity.recorder import ActivityRecorder
from flowly.agent.tool_progress import ToolProgress
from flowly.tool_activity import INTERNAL_TOOLS, project_tool_messages_for_ui


def _call(call_id: str, name: str, arguments: str = "{}") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _deferred(call_id: str, name: str) -> dict:
    return _call(call_id, "tool_call", '{"name": "%s", "arguments": {}}' % name)


def test_the_internal_tools_are_bookkeeping_and_memory():
    assert {"objective", "follow_up"} <= INTERNAL_TOOLS
    assert {"memory_search", "memory_get", "memory_recall", "memory_append", "knowledge_graph"} <= INTERNAL_TOOLS
    assert not {"web_search", "exec", "cron", "session_search"} & INTERNAL_TOOLS


def test_a_turn_shows_its_work_but_not_its_bookkeeping():
    messages = [
        {"role": "user", "content": "plan my week"},
        {"role": "assistant", "content": "", "tool_calls": [_call("a", "web_search"), _call("b", "objective")]},
        {"role": "tool", "tool_call_id": "a", "name": "web_search", "content": "results"},
        {"role": "tool", "tool_call_id": "b", "name": "objective", "content": "Tracking goal obj_1"},
        {"role": "assistant", "content": "", "tool_calls": [_call("c", "follow_up")]},
        {"role": "tool", "tool_call_id": "c", "name": "follow_up", "content": "Follow-up wk_1 planned"},
        {"role": "assistant", "content": "Here is your plan.", "tool_calls": [_deferred("d", "follow_up")]},
        {"role": "tool", "tool_call_id": "d", "name": "tool_call", "content": "Follow-up wk_2 planned"},
        {"role": "assistant", "content": "Done."},
    ]
    shown = project_tool_messages_for_ui(messages)
    assert [m["role"] for m in shown] == ["user", "assistant", "tool", "assistant", "assistant"]
    assert [c["function"]["name"] for c in shown[1]["tool_calls"]] == ["web_search"]
    assert shown[2]["name"] == "web_search"
    assert shown[3] == {"role": "assistant", "content": "Here is your plan."}  # said something: kept, no step
    assert "obj_1" not in str(shown) and "wk_" not in str(shown)
    # The canonical history is untouched: the provider still sees every call.
    assert len(messages[1]["tool_calls"]) == 2


def test_other_tools_and_empty_call_lists_are_unchanged():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "assistant", "content": "", "tool_calls": [_call("a", "cron")]},
        {"role": "tool", "tool_call_id": "a", "name": "cron", "content": "ok"},
    ]
    assert project_tool_messages_for_ui(messages) == messages


def test_live_events_drop_a_result_that_arrives_after_its_call():
    seen: set[str] = set()
    assert project_tool_messages_for_ui(
        [{"role": "assistant", "content": "", "tool_calls": [_deferred("x", "objective")]}], internal_call_ids=seen,
    ) == []
    assert seen == {"x"}
    assert project_tool_messages_for_ui(
        [{"role": "tool", "tool_call_id": "x", "name": "tool_call", "content": "Logged"}], internal_call_ids=seen,
    ) == []


@pytest.mark.asyncio
async def test_live_tool_progress_never_announces_them():
    events: list[dict] = []

    async def publish(event):
        events.append(event)

    progress = ToolProgress("run", publish)
    await progress.announce(0, [_call("a", "follow_up"), _call("b", "exec", '{"command":"pwd"}')])
    await progress.start("a", "follow_up", {"action": "schedule"})
    await progress.result("a", "Follow-up wk_1 planned", failed=False)
    assert [e["call"]["name"] for e in events] == ["exec"]


def test_activity_shows_them_as_steps(tmp_path, monkeypatch):
    """Hidden in the conversation, shown in Activity: there the owner looks to
    see what the agent did, its goal keeping and memory included."""
    from flowly.activity import store

    monkeypatch.setattr(store, "append", lambda *a, **k: None)
    recorder = ActivityRecorder()
    task = recorder.begin(session_key="desktop:chat-1", task_id="t1", trigger={"kind": "owner"}, request="plan")
    recorder.note_tool("desktop:chat-1", "objective", {"action": "create", "title": "Physics daily"}, ok=True, duration_ms=5)
    recorder.note_tool("desktop:chat-1", "memory_append", {"text": "likes rockets"}, ok=True, duration_ms=5)
    recorder.note_tool("desktop:chat-1", "follow_up", {"action": "schedule", "note": "ask"}, ok=True, duration_ms=5)
    assert [(step["tool"], step["kind"], step["target"]) for step in task.steps] == [
        ("objective", "goal", "Physics daily"),
        ("memory_append", "memory", ""),
        ("follow_up", "goal", ""),
    ]


def test_starting_to_track_a_goal_is_work_planning_a_check_in_is_talking():
    from flowly.activity.steps import describe_step, is_work

    assert is_work(describe_step("objective", {"action": "create", "title": "Physics"}))
    assert not is_work(describe_step("follow_up", {"action": "schedule"}))
    assert not is_work(describe_step("proactivity", {"action": "set"}))
    assert not is_work(describe_step("memory_append", {}))


def test_memory_work_is_not_a_step_either():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_call("m", "memory_search"), _call("w", "web_search")]},
        {"role": "tool", "tool_call_id": "m", "name": "memory_search", "content": "Hakan prefers short answers"},
        {"role": "tool", "tool_call_id": "w", "name": "web_search", "content": "results"},
    ]
    shown = project_tool_messages_for_ui(messages)
    assert [c["function"]["name"] for c in shown[0]["tool_calls"]] == ["web_search"]
    assert [m.get("name") for m in shown[1:]] == ["web_search"]
