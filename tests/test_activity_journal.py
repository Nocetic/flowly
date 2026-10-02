"""A bot's activity: the work it does, recorded from the turn, summarized by its model.

See ``docs/engineering/activity-journal.md``.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

import flowly.activity.recorder as recorder_module
import flowly.profile as profiles
from flowly.activity import journal, recap, store
from flowly.activity.recorder import BOOT_ID, LONG_REPLY_CHARS, ActivityRecorder
from flowly.activity.steps import describe_step, is_work


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "flowly-home"
    path.mkdir()
    monkeypatch.setenv("FLOWLY_HOME", str(path))
    if hasattr(profiles, "_cached_home"):
        profiles._cached_home = None
    monkeypatch.setattr(store, "_cache", None)
    return path


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(recorder_module.time, "monotonic", fake)
    return fake


def _everything(running: set[str] | None = None):
    return journal.list_tasks(limit=100, before=None, visible=lambda _key: True,
                              running_ids=running or set(), waiting_keys=set())


def _detail(task_id: str):
    return journal.get_task(task_id, visible=lambda _k: True, running_ids=set(), waiting_keys=set())


def _work(rec: ActivityRecorder, key: str, turn: str, *, outcome: str = "completed", tool: str = "web_search",
          trigger: dict | None = None, **end: Any):
    """One finished turn that did work."""
    task = rec.begin(session_key=key, task_id=turn, trigger=trigger or {"kind": "owner"}, request=f"do {turn}")
    rec.note_tool(key, tool, {"query": turn}, ok=True, duration_ms=1)
    return rec.end(task, outcome=outcome, **end)


# ── steps ───────────────────────────────────────────────────────────────────


def test_a_step_keeps_a_short_target_and_never_the_arguments():
    assert describe_step("web_search", {"query": "Flights  to\nRome"}) == {
        "tool": "web_search", "kind": "search", "target": "Flights to Rome"}
    assert describe_step("web_fetch", {"url": "https://www.example.com/a?token=x"})["target"] == "example.com"
    assert describe_step("read_file", {"path": "/Users/me/secret/plan.md"})["target"] == "plan.md"
    assert describe_step("write_file", {"path": "out/report.md", "content": "…"})["target"] == "report.md"
    # A command keeps its program only: arguments carry paths, hosts and secrets.
    assert describe_step("exec", {"command": "/usr/bin/curl -H 'Authorization: x' https://a"})["target"] == "curl"
    assert describe_step("mcp_higgsfield_generate", {"prompt": "x"}) == {
        "tool": "mcp_higgsfield_generate", "kind": "mcp", "target": "higgsfield_generate"}
    assert describe_step("message_profile", {"profile": "luna", "message": "…"})["target"] == "luna"
    assert describe_step("image_generate", {"prompt": "a long private prompt"})["target"] == ""
    assert describe_step("something_new", {"x": 1}) == {"tool": "something_new", "kind": "other", "target": ""}


def test_recalling_asking_and_planning_are_conversation_everything_else_is_work():
    for tool in ("memory_search", "memory_append", "knowledge_graph", "session_search", "clarify",
                 "plan", "skill_view", "skills_list"):
        assert not is_work(describe_step(tool, {})), tool
    for tool in ("web_search", "web_fetch", "read_file", "write_file", "exec", "email", "mcp_linear_create",
                 "message_profile", "image_generate", "cron", "a_tool_added_tomorrow"):
        assert is_work(describe_step(tool, {})), tool
    assert not is_work({}) and not is_work({"tool": ""})


def test_a_target_that_looks_like_a_credential_is_dropped():
    assert describe_step("web_search", {"query": "Bearer abcdefghijk"})["target"] == ""
    assert describe_step("web_search", {"query": "sk-live_1234567890abc"})["target"] == ""


# ── recording a task ────────────────────────────────────────────────────────


def test_a_task_is_recorded_from_start_to_end(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:chat", task_id="run-1", trigger={"kind": "owner"},
                     request="Find three  sources\nabout Rome flights")
    clock.now += 5
    rec.note_tool("desktop:chat", "web_search", {"query": "Rome"}, ok=True, duration_ms=1200, result="7 results")
    clock.now += 2
    ended = rec.end(task, outcome="completed", usage={"prompt_tokens": 900, "completion_tokens": 80},
                    model="claude", conversation_title="Research")

    assert ended.excerpts == ["7 results"]
    tasks, _ = store.load()
    saved = tasks["run-1"]
    assert saved["ended"] is True
    assert saved["status"] == "done"
    assert saved["request"] == "Find three sources about Rome flights"
    assert saved["activeMs"] == 7000
    assert saved["work"] is True and saved["taskId"] == "run-1"
    assert saved["steps"] == [{"tool": "web_search", "kind": "search", "target": "Rome", "ok": True, "durationMs": 1200}]
    assert saved["tokens"] == {"input": 900, "output": 80}
    # Step results stay in memory for the summary; they never reach the disk.
    assert "7 results" not in (home / "activity").joinpath(next((home / "activity").iterdir()).name).read_text()


@pytest.mark.parametrize("outcome,status", [("aborted", "stopped"), ("error", "failed"), ("completed", "done")])
def test_the_status_follows_how_the_turn_ended(home, clock, outcome, status):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="x")
    rec.note_tool("desktop:c", "read_file", {"path": "a.md"}, ok=True, duration_ms=1)
    ended = rec.end(task, outcome=outcome, error="Provider unavailable")
    assert ended.record["status"] == status
    assert ("error" in ended.record) is (status == "failed")


@pytest.mark.asyncio
async def test_time_parked_on_the_owner_is_not_active_time(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="deploy")
    clock.now += 4
    await rec.on_approval_requested(SimpleNamespace(id="a1", session_key="desktop:c", kind="exec",
                                                    request=SimpleNamespace(command="npm run deploy --token=x")))
    clock.now += 600  # ten minutes waiting for a yes
    await rec.on_approval_closed("a1", "allow-once", "desktop:c")
    rec.note_tool("desktop:c", "exec", {"command": "npm run deploy"}, ok=True, duration_ms=1)
    clock.now += 3
    ended = rec.end(task, outcome="completed")
    assert ended.record["activeMs"] == 7000
    # The command is reduced to its program, like a step's target.
    assert ended.record["prompts"] == [{"kind": "approval", "subject": "npm", "decision": "allowed"}]


@pytest.mark.asyncio
async def test_a_task_that_ended_on_a_refusal_is_blocked(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="send it")
    await rec.on_approval_requested(SimpleNamespace(id="a1", session_key="desktop:c", kind="action",
                                                    request=SimpleNamespace(command="Send email to team@x.com")))
    await rec.on_approval_closed("a1", "deny", "desktop:c")
    rec.note_tool("desktop:c", "email", {"to": "team@x.com"}, ok=False, duration_ms=3)
    ended = rec.end(task, outcome="completed")
    assert ended.record["status"] == "blocked"
    assert ended.record["steps"][-1]["blocked"] is True
    assert ended.record["prompts"][0] == {"kind": "approval", "subject": "Send email to team@x.com", "decision": "denied"}


def test_a_failed_step_without_a_refusal_is_not_blocked(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="read")
    rec.note_tool("desktop:c", "read_file", {"path": "missing.md"}, ok=False, duration_ms=1)
    assert rec.end(task, outcome="completed").record["status"] == "done"


@pytest.mark.parametrize("tools", [[], ["memory_search", "memory_append", "clarify"]])
def test_a_conversation_leaves_no_trace(home, clock, tools):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="hey, you up?")
    for tool in tools:
        rec.note_tool("desktop:c", tool, {}, ok=True, duration_ms=1)
    clock.now += 90  # a slow model is still a conversation
    assert rec.active_ids() == set()
    assert rec.end(task, outcome="completed", reply_chars=40) is None
    # Not even the owner's words reach the journal.
    assert not (home / "activity").exists()
    assert _everything()["items"] == []


@pytest.mark.parametrize("outcome", ["aborted", "error", "silent"])
def test_a_conversation_stays_one_however_it_ends(home, clock, outcome):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="hi")
    assert rec.end(task, outcome=outcome, error="Provider unavailable") is None
    assert not (home / "activity").exists()


def test_work_is_on_record_the_moment_it_starts(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="find flights")
    rec.note_tool("desktop:c", "memory_search", {"query": "home airport"}, ok=True, duration_ms=1)
    assert not (home / "activity").exists() and rec.active_ids() == set()

    rec.note_tool("desktop:c", "web_search", {"query": "flights"}, ok=True, duration_ms=1)
    # Written now, so a crash from here on reads as interrupted, not lost.
    tasks, _ = store.load()
    assert tasks["r"]["boot"] == BOOT_ID and tasks["r"]["ended"] is False and tasks["r"]["work"] is True
    assert rec.active_ids() == {"r"}
    assert [(item["id"], item["status"]) for item in _everything(rec.active_ids())["items"]] == [("r", "running")]
    rec.note_tool("desktop:c", "web_fetch", {"url": "https://example.com"}, ok=True, duration_ms=1)
    assert sum(1 for line in store.read_lines() if line["type"] == "start") == 1
    rec.end(task, outcome="completed")
    assert _everything()["items"][0]["status"] == "done"


@pytest.mark.parametrize("trigger", [{"kind": "routine", "jobId": "j", "name": "Morning brief"},
                                     {"kind": "goal", "goalId": "g1"}])
def test_a_routine_or_a_goal_is_work_before_its_first_step(home, clock, trigger):
    rec = ActivityRecorder()
    task = rec.begin(session_key="cron:j", task_id="r", trigger=trigger, request="brief me")
    assert rec.active_ids() == {"r"}
    ended = rec.end(task, outcome="completed")
    assert ended.record["work"] is True
    assert len(_everything()["items"]) == 1


def test_a_goal_is_one_task_across_its_turns(home, clock):
    rec = ActivityRecorder()
    for turn in ("g-1", "g-2", "g-3"):
        _work(rec, "desktop:c", turn, trigger={"kind": "goal", "goalId": "ship"})
    [item] = _everything()["items"]
    assert item["id"] == "goal:ship"
    assert [step["target"] for step in _detail("goal:ship")["steps"]] == ["g-1", "g-2", "g-3"]
    # A turn's own id still finds the task it is part of.
    assert _detail("g-2")["id"] == "goal:ship"


@pytest.mark.parametrize("trigger", [{"kind": "owner"}, {"kind": "channel", "channel": "telegram"}])
def test_a_long_reply_without_tools_is_the_models_to_judge(home, clock, trigger):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger=trigger, request="write me a cover letter")
    ended = rec.end(task, outcome="completed", reply_chars=LONG_REPLY_CHARS)
    assert ended.record["work"] is False
    # Not shown on its own, and not shown when there is no judgement at all.
    assert _everything()["items"] == []
    store.append({"type": "recap", "id": "r", "work": True, "recap": {"title": "Write a cover letter",
                                                                     "outcome": "Drafted it", "summary": "", "steps": []}})
    assert [item["title"] for item in _everything()["items"]] == ["Write a cover letter"]


@pytest.mark.parametrize("trigger", [{"kind": "owner"}, {"kind": "channel", "channel": "telegram"}])
def test_a_short_reply_without_work_is_no_candidate_wherever_it_came_from(home, clock, trigger):
    rec = ActivityRecorder()
    task = rec.begin(session_key="telegram:1", task_id="r", trigger=trigger, request="thanks!")
    assert rec.end(task, outcome="completed", reply_chars=LONG_REPLY_CHARS - 1) is None
    assert not (home / "activity").exists()


def test_the_model_can_call_a_tool_using_turn_conversation_but_not_a_routine(home, clock):
    rec = ActivityRecorder()
    _work(rec, "desktop:c", "weather")
    _work(rec, "cron:j", "brief", trigger={"kind": "routine", "jobId": "j", "name": "Brief"})
    for turn in ("weather", "brief"):
        store.append({"type": "recap", "id": turn, "work": False,
                      "recap": {"title": "x", "outcome": "y", "summary": "", "steps": []}})
    assert [item["id"] for item in _everything()["items"]] == ["brief"]


def test_silence_without_work_is_not_a_task(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="slack:C1", task_id="r", trigger={"kind": "channel", "channel": "slack"}, request="hi")
    assert rec.end(task, outcome="silent") is None
    assert _everything()["items"] == []
    # …but silence after real work is a finished task.
    task = rec.begin(session_key="slack:C1", task_id="r2", trigger={"kind": "channel", "channel": "slack"}, request="x")
    rec.note_tool("slack:C1", "web_search", {"query": "q"}, ok=True, duration_ms=1)
    assert rec.end(task, outcome="silent").record["status"] == "done"


@pytest.mark.parametrize("key", ["desktop:profile-inbox:luna:1", "desktop:profile-room:r", "desktop:profile-task:t",
                                 "heartbeat:tick", "subagent:abc"])
def test_host_orchestration_and_housekeeping_are_not_tasks(home, key):
    assert ActivityRecorder().begin(session_key=key, task_id="r", trigger={"kind": "owner"}, request="x") is None
    assert not (home / "activity").exists()


def test_steps_of_another_conversation_do_not_leak_in(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:a", task_id="r", trigger={"kind": "owner"}, request="x")
    rec.note_tool("desktop:b", "web_search", {"query": "q"}, ok=True, duration_ms=1)
    # It did no work of its own, so it is not a task at all.
    assert rec.end(task, outcome="completed") is None


# ── reading ─────────────────────────────────────────────────────────────────


def test_statuses_that_are_only_true_now_are_worked_out_when_read(home, clock):
    rec = ActivityRecorder()
    for key, turn in (("desktop:run", "running"), ("desktop:wait", "waiting")):
        rec.begin(session_key=key, task_id=turn, trigger={"kind": "owner"}, request="a")
        rec.note_tool(key, "web_search", {"query": "q"}, ok=True, duration_ms=1)
    store.append({"type": "start", "id": "crashed", "taskId": "crashed", "sessionKey": "desktop:old", "request": "c",
                  "work": True, "startedAt": int(time.time() * 1000) - 5000, "boot": "earlier-boot"})

    listed = journal.list_tasks(limit=10, before=None, visible=lambda _key: True,
                                running_ids=rec.active_ids(), waiting_keys={"desktop:wait"})
    statuses = {item["id"]: item["status"] for item in listed["items"]}
    assert statuses == {"running": "running", "waiting": "waiting", "crashed": "interrupted"}
    assert {item["id"]: item["unseen"] for item in listed["items"]} == {
        "running": False, "waiting": True, "crashed": True}


def test_the_dot_clears_once_seen_and_comes_back_for_new_trouble(home, clock):
    rec = ActivityRecorder()
    _work(rec, "desktop:c", "one", outcome="error", error="boom")
    assert _everything()["items"][0]["unseen"] is True

    journal.mark_seen(int(time.time() * 1000) + 1)
    assert _everything()["items"][0]["unseen"] is False
    assert journal.mark_seen(1) > 1  # the cursor never moves back

    time.sleep(0.005)
    _work(rec, "desktop:d", "two", outcome="error", error="again")
    assert {item["id"]: item["unseen"] for item in _everything()["items"]} == {"two": True, "one": False}


def test_titles_come_from_the_summary_never_from_the_owners_message(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"},
                     request="Please look into cheap flights to Rome for me")
    rec.note_tool("desktop:c", "web_search", {"query": "Rome flights"}, ok=True, duration_ms=1)
    rec.end(task, outcome="completed", conversation_title="Rome trip")
    item = _everything()["items"][0]
    # No summary yet: no title, and the apps show the conversation's.
    assert (item["title"], item["conversationTitle"]) == ("", "Rome trip")
    assert item["kind"] == "research"
    assert item["summarized"] is False

    store.append({"type": "recap", "id": "r", "recap": {
        "title": "Find flights to Rome", "outcome": "Found three under 200 euros", "summary": "I checked.",
        "steps": [{"i": 0, "note": "Compared three airlines."}]}})
    item = _everything()["items"][0]
    assert (item["title"], item["outcome"], item["summarized"]) == (
        "Find flights to Rome", "Found three under 200 euros", True)
    detail = _detail("r")
    assert detail["summary"] == "I checked."
    assert detail["steps"][0]["note"] == "Compared three airlines."


def test_pages_newest_first_and_hides_what_the_caller_may_not_see(home, clock):
    rec = ActivityRecorder()
    for index in range(5):
        _work(rec, f"desktop:{index}", f"t{index}")
        time.sleep(0.002)
    page = journal.list_tasks(limit=2, before=None, visible=lambda key: key != "desktop:3",
                              running_ids=set(), waiting_keys=set())
    assert [item["id"] for item in page["items"]] == ["t4", "t2"]
    rest = journal.list_tasks(limit=10, before=page["nextBefore"], visible=lambda key: key != "desktop:3",
                              running_ids=set(), waiting_keys=set())
    assert [item["id"] for item in rest["items"]] == ["t1", "t0"]
    assert journal.get_task("t3", visible=lambda key: key != "desktop:3", running_ids=set(), waiting_keys=set()) is None


def test_a_torn_last_line_is_skipped_and_old_months_are_pruned(home):
    folder = home / "activity"
    folder.mkdir()
    (folder / "2020-01.jsonl").write_text(json.dumps({"type": "start", "id": "old", "startedAt": 1}) + "\n")
    now = int(time.time() * 1000)
    current = folder / f"{time.strftime('%Y-%m', time.gmtime(now / 1000))}.jsonl"
    current.write_text(json.dumps({"type": "start", "id": "new", "sessionKey": "desktop:c", "startedAt": now})
                       + "\n" + '{"type": "task", "id": "new", "sta')
    tasks, _ = store.load()
    assert set(tasks) == {"old", "new"}
    assert store.prune(90, now_ms=now) == 1
    assert [path.name for path in folder.iterdir()] == [current.name]


# ── the summary ─────────────────────────────────────────────────────────────


def test_a_summary_is_validated_and_cleaned():
    raw = ('<think>plan</think>```json\n{"title": "**Find Rome flights**", "outcome": "Found three fares '
           '【5993†L11-L13】", "summary": "I looked it up.", "steps": [{"i": 0, "note": "Found `fares`"}, '
           '{"i": 9, "note": "out of range"}, {"i": "0", "note": "bad index"}]}\n```')
    assert recap.parse_recap(raw, step_count=2) == {
        "title": "Find Rome flights", "outcome": "Found three fares", "summary": "I looked it up.",
        "steps": [{"i": 0, "note": "Found fares"}]}


@pytest.mark.parametrize("raw", ["not json", '{"title": "", "outcome": "x"}', '["a"]', None,
                                 '{"title": "x"}'])
def test_an_unusable_summary_is_dropped(raw):
    assert recap.parse_recap(raw, step_count=1) is None


def test_the_summary_request_is_compact():
    record = {"request": "r", "status": "done", "trigger": {"kind": "routine"},
              "steps": [{"tool": "web_search", "kind": "search", "target": f"q{i}", "ok": True} for i in range(25)]}
    messages = recap.build_messages(record, ["x" * 5000] * 25, "y" * 9000)
    body = messages[1]["content"]
    assert "[19]" in body and "[20]" not in body and "(+5 more steps)" in body
    assert "Started by: routine" in body
    assert len(body) < 20 * 1_600 + 3_000


class _Provider:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any):
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.asyncio
async def test_the_summary_uses_the_bots_model_and_reports_its_cost():
    provider = _Provider(SimpleNamespace(content='{"title": "T", "outcome": "O", "summary": "S", "steps": []}',
                                         finish_reason="stop", usage={"prompt_tokens": 300, "completion_tokens": 40}))
    result, usage = await recap.summarize(provider, "bot/model", {"request": "x", "steps": []}, [], "reply")
    assert result["title"] == "T"
    assert usage == {"input": 300, "output": 40}
    assert provider.calls[0]["model"] == "bot/model"
    assert provider.calls[0]["purpose"] == "activity_recap"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [RuntimeError("down"), SimpleNamespace(content="Error calling LLM: x", finish_reason="stop", usage={}),
                                    SimpleNamespace(content="oops", finish_reason="error", usage={})])
async def test_a_failed_summary_never_raises(answer):
    result, _usage = await recap.summarize(_Provider(answer), None, {"request": "x", "steps": []}, [], "")
    assert result is None


# ── wire ────────────────────────────────────────────────────────────────────


def test_the_rpcs_validate_what_they_are_given(home):
    from flowly.channels.feature_rpc import (
        FeatureRpcError,
        activity_get,
        activity_list,
        activity_seen,
    )

    for bad in ({"limit": 0}, {"limit": 101}, {"limit": "5"}, {"before": -1}, {"limit": True}):
        with pytest.raises(FeatureRpcError):
            activity_list(bad)
    with pytest.raises(FeatureRpcError):
        activity_get({"id": ""})
    with pytest.raises(FeatureRpcError) as missing:
        activity_get({"id": "nope"})
    assert missing.value.code == "NOT_FOUND"
    with pytest.raises(FeatureRpcError):
        activity_seen({})
    assert activity_list({})["items"] == []
    assert activity_seen({"before": 5})["seenBefore"] == 5


def test_an_account_sees_only_the_tasks_of_conversations_it_may_open(home, clock):
    from flowly.channels.feature_rpc import activity_list
    from flowly.live_voice.authority import RequestOwner, request_owner_scope

    sessions = home / "sessions"
    sessions.mkdir()
    (sessions / "desktop_voice_theirs.jsonl").write_text(json.dumps(
        {"_type": "metadata", "metadata": {"voiceOwner": {"kind": "account", "uid": "b"}}}) + "\n")
    rec = ActivityRecorder()
    for key in ("desktop:plain", "desktop:voice:theirs"):
        _work(rec, key, key)
    with request_owner_scope(RequestOwner(uid="a")):
        assert [item["id"] for item in activity_list({})["items"]] == ["desktop:plain"]
    assert len(activity_list({})["items"]) == 2


def test_every_bot_can_be_read_through_the_profile_host():
    from flowly.profile_host_contract import validate_profile_rpc

    for method in ("activity.list", "activity.get", "activity.seen"):
        assert validate_profile_rpc(method, {})[0] == method


# ── through the real agent loop ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_real_turn_is_recorded_and_summarized(tmp_path, monkeypatch):
    from flowly.agent.loop import AgentLoop
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    bot_home = profiles.create_profile("researcher")
    monkeypatch.setenv("FLOWLY_HOME", str(bot_home))
    monkeypatch.setattr(store, "_cache", None)
    (bot_home / "workspace" / "notes").mkdir(parents=True, exist_ok=True)

    class Scripted(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.purposes: list[str | None] = []
            self.turn = [
                LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id="t1", name="list_dir", arguments={"path": str(bot_home / "workspace" / "notes")})]),
                LLMResponse(content="The notes folder is empty."),
            ]

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.purposes.append(kwargs.get("purpose"))
            if kwargs.get("purpose") == "activity_recap":
                return LLMResponse(content=json.dumps({
                    "title": "Check the notes folder", "outcome": "Found it empty",
                    "summary": "I listed the notes folder; it has nothing yet.",
                    "steps": [{"i": 0, "note": "The folder had no files."}]}),
                    usage={"prompt_tokens": 120, "completion_tokens": 30})
            if kwargs.get("purpose") == "title":
                return LLMResponse(content="Notes folder")
            return self.turn.pop(0) if self.turn else LLMResponse(content="Done.")

    provider = Scripted()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=bot_home / "workspace",
                     main_config=Config(), max_iterations=4, soft_warn_at_iteration=0)
    await loop.process_direct("What is in my notes folder?", session_key="desktop:notes", run_id="run-notes")
    await asyncio.gather(*getattr(loop, "_activity_recap_tasks", set()))

    listed = _everything()["items"]
    assert [(item["id"], item["status"], item["title"], item["outcome"]) for item in listed] == [
        ("run-notes", "done", "Check the notes folder", "Found it empty")]
    detail = journal.get_task("run-notes", visible=lambda _k: True, running_ids=set(), waiting_keys=set())
    assert detail["steps"] == [{"tool": "list_dir", "kind": "read", "target": "notes", "ok": True,
                                "durationMs": detail["steps"][0]["durationMs"], "blocked": False,
                                "note": "The folder had no files."}]
    assert detail["trigger"] == {"kind": "owner"}
    assert detail["recapTokens"] == {"input": 120, "output": 30}
    assert "activity_recap" in provider.purposes


def _message(channel: str, chat: str = "c", sender: str = "owner", **metadata: Any):
    from flowly.bus.events import InboundMessage

    return InboundMessage(channel=channel, sender_id=sender, chat_id=chat, content="hello", metadata=metadata)


def test_a_routine_is_titled_by_its_name_until_summarized(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="cron:job-7", task_id="r",
                     trigger={"kind": "routine", "jobId": "job-7", "name": "Morning brief"},
                     request="Read my inbox and list what needs me today.")
    rec.end(task, outcome="completed")
    item = _everything()["items"][0]
    assert (item["title"], item["kind"], item["trigger"]["name"]) == ("Morning brief", "routine", "Morning brief")


def test_who_started_a_task():
    from flowly.activity.recorder import ROUTINE_METADATA_KEY
    from flowly.agent.loop import _AGENT_INTRODUCTION, AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    trigger = loop._activity_trigger
    assert trigger(_message("web"), "") == {"kind": "owner"}
    assert trigger(_message("desktop"), "") == {"kind": "owner"}
    assert trigger(_message("telegram"), "") == {"kind": "channel", "channel": "telegram"}
    assert trigger(_message("cron", "job-7"), "") == {"kind": "routine", "jobId": "job-7"}
    # The cron runner names the routine; the name is kept to one short line.
    named = _message("cron", "job-7", **{ROUTINE_METADATA_KEY: {"name": "  Morning\n brief " + "x" * 200}})
    assert trigger(named, "")["name"] == ("Morning brief " + "x" * 200)[:80]
    assert "name" not in trigger(_message("cron", "job-7", **{ROUTINE_METADATA_KEY: "junk"}), "")
    assert trigger(_message("web"), "goal-1") == {"kind": "goal", "goalId": "goal-1"}
    # Not the owner's tasks: helper announcements, background notices, the
    # agent's own introduction, heartbeats.
    assert trigger(_message("system"), "") is None
    assert trigger(_message("web", sender="process"), "") is None
    assert trigger(_message("web", **{_AGENT_INTRODUCTION: True}), "") is None
    assert trigger(_message("heartbeat", "tick"), "") is None


def test_a_turn_that_says_nothing_is_silence_but_a_cancelled_one_is_a_stop(home, clock):
    from flowly.activity import get_activity_recorder
    from flowly.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.sessions = SimpleNamespace(exists=lambda _key: False)
    loop.model = "m"
    rec = get_activity_recorder()

    quiet = rec.begin(session_key="slack:C", task_id="quiet", trigger={"kind": "channel"}, request="x")
    loop._activity_finish(quiet, "aborted", None, _message("slack", "C"), cancelled=False)
    cancelled = rec.begin(session_key="desktop:c", task_id="cancelled", trigger={"kind": "owner"}, request="x")
    rec.note_tool("desktop:c", "web_search", {"query": "q"}, ok=True, duration_ms=1)
    loop._activity_finish(cancelled, "aborted", None, _message("desktop"), cancelled=True)

    assert [(item["id"], item["status"]) for item in _everything()["items"]] == [("cancelled", "stopped")]


@pytest.mark.asyncio
async def test_summaries_can_be_turned_off(tmp_path, monkeypatch):
    from flowly.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    monkeypatch.setattr("flowly.config.loader.load_config",
                        lambda *a, **k: SimpleNamespace(activity=SimpleNamespace(summaries=False)))
    loop._activity_schedule_recap(SimpleNamespace(record={"id": "x", "startedAt": 1}), "reply", "m")
    assert not getattr(loop, "_activity_recap_tasks", set())


# ── a task across turns ─────────────────────────────────────────────────────


def _recap(turn: str, title: str, **extra: Any) -> None:
    store.append({"type": "recap", "id": turn, **extra, "recap": {
        "title": title, "outcome": f"{title} done", "summary": f"I did {title}.",
        "steps": [{"i": 0, "note": f"note for {turn}"}]}})


def test_a_follow_up_the_model_joins_reads_as_one_task(home, clock):
    rec = ActivityRecorder()
    _work(rec, "desktop:c", "review", usage={"prompt_tokens": 100, "completion_tokens": 10})
    _recap("review", "Review the repo", work=True)
    clock.now += 60
    _work(rec, "desktop:c", "fix", tool="edit_file", outcome="error", error="Disk full",
          usage={"prompt_tokens": 50, "completion_tokens": 5})
    _recap("fix", "Review and fix the repo", work=True, taskId="review")

    [item] = _everything()["items"]
    # It keeps the first turn's id and place; the newest summary speaks for it.
    assert (item["id"], item["title"], item["status"], item["unseen"]) == (
        "review", "Review and fix the repo", "failed", True)
    detail = _detail("review")
    assert [(step["tool"], step["note"]) for step in detail["steps"]] == [
        ("web_search", "note for review"), ("edit_file", "note for fix")]
    assert detail["tokens"] == {"input": 150, "output": 15}
    assert detail["request"] == "do review"
    assert detail["error"] == "Disk full"
    # A link to the second turn still opens the task.
    assert _detail("fix")["id"] == "review"


def test_a_joined_turn_that_is_running_makes_its_task_run(home, clock):
    rec = ActivityRecorder()
    _work(rec, "desktop:c", "a")
    _recap("a", "First")
    store.append({"type": "start", "id": "b", "taskId": "a", "sessionKey": "desktop:c", "request": "more",
                  "work": True, "startedAt": int(time.time() * 1000) + 5, "boot": BOOT_ID})
    assert [(item["id"], item["status"]) for item in _everything({"b"})["items"]] == [("a", "running")]


def test_the_old_journal_reads_by_the_same_rule(home):
    now = int(time.time() * 1000)
    old = [
        # A greeting: no steps.
        {"type": "start", "id": "hi", "sessionKey": "desktop:c", "trigger": {"kind": "owner"}, "request": "hi",
         "startedAt": now - 9000, "boot": "old"},
        {"type": "task", "id": "hi", "sessionKey": "desktop:c", "trigger": {"kind": "owner"}, "startedAt": now - 9000,
         "endedAt": now - 8900, "status": "done", "steps": []},
        # Only recalled from memory.
        {"type": "task", "id": "recall", "sessionKey": "desktop:c", "trigger": {"kind": "owner"},
         "startedAt": now - 8000, "endedAt": now - 7900, "status": "done",
         "steps": [{"tool": "memory_search", "kind": "search", "target": "x", "ok": True}]},
        # Real work.
        {"type": "task", "id": "search", "sessionKey": "desktop:c", "trigger": {"kind": "owner"},
         "startedAt": now - 7000, "endedAt": now - 6900, "status": "done",
         "steps": [{"tool": "web_search", "kind": "search", "target": "x", "ok": True}]},
        # Cut short in an old boot: what it was doing is unknown.
        {"type": "start", "id": "lost", "sessionKey": "desktop:c", "trigger": {"kind": "owner"}, "request": "x",
         "startedAt": now - 6000, "boot": "old"},
        # A routine is always a task.
        {"type": "start", "id": "brief", "sessionKey": "cron:j", "trigger": {"kind": "routine", "name": "Brief"},
         "request": "x", "startedAt": now - 5000, "boot": "old"},
    ]
    folder = home / "activity"
    folder.mkdir()
    (folder / f"{time.strftime('%Y-%m', time.gmtime(now / 1000))}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in old))
    assert {item["id"]: item["status"] for item in _everything()["items"]} == {
        "search": "done", "brief": "interrupted"}


def test_earlier_work_is_the_latest_task_of_the_conversation_still_in_reach(home, clock):
    rec = ActivityRecorder()
    _work(rec, "desktop:c", "old")
    time.sleep(0.002)
    _work(rec, "desktop:c", "recent")
    _recap("recent", "Plan the trip")
    _work(rec, "desktop:other", "elsewhere")
    _work(rec, "desktop:c", "brief", trigger={"kind": "routine", "jobId": "j", "name": "Brief"})
    ended_at = _detail("recent")["endedAt"]

    earlier = journal.earlier_work("desktop:c", turn_id="next", started_at=ended_at + 60_000)
    assert earlier == {"id": "recent", "request": "do recent", "title": "Plan the trip",
                       "outcome": "Plan the trip done", "summary": "I did Plan the trip."}
    # Too long after, or about a turn that is itself that task: nothing.
    assert journal.earlier_work("desktop:c", turn_id="next",
                                started_at=ended_at + journal.CONTINUATION_WINDOW_MS + 1) is None
    assert journal.earlier_work("desktop:other", turn_id="elsewhere", started_at=ended_at + 1) is None


def test_a_goals_next_turn_sees_the_task_so_far(home, clock):
    rec = ActivityRecorder()
    _work(rec, "desktop:c", "g1", trigger={"kind": "goal", "goalId": "ship"})
    _recap("g1", "Ship the release")
    _work(rec, "desktop:c", "g2", trigger={"kind": "goal", "goalId": "ship"})
    assert journal.task_so_far("goal:ship", turn_id="g2")["title"] == "Ship the release"
    assert journal.task_so_far("goal:ship", turn_id="g1")["title"] == ""
    assert journal.task_so_far("goal:none", turn_id="x") is None


# ── the model's judgements ──────────────────────────────────────────────────


@pytest.mark.parametrize("raw,judged", [
    ('{"work": true, "continues": false, "title": "T", "outcome": "O"}', {"work": True, "continues": False}),
    ('```json\n{"work": false, "title": "T", "outcome": "O"}\n```', {"work": False}),
    ('{"work": "yes", "continues": 1, "title": "T", "outcome": "O"}', {}),
    ("not json", {}),
    (None, {}),
])
def test_only_a_real_true_or_false_counts_as_a_judgement(raw, judged):
    assert recap.judgements(raw) == judged


def test_the_request_says_what_the_turn_may_carry_on():
    record = {"request": "now fix it", "status": "done", "steps": []}
    earlier = {"id": "a", "request": "review the repo", "title": "Review the repo", "outcome": "Found 3 issues",
               "summary": "I read it."}
    asked = recap.build_messages(record, [], "ok", earlier=earlier)[1]["content"]
    assert asked.startswith("An earlier task in this conversation ended shortly before this turn:")
    assert "Title: Review the repo" in asked and "Outcome: Found 3 issues" in asked
    part = recap.build_messages(record, [], "ok", earlier=earlier, part_of=True)[1]["content"]
    assert part.startswith("This turn is part of an ongoing task.")
    # An earlier task with no summary yet is described by its request.
    bare = recap.build_messages(record, [], "ok", earlier={"id": "a", "request": "review the repo"})[1]["content"]
    assert "Request: review the repo" in bare.split("\n\n")[0]
    alone = recap.build_messages(record, [], "ok")[1]["content"]
    assert alone.startswith("Request: now fix it")
    assert "continues" in recap.build_messages(record, [], "ok")[0]["content"]


@pytest.mark.asyncio
async def test_a_broken_answer_brings_no_judgement():
    provider = _Provider(SimpleNamespace(content='{"work": false, "title": ""}', finish_reason="stop", usage={}))
    result = await recap.recap_turn(provider, None, {"request": "x", "steps": []}, [], "")
    assert (result.words, result.judged) == (None, {})


# ── through the real agent loop, across turns ───────────────────────────────


@pytest.mark.asyncio
async def test_conversation_stays_out_and_a_follow_up_joins_its_work(tmp_path, monkeypatch):
    from flowly.agent.loop import AgentLoop
    from flowly.bus.queue import MessageBus
    from flowly.config.schema import Config
    from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    default = tmp_path / "flowly"
    monkeypatch.setattr(profiles, "_DEFAULT_HOME", default)
    monkeypatch.setattr(profiles, "_PROFILES_ROOT", default / "profiles")
    bot_home = profiles.create_profile("planner")
    monkeypatch.setenv("FLOWLY_HOME", str(bot_home))
    monkeypatch.setattr(store, "_cache", None)
    notes = bot_home / "workspace" / "notes"
    notes.mkdir(parents=True, exist_ok=True)

    def listing(call_id: str) -> LLMResponse:
        return LLMResponse(content=None, tool_calls=[ToolCallRequest(id=call_id, name="list_dir",
                                                                     arguments={"path": str(notes)})])

    class Scripted(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")
            self.recap_requests: list[str] = []
            self.turns = [LLMResponse(content="Hey! I'm here."),
                          listing("t1"), LLMResponse(content="Your notes folder is empty."),
                          listing("t2"), LLMResponse(content="Still empty after a second look.")]
            self.recaps = [
                {"work": True, "title": "Check the notes folder", "outcome": "Found it empty", "summary": "I looked."},
                {"work": True, "continues": True, "title": "Check the notes folder twice",
                 "outcome": "Still empty", "summary": "I looked twice."},
            ]

        def get_default_model(self) -> str:
            return "test/model"

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            if kwargs.get("purpose") == "activity_recap":
                self.recap_requests.append(kwargs["messages"][1]["content"])
                # The first summary is slow: the second must still wait for it.
                await asyncio.sleep(0.05 if len(self.recap_requests) == 1 else 0)
                return LLMResponse(content=json.dumps({**self.recaps.pop(0), "steps": []}), usage={})
            if kwargs.get("purpose") == "title":
                return LLMResponse(content="Notes")
            return self.turns.pop(0) if self.turns else LLMResponse(content="Done.")

    provider = Scripted()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=bot_home / "workspace",
                     main_config=Config(), max_iterations=4, soft_warn_at_iteration=0)
    await loop.process_direct("you up?", session_key="desktop:notes", run_id="run-hi")
    await loop.process_direct("What is in my notes folder?", session_key="desktop:notes", run_id="run-1")
    await loop.process_direct("Look again please", session_key="desktop:notes", run_id="run-2")
    while getattr(loop, "_activity_recap_tasks", set()):
        await asyncio.gather(*loop._activity_recap_tasks)

    # The greeting never reached the journal, nor the model's summary.
    assert len(provider.recap_requests) == 2
    assert all("you up?" not in line for line in (bot_home / "activity").joinpath(
        next((bot_home / "activity").iterdir()).name).read_text().splitlines())
    # The follow-up was judged with the first summary in view, and joined it.
    assert "Title: Check the notes folder" in provider.recap_requests[1]
    [item] = _everything()["items"]
    assert (item["id"], item["title"], item["outcome"]) == ("run-1", "Check the notes folder twice", "Still empty")
    assert len(_detail("run-1")["steps"]) == 2


def test_a_provisional_conversation_title_is_not_a_title(home, clock):
    from flowly.activity import get_activity_recorder
    from flowly.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    metadata = {"title": "can you find me cheap flights to rome", "title_provisional": True}
    loop.sessions = SimpleNamespace(exists=lambda _key: True,
                                    get_or_create=lambda _key: SimpleNamespace(metadata=metadata))
    loop.model = "m"
    loop._activity_schedule_recap = lambda *args, **kwargs: None
    rec = get_activity_recorder()
    task = rec.begin(session_key="desktop:t", task_id="t", trigger={"kind": "owner"}, request="x")
    rec.note_tool("desktop:t", "web_search", {"query": "q"}, ok=True, duration_ms=1)
    loop._activity_finish(task, "completed", SimpleNamespace(content="ok", metadata={}), _message("desktop"),
                          cancelled=False)
    assert _everything()["items"][0]["conversationTitle"] == ""


# ── what a task is, at a glance ─────────────────────────────────────────────


@pytest.mark.parametrize("tool,kind", [
    ("email", "message"), ("message", "message"), ("voice_call", "message"),
    ("google_calendar", "calendar"), ("cron", "calendar"),
    ("image_generate", "image"), ("video_generate", "video"), ("voice_generate", "voice"),
    ("write_file", "writing"), ("flowlet", "writing"), ("exec", "code"), ("codex_session", "code"),
    ("mcp_linear_create", "connection"), ("github", "connection"), ("ha_call_service", "connection"),
    ("message_profile", "team"), ("spawn", "team"), ("web_search", "research"), ("web_fetch", "browse"),
    ("browser_tab", "browse"), ("read_file", "files"), ("memory_search", None), ("clarify", None),
    ("image_analyze", None),
])
def test_each_working_step_names_what_kind_of_task_it_is(tool, kind):
    from flowly.activity.steps import task_kind_of

    assert task_kind_of(describe_step(tool, {})) == kind


@pytest.mark.parametrize("tools,kind", [
    (["web_search", "web_fetch", "write_file"], "writing"),  # what it made, not what it read
    (["web_search", "web_fetch"], "research"),
    (["read_file", "email"], "message"),
    (["memory_search", "read_file"], "files"),
])
def test_a_task_takes_the_most_consequential_kind_of_its_work(home, clock, tools, kind):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="x")
    for tool in tools:
        rec.note_tool("desktop:c", tool, {}, ok=True, duration_ms=1)
    rec.end(task, outcome="completed")
    assert _everything()["items"][0]["kind"] == kind


def test_a_goal_and_a_routine_say_so_whatever_they_did(home, clock):
    rec = ActivityRecorder()
    _work(rec, "desktop:g", "g", trigger={"kind": "goal", "goalId": "ship"})
    _work(rec, "cron:j", "r", trigger={"kind": "routine", "jobId": "j"})
    assert {item["id"]: item["kind"] for item in _everything()["items"]} == {"goal:ship": "goal", "r": "routine"}


def test_until_its_title_is_in_a_task_shows_what_was_asked_and_what_it_is_doing(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"},
                     request="find me   cheap flights to Rome " + "x" * 300)
    rec.note_tool("desktop:c", "web_search", {"query": "Rome flights"}, ok=True, duration_ms=1)
    rec.note_tool("desktop:c", "web_fetch", {"url": "https://fares.example.com/rome"}, ok=True, duration_ms=1)
    live = journal.list_tasks(limit=5, before=None, visible=lambda _k: True, running_ids=rec.active_ids(),
                              waiting_keys=set(), live_steps=rec.live_steps())["items"][0]
    assert live["status"] == "running"
    assert live["latestStep"] == {"tool": "web_fetch", "kind": "web", "target": "fares.example.com"}
    assert live["request"].startswith("find me cheap flights to Rome") and len(live["request"]) == 140
    assert live["request"].endswith("…")

    rec.end(task, outcome="completed")
    ended = _everything()["items"][0]
    assert ended["latestStep"]["kind"] == "web"
    # The detail keeps the request whole (as stored).
    assert len(_detail("r")["request"]) == 280


def test_a_task_that_took_no_step_yet_has_no_latest_step(home, clock):
    rec = ActivityRecorder()
    rec.begin(session_key="cron:j", task_id="r", trigger={"kind": "routine", "jobId": "j"}, request="brief")
    item = journal.list_tasks(limit=5, before=None, visible=lambda _k: True, running_ids=rec.active_ids(),
                              waiting_keys=set(), live_steps=rec.live_steps())["items"][0]
    assert (item["status"], item["latestStep"]) == ("running", None)


def test_the_list_rpc_says_what_running_work_is_doing(home, clock):
    from flowly.activity import get_activity_recorder
    from flowly.channels.feature_rpc import activity_get, activity_list

    rec = get_activity_recorder()
    task = rec.begin(session_key="desktop:live", task_id="live", trigger={"kind": "owner"}, request="look around")
    try:
        rec.note_tool("desktop:live", "read_file", {"path": "/tmp/plan.md"}, ok=True, duration_ms=1)
        [item] = activity_list({})["items"]
        assert (item["status"], item["latestStep"]["target"]) == ("running", "plan.md")
        assert activity_get({"id": "live"})["task"]["latestStep"]["target"] == "plan.md"
    finally:
        rec.end(task, outcome="completed")


def test_a_running_tasks_detail_shows_its_steps_so_far_and_its_kind(home, clock):
    rec = ActivityRecorder()
    rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="find flights")
    rec.note_tool("desktop:c", "web_search", {"query": "Rome flights"}, ok=True, duration_ms=900)
    rec.note_tool("desktop:c", "web_fetch", {"url": "https://fares.example.com"}, ok=False, duration_ms=40)
    now = dict(running_ids=rec.active_ids(), waiting_keys=set(), live_steps=rec.live_steps())

    detail = journal.get_task("r", visible=lambda _k: True, **now)
    # Steps reach the disk only when the turn ends; the detail reads them live.
    assert [(step["tool"], step["ok"]) for step in detail["steps"]] == [("web_search", True), ("web_fetch", False)]
    assert (detail["status"], detail["kind"]) == ("running", "research")
    assert journal.list_tasks(limit=5, before=None, visible=lambda _k: True, **now)["items"][0]["kind"] == "research"
    # The recorder hands out copies: a reader cannot change a running task.
    rec.live_steps()["r"][0]["tool"] = "tampered"
    assert rec.live_steps()["r"][0]["tool"] == "web_search"


# ── a step in full ──────────────────────────────────────────────────────────


def _transcript(home, key: str, calls: list[tuple[str, str, dict, str]]) -> None:
    """A conversation file holding tool calls: (call id, tool, arguments, result)."""
    sessions = home / "sessions"
    sessions.mkdir(exist_ok=True)
    lines = [{"_type": "metadata", "metadata": {}}]
    for call_id, tool, args, result in calls:
        lines.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]})
        lines.append({"role": "tool", "tool_call_id": call_id, "name": tool, "content": result})
    (sessions / (key.replace(":", "_") + ".jsonl")).write_text("".join(json.dumps(line) + "\n" for line in lines))


def _step(task_id: str, index: int, rec: ActivityRecorder | None = None):
    rec = rec or ActivityRecorder()
    return journal.get_step(task_id, index, visible=lambda _k: True, running_ids=rec.active_ids(),
                            waiting_keys=set(), live_steps=rec.live_steps(), from_memory=rec.step_detail)


def test_a_step_shows_its_call_while_the_turn_runs_and_just_after(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="find flights")
    rec.note_tool("desktop:c", "web_search", {"query": "Rome flights"}, ok=True, duration_ms=900,
                  result="1. Fares to Rome …", call_id="call_1")
    live = _step("r", 0, rec)
    assert (live["tool"], live["detail"]) == ("web_search", "live")
    assert json.loads(live["args"]) == {"query": "Rome flights"} and live["result"] == "1. Fares to Rome …"
    # The call id goes to the journal; the arguments and result never do.
    rec.end(task, outcome="completed")
    on_disk = (home / "activity").joinpath(next((home / "activity").iterdir()).name).read_text()
    assert '"callId":"call_1"' in on_disk and "Fares to Rome" not in on_disk
    assert _step("r", 0, rec)["detail"] == "live"  # this process still has it


def test_a_step_reads_its_call_from_the_transcript_later(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="x")
    rec.note_tool("desktop:c", "exec", {"command": "ls"}, ok=True, duration_ms=5, result="a.txt", call_id="call_7")
    rec.end(task, outcome="completed")
    store.append({"type": "recap", "id": "r", "recap": {"title": "List files", "outcome": "Found one", "summary": "",
                                                        "steps": [{"i": 0, "note": "Listed the folder; one file."}]}})
    _transcript(home, "desktop:c", [("call_7", "exec", {"command": "ls -la"}, "total 1\na.txt")])

    step = _step("r", 0)  # a new process: nothing in memory
    assert step["detail"] == "transcript"
    assert json.loads(step["args"]) == {"command": "ls -la"} and step["result"] == "total 1\na.txt"
    assert step["note"] == "Listed the folder; one file."
    assert (step["tool"], step["kind"], step["ok"], step["blocked"]) == ("exec", "exec", True, False)


def test_a_step_without_its_call_says_so(home, clock):
    rec = ActivityRecorder()
    _work(rec, "desktop:c", "r")  # no call id, as the old journal
    step = _step("r", 0)
    assert step["detail"] == "none" and "args" not in step and "result" not in step
    assert _step("r", 1) is None and _step("r", -1) is None and _step("nope", 0) is None


def test_a_step_index_counts_across_a_tasks_turns(home, clock):
    rec = ActivityRecorder()
    for turn, call in (("a", "call_a"), ("b", "call_b")):
        task = rec.begin(session_key="desktop:c", task_id=turn, trigger={"kind": "owner"}, request=turn)
        rec.note_tool("desktop:c", "web_search", {"query": turn}, ok=True, duration_ms=1, result=f"hits for {turn}",
                      call_id=call)
        rec.end(task, outcome="completed")
    store.append({"type": "recap", "id": "b", "taskId": "a", "work": True,
                  "recap": {"title": "T", "outcome": "O", "summary": "", "steps": []}})
    assert _step("a", 1, rec)["result"] == "hits for b"
    assert _step("b", 0, rec)["result"] == "hits for a"  # a turn's id opens its task


def test_only_recent_turns_keep_their_calls_in_memory(home, clock):
    from flowly.activity.recorder import RECENT_DETAIL_TURNS

    rec = ActivityRecorder()
    for n in range(RECENT_DETAIL_TURNS + 1):
        _work(rec, "desktop:c", f"t{n}")
    assert rec.step_detail("t0", 0) is None
    assert rec.step_detail(f"t{RECENT_DETAIL_TURNS}", 0) is not None


def test_a_conversation_the_caller_may_not_open_keeps_its_steps(home, clock):
    rec = ActivityRecorder()
    _work(rec, "desktop:secret", "r")
    assert journal.get_step("r", 0, visible=lambda key: key != "desktop:secret", running_ids=set(),
                            waiting_keys=set()) is None


def test_the_step_rpc_validates_and_reads(home, clock):
    from flowly.activity import get_activity_recorder
    from flowly.channels.feature_rpc import FeatureRpcError, activity_step
    from flowly.profile_host_contract import validate_profile_rpc

    for bad in ({}, {"id": "r"}, {"id": "", "index": 0}, {"id": "r", "index": -1}, {"id": "r", "index": "0"}):
        with pytest.raises(FeatureRpcError):
            activity_step(bad)
    with pytest.raises(FeatureRpcError) as missing:
        activity_step({"id": "nope", "index": 0})
    assert missing.value.code == "NOT_FOUND"
    rec = get_activity_recorder()
    task = rec.begin(session_key="desktop:rpc", task_id="rpc", trigger={"kind": "owner"}, request="x")
    try:
        rec.note_tool("desktop:rpc", "read_file", {"path": "/tmp/a.md"}, ok=True, duration_ms=1, result="hello",
                      call_id="c1")
        assert activity_step({"id": "rpc", "index": 0})["step"]["result"] == "hello"
    finally:
        rec.end(task, outcome="completed")
    assert validate_profile_rpc("activity.step", {"id": "rpc", "index": 0})[0] == "activity.step"
