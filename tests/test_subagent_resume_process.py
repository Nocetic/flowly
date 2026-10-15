"""Real processes, real SIGKILL: a subagent survives its gateway being killed.

A child process runs a SubagentManager with a scripted provider and tools
that only note they ran. It is SIGKILLed at a chosen point — inside a tool
call, or while the model is answering — and a new process recovers the
home. Checked from outside: no tool call ran twice, the run finished once
with one notice, the continued run was told plainly about the cut-off
call, and two processes recovering at the same moment continue it once.
A call that kills its process every time ends after three continues.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import flowly

pytestmark = pytest.mark.skipif(os.name != "posix", reason="needs POSIX signals")

_CHILD = textwrap.dedent('''
    import asyncio, json, os, signal, sys
    from dataclasses import asdict
    from pathlib import Path

    from flowly.agent.subagent import SubagentManager
    from flowly.agent.subagent_registry import SubagentRegistry
    from flowly.agent.tools.filesystem import WriteFileTool
    from flowly.bus.queue import MessageBus
    from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    home, mode, kill = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    work = home.parent
    out = work / f"out-{os.getpid()}.json"
    side = work / "side_effects.log"

    async def write(self, path, content, **kwargs):
        with side.open("a") as stream:
            stream.write(content + "\\n")
            stream.flush()
            os.fsync(stream.fileno())
        if kill in ("mid_tool", "every_tool") and (kill == "every_tool" or content == "W1"):
            os.kill(os.getpid(), signal.SIGKILL)
        return f"Wrote {path}"

    WriteFileTool.execute = write

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(api_key="unused")
            self.sent = []

        def get_default_model(self):
            return "m"

        async def chat(self, **kwargs):
            self.sent.append(kwargs["messages"])
            out.write_text(json.dumps({"sent": self.sent}))
            tool_results = [m for m in kwargs["messages"] if m.get("role") == "tool"]
            if kill == "every_tool":
                return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                    id=f"k{len(self.sent)}", name="write_file",
                    arguments={"path": str(work / "x"), "content": f"K{os.getpid()}"})])
            if not tool_results:
                return LLMResponse(content=None, tool_calls=[
                    ToolCallRequest(id="c1", name="write_file", arguments={"path": str(work / "a"), "content": "W1"}),
                    ToolCallRequest(id="c2", name="write_file", arguments={"path": str(work / "b"), "content": "W2"}),
                ], usage={"prompt_tokens": 10, "completion_tokens": 2})
            if kill == "in_model_call" and mode == "start":
                os.kill(os.getpid(), signal.SIGKILL)
            return LLMResponse(content="All done.", usage={"prompt_tokens": 20, "completion_tokens": 4})

    async def main():
        bus = MessageBus()
        provider = Provider()
        manager = SubagentManager(provider=provider, workspace=work, bus=bus,
                                  registry=SubagentRegistry(home / "runs.json"), state_dir=work)
        manager.resume_enabled = True
        if mode == "start":
            await manager.spawn(task="Write both files", origin_channel="web", origin_chat_id="chat-1",
                                parent_session_key="web:chat-1", origin="agent")
            await asyncio.sleep(30)
            return
        manager.resume_pending()
        notices = []
        for _ in range(300):
            await asyncio.sleep(0.02)
            while not bus.inbound.empty():
                notices.append((await bus.consume_inbound()).content)
            if not manager._running_tasks and manager.registry.all() and all(
                    r.ended_at is not None for r in manager.registry.all()):
                break
        await asyncio.sleep(0.2)
        while not bus.inbound.empty():
            notices.append((await bus.consume_inbound()).content)
        records = [asdict(r) for r in manager.registry.all()]
        out.write_text(json.dumps({"sent": provider.sent, "notices": notices, "records": records}))

    asyncio.run(main())
''')


def _spawn(tmp_path: Path, mode: str, kill: str) -> subprocess.Popen:
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    env = dict(os.environ, FLOWLY_HOME=str(tmp_path / "home"),
               PYTHONPATH=str(Path(flowly.__file__).resolve().parents[1]))
    return subprocess.Popen([sys.executable, str(script), str(tmp_path / "home"), mode, kill],
                            cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _killed(process: subprocess.Popen) -> None:
    _, err = process.communicate(timeout=60)
    assert process.returncode == -signal.SIGKILL, err.decode(errors="replace")[-2000:]


def _recovered(tmp_path: Path, process: subprocess.Popen) -> dict:
    _, err = process.communicate(timeout=60)
    assert process.returncode == 0, err.decode(errors="replace")[-2000:]
    return json.loads((tmp_path / f"out-{process.pid}.json").read_text())


def _side_effects(tmp_path: Path) -> list[str]:
    path = tmp_path / "side_effects.log"
    return path.read_text().split() if path.exists() else []


@pytest.mark.parametrize("kill", ["mid_tool", "in_model_call"])
def test_a_sigkilled_gateways_subagent_continues_in_the_next_process(tmp_path, kill):
    (tmp_path / "home").mkdir()
    _killed(_spawn(tmp_path, "start", kill))
    before = _side_effects(tmp_path)
    assert before == (["W1"] if kill == "mid_tool" else ["W1", "W2"])

    result = _recovered(tmp_path, _spawn(tmp_path, "recover", "none"))

    assert _side_effects(tmp_path) == before  # no call ran twice
    (record,) = result["records"]
    assert record["outcome"] == "ok" and record["resumed_at"]
    assert record["resume_count"] == 1
    assert record["usage"]["input"] == 30 and record["usage"]["output"] == 6
    assert len(result["notices"]) == 1 and "All done." in result["notices"][0]
    sent = result["sent"][0]
    tools = {m["tool_call_id"]: m["content"] for m in sent if m.get("role") == "tool"}
    if kill == "mid_tool":
        assert tools["c1"].startswith("Interrupted: Flowly restarted while this was writing")
        assert tools["c2"].startswith("Not run: Flowly restarted before this call started")
    else:
        assert tools == {"c1": f"Wrote {tmp_path / 'a'}", "c2": f"Wrote {tmp_path / 'b'}"}
    assert not list((tmp_path / "home" / "runs-checkpoints").glob("*.jsonl"))


def test_two_processes_recovering_at_once_continue_the_run_once(tmp_path):
    (tmp_path / "home").mkdir()
    _killed(_spawn(tmp_path, "start", "mid_tool"))
    first, second = _spawn(tmp_path, "recover", "none"), _spawn(tmp_path, "recover", "none")
    results = [_recovered(tmp_path, first), _recovered(tmp_path, second)]
    assert _side_effects(tmp_path) == ["W1"]
    assert sorted(len(r["sent"]) for r in results) == [0, 1]
    assert sum(len(r["notices"]) for r in results) == 1


def test_a_call_that_kills_its_process_every_time_ends_after_three_continues(tmp_path):
    (tmp_path / "home").mkdir()
    _killed(_spawn(tmp_path, "start", "every_tool"))
    for _ in range(3):
        _killed(_spawn(tmp_path, "recover", "every_tool"))
    result = _recovered(tmp_path, _spawn(tmp_path, "recover", "none"))
    (record,) = result["records"]
    assert record["outcome"] == "interrupted" and record["error_code"] == "resume_failed"
    assert len(result["notices"]) == 1 and "restarted 3 times" in result["notices"][0]
    # Each process ran the call once; none ran an earlier process's call again.
    effects = _side_effects(tmp_path)
    assert len(effects) == 4 and len(set(effects)) == 4
