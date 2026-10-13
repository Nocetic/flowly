"""Tests for the retired specialist RPCs and the manual-spawn feature RPC.

  * The stored specialists and owner-made assistants are gone. An older
    client still asks for them: reads answer "none, retired" and writes say
    plainly that the feature is gone, instead of failing as unknown methods.
  * ``subagents.spawn`` launches a manual background subagent through the
    wired SubagentManager and splits the session key into the origin
    channel/chat so the async result re-enters the right chat.
"""

from __future__ import annotations

import json

import pytest

from flowly.channels import feature_rpc
from flowly.channels.feature_rpc import FeatureRpcError, dispatch


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWLY_HOME", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["subagents.assistants", "assistants.list", "assistants.reload"])
async def test_retired_reads_answer_an_empty_list(home, method) -> None:
    result, _ = await dispatch(method, {})
    assert result == {"assistants": [], "retired": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("subagents.set_model", {"name": "writer", "model": "openrouter/x/y"}),
        ("assistants.write", {"name": "helper", "systemPrompt": "x"}),
        ("assistants.delete", {"name": "helper"}),
    ],
)
async def test_retired_writes_say_the_feature_is_gone(home, method, params) -> None:
    with pytest.raises(FeatureRpcError) as caught:
        await dispatch(method, params)
    assert caught.value.code == "FEATURE_RETIRED"


@pytest.mark.asyncio
async def test_an_owner_made_assistant_file_is_left_on_disk(home) -> None:
    folder = home / "assistants"
    folder.mkdir()
    made = folder / "helper.md"
    made.write_text("---\nname: helper\n---\nbody\n")
    with pytest.raises(FeatureRpcError):
        await dispatch("assistants.delete", {"name": "helper"})
    assert made.read_text() == "---\nname: helper\n---\nbody\n"


class _FakeManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def spawn(self, **kwargs):
        # Mirror the real async (wait=False) envelope.
        self.calls.append(kwargs)
        return json.dumps({"status": "dispatched", "run_id": "run-xyz", "label": "weather"})


class _RejectingManager:
    async def spawn(self, **kwargs):
        return json.dumps({"status": "rejected", "error": "Maximum 5 concurrent subagents."})


@pytest.mark.asyncio
async def test_spawn_without_manager_is_unavailable(home, monkeypatch) -> None:
    monkeypatch.setattr(feature_rpc, "_subagent_manager_provider", None)
    with pytest.raises(FeatureRpcError):
        await dispatch("subagents.spawn", {"task": "do it", "sessionKey": "tui:default"})


@pytest.mark.asyncio
async def test_spawn_splits_session_key_and_runs_async(home) -> None:
    fake = _FakeManager()
    feature_rpc.set_subagent_manager_provider(lambda: fake)
    try:
        res, _ = await dispatch(
            "subagents.spawn", {"task": "research foo", "sessionKey": "tui:default"}
        )
    finally:
        feature_rpc.set_subagent_manager_provider(None)
    assert res["ok"] is True
    assert res["runId"] == "run-xyz"
    assert res["displayName"] == "weather"
    call = fake.calls[0]
    assert call["origin_channel"] == "tui"
    assert call["origin_chat_id"] == "default"
    assert call["wait"] is False
    assert call["label"] == "manual"


@pytest.mark.asyncio
async def test_spawn_requires_task(home) -> None:
    feature_rpc.set_subagent_manager_provider(lambda: _FakeManager())
    try:
        with pytest.raises(FeatureRpcError):
            await dispatch("subagents.spawn", {"task": "   ", "sessionKey": "tui:default"})
    finally:
        feature_rpc.set_subagent_manager_provider(None)


@pytest.mark.asyncio
async def test_spawn_surfaces_manager_rejection(home) -> None:
    feature_rpc.set_subagent_manager_provider(lambda: _RejectingManager())
    try:
        with pytest.raises(FeatureRpcError):
            await dispatch("subagents.spawn", {"task": "y", "sessionKey": "cli:direct"})
    finally:
        feature_rpc.set_subagent_manager_provider(None)
