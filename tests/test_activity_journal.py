"""A bot's activity: each task recorded from the turn, summarized by its model.

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
from flowly.activity.recorder import ActivityRecorder
from flowly.activity.steps import describe_step


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


def _everything():
    return journal.list_tasks(limit=100, before=None, visible=lambda _key: True, running_ids=set(), waiting_keys=set())


# ── steps ───────────────────────────────────────────────────────────────────


def test_a_step_keeps_a_short_target_and_never_the_arguments():
    assert describe_step("web_search", {"query": "Hermes  AI\nagent"}) == {
        "tool": "web_search", "kind": "search", "target": "Hermes AI agent"}
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


def test_a_target_that_looks_like_a_credential_is_dropped():
    assert describe_step("web_search", {"query": "Bearer abcdefghijk"})["target"] == ""
    assert describe_step("web_search", {"query": "sk-live_1234567890abc"})["target"] == ""


# ── recording a task ────────────────────────────────────────────────────────


def test_a_task_is_recorded_from_start_to_end(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:chat", task_id="run-1", trigger={"kind": "owner"},
                     request="Find three  sources\nabout Hermes")
    clock.now += 5
    rec.note_tool("desktop:chat", "web_search", {"query": "Hermes"}, ok=True, duration_ms=1200, result="7 results")
    clock.now += 2
    ended = rec.end(task, outcome="completed", usage={"prompt_tokens": 900, "completion_tokens": 80},
                    model="claude", conversation_title="Research")

    assert ended.summarize is True
    assert ended.excerpts == ["7 results"]
    tasks, _ = store.load()
    saved = tasks["run-1"]
    assert saved["ended"] is True
    assert saved["status"] == "done"
    assert saved["request"] == "Find three sources about Hermes"
    assert saved["activeMs"] == 7000
    assert saved["steps"] == [{"tool": "web_search", "kind": "search", "target": "Hermes", "ok": True, "durationMs": 1200}]
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
    clock.now += 3
    ended = rec.end(task, outcome="completed")
    assert ended.record["activeMs"] == 7000
    assert ended.summarize is False  # no tool, 7 s of work
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


def test_long_work_without_tools_is_summarized(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"}, request="write an essay")
    clock.now += 31
    assert rec.end(task, outcome="completed").summarize is True


def test_silence_without_work_is_not_a_task(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="slack:C1", task_id="r", trigger={"kind": "channel", "channel": "slack"}, request="hi")
    assert rec.end(task, outcome="silent") is None
    assert _everything()["items"] == []
    # …but silence after real work is a finished task.
    task = rec.begin(session_key="slack:C1", task_id="r2", trigger={"kind": "channel", "channel": "slack"}, request="x")
    rec.note_tool("slack:C1", "memory_append", {}, ok=True, duration_ms=1)
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
    assert rec.end(task, outcome="completed").record["steps"] == []


# ── reading ─────────────────────────────────────────────────────────────────


def test_statuses_that_are_only_true_now_are_worked_out_when_read(home, clock):
    rec = ActivityRecorder()
    rec.begin(session_key="desktop:run", task_id="running", trigger={"kind": "owner"}, request="a")
    rec.begin(session_key="desktop:wait", task_id="waiting", trigger={"kind": "owner"}, request="b")
    store.append({"type": "start", "id": "crashed", "sessionKey": "desktop:old", "request": "c",
                  "startedAt": int(time.time() * 1000) - 5000, "boot": "earlier-boot"})

    listed = journal.list_tasks(limit=10, before=None, visible=lambda _key: True,
                                running_ids=rec.active_ids(), waiting_keys={"desktop:wait"})
    statuses = {item["id"]: item["status"] for item in listed["items"]}
    assert statuses == {"running": "running", "waiting": "waiting", "crashed": "interrupted"}
    assert {item["id"]: item["unseen"] for item in listed["items"]} == {
        "running": False, "waiting": True, "crashed": True}


def test_the_dot_clears_once_seen_and_comes_back_for_new_trouble(home, clock):
    rec = ActivityRecorder()
    first = rec.begin(session_key="desktop:c", task_id="one", trigger={"kind": "owner"}, request="x")
    rec.end(first, outcome="error", error="boom")
    assert _everything()["items"][0]["unseen"] is True

    journal.mark_seen(int(time.time() * 1000) + 1)
    assert _everything()["items"][0]["unseen"] is False
    assert journal.mark_seen(1) > 1  # the cursor never moves back

    time.sleep(0.005)
    second = rec.begin(session_key="desktop:c", task_id="two", trigger={"kind": "owner"}, request="y")
    time.sleep(0.005)
    rec.end(second, outcome="error", error="again")
    assert {item["id"]: item["unseen"] for item in _everything()["items"]} == {"two": True, "one": False}


def test_titles_come_from_the_summary_or_the_request(home, clock):
    rec = ActivityRecorder()
    task = rec.begin(session_key="desktop:c", task_id="r", trigger={"kind": "owner"},
                     request="Please look into the Hermes agent for me")
    rec.note_tool("desktop:c", "web_search", {"query": "Hermes"}, ok=True, duration_ms=1)
    rec.end(task, outcome="completed")
    item = _everything()["items"][0]
    assert item["title"] == "Please look into the Hermes agent for me"
    assert item["kind"] == "research"
    assert item["summarized"] is False

    store.append({"type": "recap", "id": "r", "recap": {
        "title": "Research Hermes AI agent", "outcome": "Confirmed it is an AI agent", "summary": "I checked.",
        "steps": [{"i": 0, "note": "Found the GitHub repo."}]}})
    item = _everything()["items"][0]
    assert (item["title"], item["outcome"], item["summarized"]) == (
        "Research Hermes AI agent", "Confirmed it is an AI agent", True)
    detail = journal.get_task("r", visible=lambda _k: True, running_ids=set(), waiting_keys=set())
    assert detail["summary"] == "I checked."
    assert detail["steps"][0]["note"] == "Found the GitHub repo."


def test_pages_newest_first_and_hides_what_the_caller_may_not_see(home, clock):
    rec = ActivityRecorder()
    for index in range(5):
        task = rec.begin(session_key=f"desktop:{index}", task_id=f"t{index}", trigger={"kind": "owner"}, request=str(index))
        rec.end(task, outcome="completed")
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
    raw = ('<think>plan</think>```json\n{"title": "**Research Hermes**", "outcome": "Confirmed an AI agent '
           '【5993†L11-L13】", "summary": "I looked it up.", "steps": [{"i": 0, "note": "Found `repo`"}, '
           '{"i": 9, "note": "out of range"}, {"i": "0", "note": "bad index"}]}\n```')
    assert recap.parse_recap(raw, step_count=2) == {
        "title": "Research Hermes", "outcome": "Confirmed an AI agent", "summary": "I looked it up.",
        "steps": [{"i": 0, "note": "Found repo"}]}


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
        rec.end(rec.begin(session_key=key, task_id=key, trigger={"kind": "owner"}, request="x"), outcome="completed")
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
