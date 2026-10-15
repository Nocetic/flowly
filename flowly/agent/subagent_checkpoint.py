"""A running subagent's checkpoint: enough to continue it after a restart.

The transcript is what the owner reads; it clips arguments and results and
leaves out what the model never shows. The checkpoint is what the provider
saw: one append-only JSON-lines file per running run
(``runs-checkpoints/<sha256(run_id)>.jsonl``, mode 0600) from which the exact
message list can be rebuilt. It exists only while the run is running and is
deleted once its result is durable; the registry also prunes any file whose
run is no longer running. System runs keep none.

Every line is ``{"v": 1, "n": <sequence>, "t": <time>, "type": …, …}``:

=============  =============================================================
``header``     system prompt, first message (brief + forked conversation),
               model, iteration limit, core version
``mail``       a message from the main agent arrived (not yet read)
``step``       iteration k began: messages read from the inbox, the context
               budget its trim used
``model``      the model's reply with tool calls — written BEFORE any of its
               tools run (full arguments, ids, ``extra_content``), with usage
``text``       a reply kept because a message arrived while it was produced
``recovery``   the one-off trim after a context overflow
``tool_start`` a tool is about to run (written before it starts)
``tool``       a tool message as appended (full content), its status and
               what the repetition guard saw
``inbox``      messages a tool call read (agent_message's reply)
``phase``      the summary (grace) call was asked for, and why
``final``      the run's result, before the registry records it
``suspended``  a clean shutdown stopped the run to continue it later
``resumed``    the run continued after a restart
=============  =============================================================

``model``, ``tool_start``, ``mail`` and ``final`` are fsynced: they are the
lines a restart must be able to trust (a call that may have started, a
message the main agent was told was delivered, a result). Others are
flushed; losing one only makes the restart redo a step that had no effect.

``restore_child_state`` replays the lines through the same functions the
live loop uses, so the rebuilt list is the live one, step for step.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from flowly.agent.repetition import RepetitionGuard, mark_repeated

CHECKPOINT_VERSION = 1
# A run whose checkpoint grows past this stops checkpointing and is not
# continued after a restart (typically a run writes 0.3–2 MB).
MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024

GRACE_PROMPT = (
    "You cannot call any more tools. Provide a brief "
    "final summary of what you accomplished and found "
    "so far based on your previous tool results. Do "
    "not call tools — just answer directly."
)


# What a tool call can do, for telling the model plainly what an
# interrupted call may have left behind. Unknown tools get the most careful
# class.
_READ_TOOLS = frozenset({
    "read_file", "list_dir", "web_search", "web_fetch", "web_extract", "tool_search", "tool_describe",
})
_READ_ACTIONS = {
    "knowledge_graph": frozenset({"query", "search", "timeline", "stats"}),
    "artifact": frozenset({"get", "list", "export", "get_versions"}),
}


def tool_class(name: str, arguments: dict[str, Any] | None = None) -> str:
    """"read", "write", "exec", "message" or "change" (may or may not have taken effect)."""
    if name in _READ_TOOLS:
        return "read"
    if name in _READ_ACTIONS and str((arguments or {}).get("action") or "") in _READ_ACTIONS[name]:
        return "read"
    if name == "write_file":
        return "write"
    if name == "exec":
        return "exec"
    if name == "agent_message":
        return "message"
    return "change"


class CheckpointFull(Exception):
    """The checkpoint reached MAX_CHECKPOINT_BYTES."""


class SubagentCheckpoint:
    """One run's checkpoint file. Only the run's owner writes it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._next: int | None = None
        self._size = 0

    def _open(self) -> None:
        """Find where the log ends; cut a line torn by a crash."""
        self._next, self._size = 0, 0
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        end = data.rfind(b"\n") + 1
        if end != len(data):
            with open(self.path, "r+b") as stream:
                stream.truncate(end)
                stream.flush()
                os.fsync(stream.fileno())
            data = data[:end]
        self._size = len(data)
        for raw in reversed(data.splitlines()):
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict) and type(record.get("n")) is int:
                self._next = record["n"] + 1
                break

    def append(self, kind: str, *, durable: bool = False, **fields: Any) -> None:
        if self._next is None:
            self._open()
        record = {"v": CHECKPOINT_VERSION, "n": self._next, "t": round(time.time(), 3),
                  "type": kind, **fields}
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
                + "\n").encode("utf-8")
        if self._size + len(line) > MAX_CHECKPOINT_BYTES:
            raise CheckpointFull(str(self.path))
        created = self._size == 0 and not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, line)
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        if created and durable and os.name == "posix":
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        self._next += 1
        self._size += len(line)

    def read(self) -> list[dict[str, Any]]:
        return read_checkpoint(self.path)

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete a subagent checkpoint")
        self._next, self._size = None, 0


def read_checkpoint(path: Path) -> list[dict[str, Any]]:
    """Every complete record, in order; a torn last line is not a record."""
    if not path.exists():
        return []
    records = []
    with path.open("rb") as stream:
        for raw in stream:
            if not raw.endswith(b"\n"):
                break
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("type"), str):
                records.append(record)
    return records


# ---------------------------------------------------------------------------
# The child's state, and replaying a checkpoint into it
# ---------------------------------------------------------------------------

@dataclass
class OpenCall:
    """A tool call of the last model reply that has no tool message yet."""

    id: str
    name: str            # as the model called it (the protocol name)
    arguments: dict[str, Any] = field(default_factory=dict)
    started: bool = False
    effective: str = ""  # the tool that ran (tool_call resolves to another)
    kind: str = ""       # tool_class() of what ran, as recorded when it started
    # A clean shutdown was recorded after it started (exec's process group
    # was stopped then); otherwise the process died under it.
    stopped: bool = False


@dataclass
class ChildState:
    """What a child run carries from one step to the next."""

    messages: list[dict[str, Any]]
    max_iterations: int
    iteration: int = 0
    input_budget_recovered: bool = False
    model_used_artifact: bool = False
    repetition: RepetitionGuard = field(default_factory=RepetitionGuard)
    stalled: bool = False
    search_sets: dict[tuple[str, ...], int] = field(default_factory=dict)
    # A step began and its model call has not answered yet.
    awaiting_model: bool = False
    # Set once the summary (grace) call was asked for:
    # {"reason", "hitMaxIter", "preserved", "status", "errorCode"}.
    grace: dict[str, Any] | None = None
    # Set once the run's result was decided: {"status", "error", "errorCode", "result"}.
    final: dict[str, Any] | None = None
    # Restored only: the last reply's calls without a tool message, messages
    # from the main agent not read yet, whether the last stop was clean.
    open_calls: list[OpenCall] = field(default_factory=list)
    unread_mail: list[dict[str, str]] = field(default_factory=list)
    suspended: bool = False
    resumes: int = 0


@dataclass
class ResumeRefusal:
    """Why a checkpoint cannot be continued."""

    reason: str   # "missing" | "version" | "corrupt"
    detail: str = ""


def _arguments(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


NOT_RUN_TEXT = (
    "Not run: Flowly restarted before this call started. Nothing was done; "
    "call it again if you still need it."
)


def interrupted_result(call: OpenCall) -> tuple[str, str]:
    """(status, text) answering a call a restart left without a result.

    The call is never run again. The text says what that kind of call may
    have left behind, so the model can check before relying on it.
    """
    if not call.started:
        return "not_run", NOT_RUN_TEXT
    kind = call.kind or tool_class(call.effective or call.name, call.arguments)
    if kind == "read":
        text = (
            "Interrupted: Flowly restarted while this was running and its result was lost. "
            "It changes nothing; call it again if you still need it."
        )
    elif kind == "write":
        path = str(call.arguments.get("path") or "the file")
        text = (
            f"Interrupted: Flowly restarted while this was writing {path}. The file may be "
            "complete, partial or unchanged; read it before relying on it."
        )
    elif kind == "exec":
        if call.stopped:
            text = (
                "Interrupted: this command was started before Flowly restarted and was stopped "
                "when Flowly shut down. It may have finished or been cut off part-way; check its "
                "effects (files, processes) before relying on it or running it again."
            )
        else:
            text = (
                "Interrupted: this command was started before Flowly restarted. It may have "
                "finished, been cut off, or still be running (it runs in its own session); check "
                "its effects (files, processes) before relying on it or running it again."
            )
    elif kind == "message":
        text = (
            "Interrupted: Flowly restarted while this ran. Your message may not have reached "
            "the main agent, and no reply reached this call; a reply that arrives reaches you "
            "as a message from the main agent."
        )
    else:
        text = (
            "Interrupted: Flowly restarted while this was running. It may or may not have taken "
            "effect; check the current state before doing it again, since doing it twice may "
            "duplicate it."
        )
    return "interrupted", text


def main_agent_note(text: str) -> dict[str, Any]:
    return {"role": "user", "content": f"[Message from the main agent]\n{text}"}


def apply_step(
    state: ChildState, iteration: int, notes: list[str], budget: int,
    strip: Callable[..., list[dict[str, Any]]],
    trim: Callable[..., list[dict[str, Any]]],
) -> None:
    """Begin iteration ``iteration``: read the inbox, compact old results.

    The live loop and the replay both go through here, so a rebuilt list is
    compacted exactly as the live one was.
    """
    state.iteration = iteration
    for note in notes:
        state.messages.append(main_agent_note(note))
    # Strip old tool results to save tokens (keep recent, compact old).
    if iteration > 3:
        state.messages = strip(state.messages, keep_last=3, max_old_chars=300)
    if iteration > 7:
        state.messages = strip(state.messages, keep_last=2, max_old_chars=150)
    # Iteration count is a poor proxy for context pressure: measure the
    # actual budget too.
    state.messages = trim(state.messages, budget=budget)
    state.awaiting_model = True


def observe_tool_result(state: ChildState, guard: dict[str, Any] | None, content: str,
                        search: bool, annotate: Callable[..., str]) -> None:
    """Rebuild what the repetition guard and the search memo saw."""
    if search:
        annotate(content, state.search_sets)
    if not guard:
        return
    tool = str(guard.get("tool") or "")
    raw = content
    if guard.get("repeated"):
        prefix = mark_repeated(tool, "")
        raw = content[len(prefix):] if content.startswith(prefix) else content
    if state.repetition.observe(tool, raw):
        state.stalled = state.repetition.stalled


def restore_child_state(
    records: list[dict[str, Any]],
    *,
    strip: Callable[..., list[dict[str, Any]]],
    trim: Callable[..., list[dict[str, Any]]],
    annotate: Callable[..., str],
) -> ChildState | ResumeRefusal:
    """Replay a checkpoint into the state the run had when it stopped.

    Pure: reads nothing but ``records``. The last model reply's calls that
    have no tool message come back in ``open_calls`` — started or not — for
    the caller to answer; none is ever run again.
    """
    if not records:
        return ResumeRefusal("missing")
    if any(record.get("v") != CHECKPOINT_VERSION for record in records):
        versions = sorted({str(record.get("v")) for record in records})
        return ResumeRefusal("version", ",".join(versions))
    header = records[0]
    if header.get("type") != "header":
        return ResumeRefusal("corrupt", "no header")
    try:
        state = ChildState(
            messages=[
                {"role": "system", "content": str(header["systemPrompt"])},
                {"role": "user", "content": str(header["firstMessage"])},
            ],
            max_iterations=max(1, int(header["maxIterations"])),
        )
        mail: dict[str, str] = {}
        consumed: set[str] = set()
        reply_reads: dict[str, list[str]] = {}
        answered: set[str] = set()
        calls: dict[str, OpenCall] = {}
        for record in records[1:]:
            kind = record["type"]
            if kind == "mail":
                mail[str(record["id"])] = str(record["text"])
            elif kind == "step":
                apply_step(state, int(record["iteration"]), [str(n) for n in record.get("notes", [])],
                           int(record["budget"]), strip, trim)
                consumed.update(str(i) for i in record.get("mail", []))
                calls = {}
            elif kind == "model":
                message = record["message"]
                state.messages.append(message)
                state.awaiting_model = False
                calls = {
                    str(call["id"]): OpenCall(
                        id=str(call["id"]), name=str(call["function"]["name"]),
                        arguments=_arguments(call["function"].get("arguments")),
                    )
                    for call in message.get("tool_calls") or []
                }
            elif kind == "text":
                state.messages.append({"role": "assistant", "content": str(record["content"])})
                state.awaiting_model = False
            elif kind == "recovery":
                state.input_budget_recovered = True
                state.messages = strip(state.messages, keep_last=1, max_old_chars=60)
                state.awaiting_model = False
            elif kind == "tool_start":
                call = calls.get(str(record["callId"]))
                if call is not None:
                    call.started = True
                    call.effective = str(record.get("name") or call.name)
                    call.kind = str(record.get("toolClass") or tool_class(call.effective, call.arguments))
                if record.get("name") == "artifact":
                    state.model_used_artifact = True
            elif kind == "tool":
                call_id = str(record["callId"])
                content = str(record["content"])
                state.messages.append({
                    "role": "tool", "tool_call_id": call_id,
                    "name": str(record["protocolName"]), "content": content,
                })
                if record.get("status") not in ("interrupted", "not_run"):
                    # A result a restart wrote never carried a reply to the model.
                    answered.add(call_id)
                calls.pop(call_id, None)
                observe_tool_result(state, record.get("guard"), content, bool(record.get("search")), annotate)
            elif kind == "inbox":
                reply_reads.setdefault(str(record["callId"]), []).extend(str(i) for i in record["ids"])
            elif kind == "phase":
                state.grace = {
                    "reason": record.get("reason"),
                    "hitMaxIter": bool(record.get("hitMaxIter")),
                    "preserved": record.get("preserved"),
                    "status": record.get("status"),
                    "errorCode": record.get("errorCode"),
                }
                state.messages.append({"role": "user", "content": GRACE_PROMPT})
                state.awaiting_model = False
            elif kind == "final":
                state.final = {
                    "status": record["status"], "error": record.get("error"),
                    "errorCode": record.get("errorCode"), "result": str(record.get("result") or ""),
                }
            elif kind == "suspended":
                state.suspended = True
                for call in calls.values():
                    if call.started:
                        call.stopped = True
            elif kind == "resumed":
                state.resumes += 1
                state.suspended = False
            # Other kinds (written by a newer build of the same version)
            # carry nothing the message list depends on.
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return ResumeRefusal("corrupt", type(exc).__name__)
    # A reply a tool call read counts as read only if that call finished;
    # otherwise the reply never reached the model and is read again.
    for call_id, ids in reply_reads.items():
        if call_id in answered:
            consumed.update(ids)
    state.unread_mail = [{"id": mid, "text": text} for mid, text in mail.items() if mid not in consumed]
    state.open_calls = list(calls.values())
    return state
