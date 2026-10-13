"""Opening a subagent shows what it did: brief, steps, tools, result.

The child's conversation used to live only in memory and vanish when the run
ended; the owner could see a final result and a list of tool names, nothing
else. Each step is now appended to the run's transcript, which
``subagents.transcript`` pages through by sequence number for a client to
render — and which is kept, and pruned, together with the run.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from flowly.agent.subagent import SubagentManager
from flowly.agent.subagent_registry import SubagentRegistry
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.channels.feature_rpc import FeatureRpcError, dispatch
from flowly.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path / "home"))


class _Provider(LLMProvider):
    def __init__(self, folder: str) -> None:
        super().__init__(api_key="unused")
        self.folder = folder
        self.calls = 0

    def get_default_model(self) -> str:
        return "fake/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="Önce klasöre bakıyorum.",
                tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": self.folder})],
            )
        return LLMResponse(content="Klasörde iki dosya var.")


async def _run(tmp_path) -> tuple[SubagentManager, str]:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    manager = SubagentManager(
        provider=_Provider(str(tmp_path)), workspace=tmp_path, bus=MessageBus(),
        registry=SubagentRegistry(tmp_path / "runs.json"), state_dir=tmp_path,
    )
    await manager.spawn("Klasördeki dosyaları say.", wait=True, origin="agent",
                        context="**Owner:** dosyaları say")
    return manager, manager.registry.all()[0].run_id


@pytest.mark.asyncio
async def test_every_step_of_a_run_is_recorded_in_order(tmp_path):
    manager, run_id = await _run(tmp_path)
    page = manager.registry.read_transcript(run_id)
    kinds = [item["kind"] for item in page["items"]]
    assert kinds == ["brief", "assistant", "tool", "result"]
    brief, step, tool, result = page["items"]
    assert brief["content"] == "Klasördeki dosyaları say." and brief["withConversation"] is True
    assert step["content"] == "Önce klasöre bakıyorum."
    assert step["toolCalls"][0]["name"] == "list_dir"
    assert tool["name"] == "list_dir" and tool["status"] == "ok"
    assert result == {**result, "status": "ok", "content": "Klasörde iki dosya var."}
    assert [item["seq"] for item in page["items"]] == [1, 2, 3, 4]
    assert page["done"] is True


@pytest.mark.asyncio
async def test_a_client_polls_from_its_last_sequence_number(tmp_path):
    manager, run_id = await _run(tmp_path)
    first = manager.registry.read_transcript(run_id, after=0, limit=2)
    assert [i["seq"] for i in first["items"]] == [1, 2] and first["more"] is True
    rest = manager.registry.read_transcript(run_id, after=first["nextSeq"])
    assert [i["seq"] for i in rest["items"]] == [3, 4] and rest["more"] is False


@pytest.mark.asyncio
async def test_the_transcript_rpc_returns_the_run_and_its_steps(tmp_path, monkeypatch):
    manager, run_id = await _run(tmp_path)
    monkeypatch.setattr(feature_rpc, "_subagent_registry", lambda: manager.registry)
    result, _ = await dispatch("subagents.transcript", {"runId": run_id, "after": 1})
    assert result["task"]["runId"] == run_id
    assert result["task"]["childSessionKey"] == f"subagent:{run_id}"
    assert [i["kind"] for i in result["items"]] == ["assistant", "tool", "result"]
    with pytest.raises(FeatureRpcError):
        await dispatch("subagents.transcript", {"runId": run_id, "after": -1})


@pytest.mark.asyncio
async def test_transcripts_are_kept_for_weeks_and_pruned_with_their_run(tmp_path):
    manager, run_id = await _run(tmp_path)
    registry = manager.registry
    path = registry._transcript_path(run_id)
    assert path.exists()

    # A week old: still there.
    registry._runs[run_id].ended_at = time.time() - 7 * 86_400
    with registry._lock:
        registry._write_locked()
    assert SubagentRegistry(tmp_path / "runs.json").read_transcript(run_id)["items"]

    # Older than the retention window: the run and its transcript go together.
    registry._runs[run_id].ended_at = time.time() - 40 * 86_400
    with registry._lock:
        registry._write_locked()
    reopened = SubagentRegistry(tmp_path / "runs.json")
    assert reopened.all() == []
    with reopened._lock:
        reopened._write_locked()
    assert not path.exists()


@pytest.mark.asyncio
async def test_polling_resumes_where_it_left_off_and_sees_new_steps(tmp_path):
    manager, run_id = await _run(tmp_path)
    registry = manager.registry
    first = registry.read_transcript(run_id, after=0, limit=2)
    # A later step written after the client's last poll.
    registry._runs[run_id].ended_at = None
    registry.append_transcript(run_id, {"kind": "message_in", "from": "orchestrator", "content": "late"})
    rest = registry.read_transcript(run_id, after=first["nextSeq"])
    assert [i["seq"] for i in rest["items"]] == [3, 4, 5]
    assert rest["items"][-1]["content"] == "late"
    # A cursor the cache does not hold still reads correctly from the start.
    again = registry.read_transcript(run_id, after=1, limit=1)
    assert [i["seq"] for i in again["items"]] == [2]


@pytest.mark.asyncio
async def test_an_old_short_id_still_opens_the_transcript(tmp_path, monkeypatch):
    manager, run_id = await _run(tmp_path)
    monkeypatch.setattr(feature_rpc, "_subagent_registry", lambda: manager.registry)
    result, _ = await dispatch("subagents.transcript", {"runId": run_id[:8]})
    assert result["runId"] == run_id and len(result["items"]) == 4
