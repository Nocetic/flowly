"""A subagent's replies stream token by token to whoever watches its thread (phase 6).

While somebody watches a thread (``SubagentStreamHub.watched``) and the
provider streams, the child's model call goes through ``chat_stream`` — the
main agent's streaming path, assembled by the same ``StreamedReply`` — and its
text goes out as ``agent`` deltas while it is written. These tests pin

* streamed deltas assemble to exactly the reply the run records (checkpoint,
  journal, saved turn, terminal), and a streamed run records what a blocking
  run of the same replies records, call for call;
* tool-call boundaries: a reply's text goes out before its ``iteration_step``,
  which takes it out of the live bubble (in-flight text too);
* the in-flight entry holds the partial reply, and a watcher arriving mid-reply
  gets every character exactly once (snapshot + deltas);
* nobody watching: the call is today's blocking ``chat`` and nothing more is
  built; a provider without streaming keeps one delta per reply; a stream that
  fails before any text falls back to a blocking call;
* a reply that fails after some text is taken back (``stream_reset``) and the
  retry streams cleanly; a timeout bounds the whole reply;
* a crash mid-stream: nothing of the partial reply is persisted, the restart
  redoes the call once and the replay is exact;
* coalescing and backpressure: a slow watcher never holds the child up and its
  queue holds about one delta of a long reply;
* tokens are counted from the final response, as for the main agent.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest

from flowly.agent import inflight, subagent_stream
from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_checkpoint import read_checkpoint, restore_child_state
from flowly.agent.subagent_registry import SubagentRegistry, SubagentRunRecord
from flowly.agent.subagent_stream import ChildStream, SubagentStreamHub, child_event_access
from flowly.agent.tools.discovery import annotate_search_repetition
from flowly.bus.queue import MessageBus
from flowly.providers.base import LLMErrorInfo, LLMProvider, LLMResponse, ToolCallRequest
from flowly.providers.streamed_reply import StreamedReply

CHAT = "desktop:chat-1"
MODEL = "m"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("FLOWLY_HOME", str(path))
    inflight._runs.clear()
    inflight._control_scopes.clear()
    yield path
    inflight._runs.clear()
    inflight._control_scopes.clear()


@pytest.fixture
def every_token(monkeypatch):
    """No coalescing interval: each chunk is a delta (unless the watcher is behind)."""
    monkeypatch.setattr(subagent_stream, "_DELTA_INTERVAL_SECONDS", 0.0)


# ── Helpers ──────────────────────────────────────────────────────────────────


class _Fails:
    """A reply that streams ``partial`` and then fails with ``error``."""

    def __init__(self, partial: str, error: LLMResponse) -> None:
        self.partial = partial
        self.error = error


def _pieces(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


class _Streaming(LLMProvider):
    """Scripted replies; ``chat_stream`` yields each in pieces, as a real provider does.

    Text deltas (``content``, ``finish_reason=""``), then one final chunk with
    the finish reason, the tool calls and the usage (``content=None``). A
    call may pause before its k-th chunk (``pause(n, k)``). Every request is
    kept in ``sent``, the kind of call in ``kinds``.
    """

    def __init__(self, script: list[Any], *, size: int = 3, gap: float = 0.0,
                 default: str = "All done.") -> None:
        super().__init__(api_key="unused")
        self.script = list(script)
        self.size = size
        self.gap = gap
        self.default = default
        self.sent: list[list[dict[str, Any]]] = []
        self.kinds: list[str] = []
        self.pauses: dict[tuple[int, int], asyncio.Event] = {}
        self.paused: dict[tuple[int, int], asyncio.Event] = {}

    def get_default_model(self) -> str:
        return MODEL

    def pause(self, call: int, chunk: int) -> asyncio.Event:
        self.pauses[(call, chunk)] = asyncio.Event()
        self.paused[(call, chunk)] = asyncio.Event()
        return self.pauses[(call, chunk)]

    def _next(self) -> Any:
        return self.script.pop(0) if self.script else LLMResponse(
            content=self.default, usage={"prompt_tokens": 7, "completion_tokens": 3})

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.sent.append(copy.deepcopy(kwargs["messages"]))
        self.kinds.append("chat")
        step = self._next()
        return step.error if isinstance(step, _Fails) else step

    async def chat_stream(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7,
                          tool_choice="auto"):
        self.sent.append(copy.deepcopy(messages))
        self.kinds.append("stream")
        n = len(self.sent)
        step = self._next()
        text = step.partial if isinstance(step, _Fails) else (step.content or "")
        for k, piece in enumerate(_pieces(text, self.size)):
            if (n, k) in self.pauses:
                self.paused[(n, k)].set()
                await self.pauses[(n, k)].wait()
            if self.gap:
                await asyncio.sleep(self.gap)
            yield LLMResponse(content=piece, finish_reason="")
        if isinstance(step, _Fails):
            yield step.error
            return
        yield LLMResponse(content=None, tool_calls=list(step.tool_calls),
                          finish_reason="tool_calls" if step.tool_calls else (step.finish_reason or "stop"),
                          usage=dict(step.usage))


class _Blocking(LLMProvider):
    """The same script, answered by ``chat`` only (a provider without streaming)."""

    def __init__(self, script: list[Any]) -> None:
        super().__init__(api_key="unused")
        self.script = list(script)
        self.sent: list[list[dict[str, Any]]] = []

    def get_default_model(self) -> str:
        return MODEL

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.sent.append(copy.deepcopy(kwargs["messages"]))
        return self.script.pop(0) if self.script else LLMResponse(
            content="All done.", usage={"prompt_tokens": 7, "completion_tokens": 3})


class _Recorder:
    """A transport that watches (or not) and keeps what it was sent, in order."""

    def __init__(self, watching: bool = True) -> None:
        self.watching = watching
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.prepared = 0
        self.checked = 0

    def subagent_stream_watched(self, session_key: str) -> bool:
        self.checked += 1
        return self.watching

    def prepare_subagent_stream(self, event: str, session_key: str, data: dict[str, Any]):
        self.prepared += 1
        if not self.watching:
            return None
        frame = (event, session_key, json.loads(json.dumps(data)))

        async def deliver() -> None:
            self.events.append(frame)

        return deliver


def _manager(home: Path, workspace: Path, provider: LLMProvider, *, sink: Any = None) -> SubagentManager:
    manager = SubagentManager(provider=provider, workspace=workspace, bus=MessageBus(),
                              registry=SubagentRegistry(home / "subagents" / "runs.json"),
                              state_dir=workspace, max_iterations=10)
    manager.resume_enabled = True
    manager.stream_sink = sink
    return manager


async def _spawn(manager: SubagentManager, task: str = "Look into the notes") -> str:
    started = json.loads(await manager.spawn(
        task=task, origin_channel="desktop", origin_chat_id="chat-1", parent_session_key=CHAT,
        origin="agent", context="owner: look into it\nagent: on it",
    ))
    assert started["status"] == "started", started
    return started["run_id"]


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.005)


async def _settle(manager: SubagentManager, run_id: str, hub: SubagentStreamHub | None = None) -> SubagentRunRecord:
    await _until(lambda: manager.registry.get(run_id).ended_at is not None and not manager._running_tasks)
    if hub is not None:
        await hub.drained()
    return manager.registry.get(run_id)


def _read(call_id: str, path: Path, text: str = "Reading the notes first.", **extra: Any) -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[
        ToolCallRequest(id=call_id, name="read_file", arguments={"path": str(path)}, **extra)],
        usage={"prompt_tokens": 10, "completion_tokens": 2})


def _live(events: list[tuple[str, str, dict[str, Any]]]) -> tuple[list[str], str, list[int]]:
    """Replay the live bubble as a client does.

    Returns the text each step boundary took out of the bubble, the text left
    when the terminal came, and the number of ``agent`` deltas per segment.
    """
    segments: list[str] = []
    counts: list[int] = []
    text, count = "", 0
    for name, _key, data in events:
        if name == "agent":
            text += data["data"]["text"]
            count += 1
        elif data.get("state") == "stream_reset":
            text = data["text"]
        elif data.get("state") == "iteration_step" and data["role"] == "assistant" \
                and isinstance(data.get("tool_calls"), list):
            segments.append(text)
            counts.append(count)
            text, count = "", 0
    counts.append(count)
    return segments, text, counts


def _model_records(path_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"message": r["message"], "usage": r.get("usage")} for r in path_records if r["type"] == "model"]


# ── Deltas assemble to the recorded reply ────────────────────────────────────


@pytest.mark.asyncio
async def test_streamed_deltas_assemble_to_exactly_the_recorded_replies(tmp_path, home, every_token):
    (tmp_path / "notes.txt").write_text("the answer is 42")
    script = [
        _read("c1", tmp_path / "notes.txt", text="Reading the notes first, then I answer.",
              extra_content={"google": {"thought_signature": "sig-1"}}),
        LLMResponse(content="The notes say the answer is 42 — nothing else in them.",
                    usage={"prompt_tokens": 9, "completion_tokens": 4}),
    ]
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Streaming(copy.deepcopy(script), size=4, gap=0.002)
    checkpoints: list[list[dict[str, Any]]] = []
    manager = _manager(home, tmp_path, provider, sink=hub)
    real_save = manager._save_turn

    def save_turn(run_id, turn, **kwargs):
        checkpoints.append(read_checkpoint(manager.registry.checkpoint_path(run_id)))
        return real_save(run_id, turn, **kwargs)

    manager._save_turn = save_turn
    run_id = await _spawn(manager)
    record = await _settle(manager, run_id, hub)
    assert record.outcome == "ok", record.error
    assert provider.kinds == ["stream", "stream"]

    segments, closing, counts = _live(recorder.events)
    assert segments == ["Reading the notes first, then I answer."]
    assert closing == "The notes say the answer is 42 — nothing else in them."
    assert all(c > 3 for c in counts), counts  # token by token, not one delta per reply
    final = recorder.events[-1][2]
    assert final["state"] == "final" and final["message"]["content"][0]["text"] == closing
    # The checkpoint's model record and the saved turn have exactly what streamed.
    models = _model_records(checkpoints[0])
    assert models[0]["message"]["content"] == segments[0]
    assert models[0]["message"]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "sig-1"}}
    rows = manager.registry.thread_store.display_rows(run_id)
    assistant = [r for r in rows if r.get("role") == "assistant"]
    assert [a["content"] for a in assistant] == [segments[0], closing]
    # The live boundary event carries the same text it took out of the bubble.
    step = next(e[2] for e in recorder.events if e[2].get("state") == "iteration_step")
    assert step["content"] == segments[0]
    assert not [e for e in recorder.events if e[2].get("state") == "stream_reset"]


@pytest.mark.asyncio
async def test_a_streamed_run_records_what_a_blocking_run_records(tmp_path, home):
    """The golden check: same replies, streamed or not — the same requests, records, rows and tokens."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("the answer is 42")
    script = [
        _read("c1", workspace / "notes.txt", text="Let me read.",
              extra_content={"google": {"thought_signature": "sig-1"}}),
        LLMResponse(content=None, tool_calls=[
            ToolCallRequest(id="c2", name="list_dir", arguments={"path": str(workspace)})],
            usage={"prompt_tokens": 20, "completion_tokens": 3}),
        LLMResponse(content="All read. The answer is 42.", usage={"prompt_tokens": 30, "completion_tokens": 6}),
    ]

    async def run(provider: LLMProvider, home_dir: Path, sink: Any) -> tuple[SubagentRunRecord, list, list, list]:
        kept: list[list[dict[str, Any]]] = []
        manager = _manager(home_dir, workspace, provider, sink=sink)
        real_save = manager._save_turn

        def save_turn(run_id, turn, **kwargs):
            kept.append(read_checkpoint(manager.registry.checkpoint_path(run_id)))
            return real_save(run_id, turn, **kwargs)

        manager._save_turn = save_turn
        run_id = await _spawn(manager)
        record = await _settle(manager, run_id, sink)
        rows = [{k: v for k, v in r.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")}
                for r in manager.registry.thread_store.display_rows(run_id)]
        return record, kept[0], rows, provider.sent

    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    streaming = _Streaming(copy.deepcopy(script), size=2)
    s_record, s_log, s_rows, s_sent = await run(streaming, tmp_path / "a", hub)
    b_record, b_log, b_rows, b_sent = await run(_Blocking(copy.deepcopy(script)), tmp_path / "b", None)
    assert streaming.kinds == ["stream", "stream", "stream"]
    assert s_record.outcome == b_record.outcome == "ok"
    assert s_sent == b_sent and len(s_sent) == 3
    assert _model_records(s_log) == _model_records(b_log) and len(_model_records(s_log)) == 2
    # Replaying the streamed run's checkpoint gives the list its last request had.
    state = restore_child_state(s_log, annotate=annotate_search_repetition)
    assert state.messages == s_sent[-1]
    assert s_rows == b_rows
    assert s_record.usage == b_record.usage
    assert s_record.result_preview == b_record.result_preview == "All read. The answer is 42."
    segments, closing, _ = _live(recorder.events)
    assert segments == ["Let me read.", ""] and closing == "All read. The answer is 42."


# ── Tool-call boundaries and the in-flight entry ─────────────────────────────


@pytest.mark.asyncio
async def test_a_tool_call_boundary_takes_the_streamed_text_out_of_the_live_bubble(tmp_path, home, every_token):
    (tmp_path / "notes.txt").write_text("notes")
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Streaming([_read("c1", tmp_path / "notes.txt", text="Checking the notes now."),
                           LLMResponse(content="Final words here.", usage={"prompt_tokens": 1, "completion_tokens": 1})],
                          size=5, gap=0.002)
    # Pause the closing reply after its first chunk: the bubble holds only it.
    provider.pause(2, 1)
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    key = f"subagent:{run_id}"
    await provider.paused[(2, 1)].wait()
    await _until(lambda: (inflight.get(key) or {}).get("text") == "Final")
    live = inflight.get(key)
    assert live["text"] == "Final"  # the text before the boundary moved into the step
    step = next(e for e in live["iterations"] if e.get("role") == "assistant")
    assert step["content"] == "Checking the notes now." and step["tool_calls"]
    provider.pauses[(2, 1)].set()
    await _settle(manager, run_id, hub)
    names = [(n, d.get("state"), d.get("role")) for n, _k, d in recorder.events]
    boundary = names.index(("chat", "iteration_step", "assistant"))
    first_reply = [d["data"]["text"] for n, _k, d in recorder.events[:boundary] if n == "agent"]
    assert "".join(first_reply) == "Checking the notes now." and len(first_reply) > 1
    # Nothing of the first reply after its boundary; the second starts after the tool result.
    tool_result = names.index(("chat", "iteration_step", "tool"))
    assert not [n for n, *_ in recorder.events[boundary:tool_result] if n == "agent"]
    segments, closing, _ = _live(recorder.events)
    assert segments == ["Checking the notes now."] and closing == "Final words here."


@pytest.mark.asyncio
async def test_a_watcher_arriving_mid_reply_gets_every_character_once(tmp_path, home, every_token):
    late = _Recorder(watching=False)
    early = _Recorder()
    hub = SubagentStreamHub(lambda: [early, late])
    reply = "One two three four five six seven eight nine ten."
    provider = _Streaming([LLMResponse(content=reply, usage={"prompt_tokens": 5, "completion_tokens": 9})],
                          size=3, gap=0.001)
    provider.pause(1, 6)
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    key = f"subagent:{run_id}"
    await provider.paused[(1, 6)].wait()
    await _until(lambda: (inflight.get(key) or {}).get("text") == reply[:18])
    # The late watcher binds and takes its snapshot in one step (as subagents.watch does).
    late.watching = True
    snapshot = inflight.get(key)
    assert snapshot["text"] == reply[:18]
    provider.pauses[(1, 6)].set()
    record = await _settle(manager, run_id, hub)
    assert record.outcome == "ok"
    late_text = snapshot["text"] + "".join(d["data"]["text"] for n, _k, d in late.events if n == "agent")
    assert late_text == reply
    _segments, early_text, _ = _live(early.events)
    assert early_text == reply
    assert late.events[-1][2]["state"] == "final"


# ── Nobody watching; providers without streaming ────────────────────────────


@pytest.mark.asyncio
async def test_with_nobody_watching_the_call_is_the_blocking_one_and_nothing_more_is_built(tmp_path, home):
    (tmp_path / "notes.txt").write_text("notes")
    script = [_read("c1", tmp_path / "notes.txt"), LLMResponse(content="Done.", usage={"prompt_tokens": 1})]

    async def run(provider: LLMProvider, home_dir: Path) -> tuple[_Recorder, SubagentRunRecord]:
        recorder = _Recorder(watching=False)
        hub = SubagentStreamHub(lambda: [recorder])
        manager = _manager(home_dir, tmp_path, provider, sink=hub)
        run_id = await _spawn(manager)
        return recorder, await _settle(manager, run_id, hub)

    streaming = _Streaming(copy.deepcopy(script))
    unwatched, record = await run(streaming, tmp_path / "a")
    blocking_recorder, blocking_record = await run(_Blocking(copy.deepcopy(script)), tmp_path / "b")
    assert record.outcome == "ok"
    assert streaming.kinds == ["chat", "chat"]  # chat_stream never called
    # The same events were offered to the transport as for a provider that cannot stream.
    assert unwatched.prepared == blocking_recorder.prepared and not unwatched.events
    # One watch check per model call, nothing else.
    assert unwatched.checked == 2 and blocking_recorder.checked == 0
    assert record.usage == blocking_record.usage


@pytest.mark.asyncio
async def test_a_provider_without_streaming_keeps_one_delta_per_reply(tmp_path, home):
    (tmp_path / "notes.txt").write_text("notes")
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Blocking([_read("c1", tmp_path / "notes.txt"), LLMResponse(content="Done reading.")])
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    await _settle(manager, run_id, hub)
    segments, closing, counts = _live(recorder.events)
    assert segments == ["Reading the notes first."] and closing == "Done reading."
    assert counts == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [
    LLMResponse(content="Error calling LLM: this route does not stream", finish_reason="error",
                usage={"prompt_tokens": 0}),
    # The blocking call moves to a fallback key; the stream does not.
    LLMResponse(content="Error calling LLM: invalid x-api-key", finish_reason="error",
                error_info=LLMErrorInfo(status_code=401, type="authentication_error")),
], ids=["no-stream", "rejected-key"])
async def test_a_stream_that_fails_before_any_text_falls_back_to_a_blocking_call(tmp_path, home, rejected):
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Streaming([_Fails("", rejected),
                           LLMResponse(content="Answered without streaming.",
                                       usage={"prompt_tokens": 12, "completion_tokens": 5})])
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    record = await _settle(manager, run_id, hub)
    assert record.outcome == "ok" and record.result_preview == "Answered without streaming."
    assert provider.kinds == ["stream", "chat"] and provider.sent[0] == provider.sent[1]
    _segments, closing, counts = _live(recorder.events)
    assert closing == "Answered without streaming." and counts == [1]
    assert not [e for e in recorder.events if e[2].get("state") == "stream_reset"]
    assert record.usage["input"] == 12 and record.usage["output"] == 5


@pytest.mark.asyncio
async def test_a_stream_rejected_for_what_a_blocking_call_is_rejected_for_too_is_not_repeated(tmp_path, home):
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    no_credits = LLMResponse(content="Error calling LLM: insufficient_credits", finish_reason="error",
                             error_info=LLMErrorInfo(status_code=402, code="insufficient_credits"))
    provider = _Streaming([_Fails("", no_credits)])
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    record = await _settle(manager, run_id, hub)
    assert record.outcome == "error" and record.error_code == "insufficient_credits"
    assert provider.kinds == ["stream"]
    assert [e[2]["state"] for e in recorder.events if e[2].get("state") in ("final", "error")] == ["error"]


@pytest.mark.asyncio
async def test_a_reply_that_fails_after_some_text_is_taken_back_and_made_again(tmp_path, home, every_token,
                                                                                monkeypatch):
    from flowly.agent import error_classifier

    monkeypatch.setattr(error_classifier, "backoff_for", lambda *a, **k: 0.0)
    (tmp_path / "notes.txt").write_text("notes")
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    overloaded = LLMResponse(content="Error calling LLM: overloaded_error", finish_reason="error",
                             error_info=LLMErrorInfo(status_code=529, type="overloaded_error"))
    provider = _Streaming([
        _read("c1", tmp_path / "notes.txt", text="Looking."),
        _Fails("I will now explain every", overloaded),
        LLMResponse(content="The notes are short.", usage={"prompt_tokens": 3, "completion_tokens": 2}),
    ], size=4, gap=0.002)
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    key = f"subagent:{run_id}"
    seen: list[str] = []
    real_chat_stream = provider.chat_stream

    def chat_stream(*args, **kwargs):
        seen.append((inflight.get(key) or {}).get("text", ""))
        return real_chat_stream(*args, **kwargs)

    provider.chat_stream = chat_stream
    record = await _settle(manager, run_id, hub)
    assert record.outcome == "ok" and record.result_preview == "The notes are short."
    # No blocking fallback once text went out; the child's own retry made the call again.
    assert provider.kinds == ["stream", "stream", "stream"]
    resets = [e[2] for e in recorder.events if e[2].get("state") == "stream_reset"]
    assert len(resets) == 1 and resets[0]["text"] == "" and resets[0]["runId"] == run_id
    assert resets[0]["turnOrigin"] == "subagent_task" and resets[0]["sessionKey"] == key
    assert seen[2] == ""  # the in-flight entry no longer has the failed text when the retry starts
    segments, closing, _ = _live(recorder.events)
    assert segments == ["Looking."] and closing == "The notes are short."
    rows = manager.registry.thread_store.display_rows(run_id)
    assert not any("explain every" in str(r.get("content")) for r in rows)


@pytest.mark.asyncio
async def test_a_streamed_call_is_bounded_as_a_whole(home):
    class _Stalls(_Streaming):
        async def chat_stream(self, messages, **kwargs):
            yield LLMResponse(content="Partial ", finish_reason="")
            await asyncio.sleep(30)
            yield LLMResponse(content=None, finish_reason="stop")

    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Stalls([])
    manager = _manager(home, home, provider, sink=hub)
    stream = ChildStream(hub, run_id="r1", session_key="subagent:t1", thread_id="t1", parent_session_key=CHAT,
                         turn=1, trigger="spawn", title="T", mark={}, model=MODEL, brief="b",
                         with_conversation=False, access=child_event_access(CHAT))
    await stream.start([])
    started = time.monotonic()
    response = await manager._stream_model("r1", stream, [{"role": "user", "content": "x"}], tools=None,
                                           model=MODEL, max_tokens=100, timeout=0.2)
    assert time.monotonic() - started < 5
    assert response.finish_reason == "error" and "timed out" in response.content
    assert response.partial_content_delivered is True
    await hub.drained()
    assert [e[2].get("state") or e[0] for e in recorder.events] == ["run_started", "agent", "stream_reset"]
    assert inflight.get("subagent:t1")["text"] == ""


@pytest.mark.asyncio
async def test_a_turn_stopped_mid_reply_ends_with_one_aborted_final(tmp_path, home, every_token):
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Streaming([LLMResponse(content="This reply is never finished.")], size=4)
    provider.pause(1, 2)
    manager = _manager(home, tmp_path, provider, sink=hub)
    run_id = await _spawn(manager)
    await provider.paused[(1, 2)].wait()
    await manager.cancel(run_id)
    record = await _settle(manager, run_id, hub)
    assert record.outcome == "cancelled"
    terminals = [e[2] for e in recorder.events if e[2].get("state") in ("final", "error")]
    assert len(terminals) == 1 and terminals[0]["aborted"] is True
    assert inflight.get(f"subagent:{run_id}") is None
    rows = manager.registry.thread_store.display_rows(run_id)
    assert not any("never finished" in str(r.get("content")) for r in rows)


# ── A crash mid-stream ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_crash_mid_stream_persists_nothing_and_the_restart_redoes_the_call_once(tmp_path, every_token):
    from tests.test_subagent_threads import _crash_with_thread

    first = _Recorder()
    first_hub = SubagentStreamHub(lambda: [first])
    crashed = _Streaming([LLMResponse(content="Half of this reply streams before the crash.",
                                      usage={"prompt_tokens": 50, "completion_tokens": 9})], size=5, gap=0.001)
    crashed.pause(1, 4)
    manager = _manager(tmp_path / "a", tmp_path, crashed, sink=first_hub)
    run_id = await _spawn(manager)
    key = f"subagent:{run_id}"
    await crashed.paused[(1, 4)].wait()
    partial = "Half of this reply s"  # four chunks of five
    await _until(lambda: (inflight.get(key) or {}).get("text") == partial)
    await first_hub.drained()
    assert "".join(d["data"]["text"] for n, _k, d in first.events if n == "agent") == partial
    # What a crash now leaves: the step, no reply; nothing of the partial text anywhere on disk.
    path = manager.registry.checkpoint_path(run_id)
    records = read_checkpoint(path)
    assert [r["type"] for r in records] == ["header", "step"]
    assert "Half of this" not in path.read_text()
    assert not manager.registry.thread_store.display_rows(run_id)
    reference = manager.registry.get(run_id)
    assert not any((reference.usage or {}).values())  # the call's tokens never arrived
    # The process dies here (its run is abandoned; its home is left as it is).
    await manager.cancel(run_id)
    await _settle(manager, run_id)
    inflight._runs.clear()

    home2 = tmp_path / "home2"
    _crash_with_thread(home2, reference, records)
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    provider = _Streaming([LLMResponse(content="The whole reply, made again after the restart.",
                                       usage={"prompt_tokens": 50, "completion_tokens": 11})], size=5, gap=0.001)
    resumed = SubagentManager(provider=provider, workspace=tmp_path, bus=MessageBus(),
                              registry=SubagentRegistry(home2 / "runs.json"), state_dir=tmp_path,
                              max_iterations=10)
    resumed.resume_enabled = True
    resumed.stream_sink = hub
    resumed.resume_pending()
    record = await _settle(resumed, run_id, hub)
    assert record.outcome == "ok", record.error
    # The call was made once more, with exactly the list the crashed call had.
    assert provider.kinds == ["stream"]
    restored = restore_child_state(records, annotate=annotate_search_repetition)
    assert provider.sent[0] == restored.messages == crashed.sent[0]
    started = [e[2] for e in recorder.events if e[2].get("state") == "run_started"]
    assert len(started) == 1 and started[0]["resumed"] is True
    _segments, closing, counts = _live(recorder.events)
    assert closing == "The whole reply, made again after the restart." and counts[-1] > 1
    assert [e[2]["state"] for e in recorder.events if e[2].get("state") in ("final", "error")] == ["final"]
    # One closing reply saved; tokens of the call that was made, once.
    rows = resumed.registry.thread_store.display_rows(run_id)
    assistant = [r["content"] for r in rows if r.get("role") == "assistant"]
    assert assistant == ["The whole reply, made again after the restart."]
    assert record.usage["input"] == 50 and record.usage["output"] == 11


# ── Coalescing and backpressure ──────────────────────────────────────────────


class _Slow:
    """A watcher whose deliveries wait until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.started = 0

    def subagent_stream_watched(self, session_key: str) -> bool:
        return True

    def prepare_subagent_stream(self, event: str, session_key: str, data: dict[str, Any]):
        frame = (event, json.loads(json.dumps(data)))

        async def deliver() -> None:
            self.started += 1
            await self.release.wait()
            self.events.append(frame)

        return deliver


def _child_stream(hub: Any, key: str = "subagent:t1") -> ChildStream:
    return ChildStream(hub, run_id="r1", session_key=key, thread_id=key.split(":", 1)[1],
                       parent_session_key=CHAT, turn=1, trigger="spawn", title="T", mark={}, model=MODEL,
                       brief="b", with_conversation=False, access=child_event_access(CHAT))


@pytest.mark.asyncio
async def test_small_deltas_are_coalesced_and_the_last_ones_are_not_held(home):
    recorder = _Recorder()
    hub = SubagentStreamHub(lambda: [recorder])
    stream = _child_stream(hub)
    await stream.start([])
    await hub.drained()
    stream.begin_reply()
    tokens = [f"t{i} " for i in range(500)]
    for token in tokens:
        stream.stream_text(token)
    # Fed in one go: the first token goes out, the rest waits for the interval.
    await hub.drained()
    agents = [d for n, _k, d in recorder.events if n == "agent"]
    assert len(agents) == 1
    # Nothing more arrives: the held tokens go out on their own within the interval.
    await asyncio.sleep(subagent_stream._DELTA_INTERVAL_SECONDS * 3)
    await hub.drained()
    agents = [d["data"]["text"] for n, _k, d in recorder.events if n == "agent"]
    assert len(agents) == 2 and "".join(agents) == "".join(tokens)
    assert inflight.get("subagent:t1")["text"] == "".join(tokens)
    stream.answer("".join(tokens))  # the recorded reply: nothing more to send
    await hub.drained()
    assert len([1 for n, *_ in recorder.events if n == "agent"]) == 2


@pytest.mark.asyncio
async def test_a_slow_watcher_never_holds_the_child_and_its_queue_stays_small(home, monkeypatch):
    monkeypatch.setattr(subagent_stream, "_DELTA_INTERVAL_SECONDS", 0.001)
    slow = _Slow()
    hub = SubagentStreamHub(lambda: [slow])
    stream = _child_stream(hub)
    await stream.start([])
    stream.begin_reply()
    tokens = [f"w{i} " for i in range(3_000)]
    started = time.monotonic()
    deepest = 0
    for i, token in enumerate(tokens):
        stream.stream_text(token)  # synchronous: the child never waits for a socket
        if i % 50 == 0:
            await asyncio.sleep(0.002)  # the provider's own pace
        deepest = max(deepest, hub.backlog("subagent:t1"))
    stream.end_reply()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    # The watcher took the first event and never finished it; the queue held
    # about one delta of the reply, never the reply token by token.
    assert deepest <= 3 and hub.dropped == 0
    assert inflight.get("subagent:t1")["text"] == "".join(tokens)
    slow.release.set()
    await hub.drained()
    text = "".join(d["data"]["text"] for n, d in slow.events if n == "agent")
    assert text == "".join(tokens)


@pytest.mark.asyncio
async def test_a_long_reply_held_back_goes_out_at_the_size_cap(home, monkeypatch):
    monkeypatch.setattr(subagent_stream, "_DELTA_MAX_CHARS", 100)
    slow = _Slow()
    hub = SubagentStreamHub(lambda: [slow])
    stream = _child_stream(hub)
    await stream.start([])
    stream.begin_reply()
    stream.stream_text("x")  # out at once; the run_started is still being delivered
    for _ in range(250):
        stream.stream_text("y")
    # Behind: held until 100 characters piled up, then sent whatever the backlog.
    assert hub.backlog("subagent:t1") >= 2
    slow.release.set()
    await asyncio.sleep(subagent_stream._DELTA_INTERVAL_SECONDS * 3)
    stream.end_reply()
    await hub.drained()
    text = "".join(d["data"]["text"] for n, d in slow.events if n == "agent")
    assert text == "x" + "y" * 250


@pytest.mark.asyncio
async def test_a_streamed_run_with_a_stalled_watcher_finishes_on_time(tmp_path, home):
    (tmp_path / "notes.txt").write_text("notes")
    slow = _Slow()
    hub = SubagentStreamHub(lambda: [slow])
    long_reply = "word " * 4_000
    provider = _Streaming([_read("c1", tmp_path / "notes.txt", text="Reading " * 200),
                           LLMResponse(content=long_reply, usage={"prompt_tokens": 1, "completion_tokens": 1})],
                          size=7)
    manager = _manager(home, tmp_path, provider, sink=hub)
    started = time.monotonic()
    run_id = await _spawn(manager)
    await _until(lambda: manager.registry.get(run_id).ended_at is not None and not manager._running_tasks)
    assert time.monotonic() - started < 5.0
    assert manager.registry.get(run_id).outcome == "ok"
    assert hub.dropped == 0
    slow.release.set()
    await hub.drained()
    agent_text = "".join(d["data"]["text"] for n, d in slow.events if n == "agent")
    assert agent_text == "Reading " * 200 + long_reply
    assert slow.events[-1][1]["state"] == "final"


# ── The shared assembly ──────────────────────────────────────────────────────


def test_the_shared_assembly_gives_what_a_blocking_call_gives():
    calls = [ToolCallRequest(id="c1", name="read_file", arguments={"path": "a"},
                             extra_content={"google": {"thought_signature": "s"}})]
    reply = StreamedReply()
    for chunk in (LLMResponse(content="Let ", finish_reason=""), LLMResponse(content="me.", finish_reason=""),
                  LLMResponse(content=None, tool_calls=calls, finish_reason="tool_calls",
                              usage={"prompt_tokens": 3, "completion_tokens": 2},
                              provider_state={"k": 1}, provider_replay={"items": [1]})):
        reply.feed(chunk)
    response = reply.response()
    assert response.content == "Let me." and response.tool_calls == calls
    assert response.finish_reason == "tool_calls" and response.usage == {"prompt_tokens": 3, "completion_tokens": 2}
    assert response.provider_state == {"k": 1} and response.provider_replay == {"items": [1]}
    assert response.partial_content_delivered is True

    error = StreamedReply()
    assert error.feed(LLMResponse(content="Error calling LLM: boom", finish_reason="error")) == ""
    assert error.text == "" and error.response().content == "Error calling LLM: boom"
    assert error.response().partial_content_delivered is False

    cut = StreamedReply()
    cut.feed(LLMResponse(content="half", finish_reason=""))
    stopped = cut.interrupted("aborted")
    assert stopped.content == "half" and stopped.finish_reason == "aborted" and not stopped.tool_calls

    nothing = StreamedReply()
    assert nothing.response().content is None and nothing.response().finish_reason == "stop"
