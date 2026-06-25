"""Tests for the subagent specialist-model + manual-spawn feature RPCs.

Pins the contract the desktop Settings editor and the TUI ``/subagents``
command depend on:

  * ``subagents.assistants`` lists specialists with their default model,
    override, and resolved effective model;
  * ``subagents.set_model`` persists/clears ``agents.assistant_models`` and
    resolves the three-tier policy (override id > inherit > default);
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
async def test_assistants_lists_builtins_with_default_models(home) -> None:
    result, _ = await dispatch("subagents.assistants", {})
    names = {a["name"] for a in result["assistants"]}
    assert {"researcher", "writer", "coder"} <= names
    for a in result["assistants"]:
        # Unset → no override, effective == default.
        assert a["override"] == ""
        assert a["effectiveModel"] == a["defaultModel"]
        assert a["builtin"] is True


@pytest.mark.asyncio
async def test_set_model_concrete_inherit_and_clear(home) -> None:
    # Concrete override.
    res, _ = await dispatch(
        "subagents.set_model", {"name": "writer", "model": "openrouter/x/y"}
    )
    assert res["override"] == "openrouter/x/y"
    assert res["effectiveModel"] == "openrouter/x/y"

    # Inherit → effective resolves to the bot model.
    res, _ = await dispatch("subagents.set_model", {"name": "researcher", "model": "inherit"})
    assert res["override"] == "inherit"
    assert res["effectiveModel"] == res["botModel"]

    # Persisted across reads.
    listing, _ = await dispatch("subagents.assistants", {})
    by_name = {a["name"]: a for a in listing["assistants"]}
    assert by_name["writer"]["override"] == "openrouter/x/y"
    assert by_name["researcher"]["override"] == "inherit"

    # Clear → back to default.
    res, _ = await dispatch("subagents.set_model", {"name": "writer", "model": ""})
    assert res["override"] == ""
    listing, _ = await dispatch("subagents.assistants", {})
    by_name = {a["name"]: a for a in listing["assistants"]}
    assert by_name["writer"]["override"] == ""
    assert by_name["writer"]["effectiveModel"] == by_name["writer"]["defaultModel"]


@pytest.mark.asyncio
async def test_set_model_rejects_unknown_specialist(home) -> None:
    with pytest.raises(FeatureRpcError):
        await dispatch("subagents.set_model", {"name": "nope", "model": "x"})


@pytest.mark.asyncio
async def test_set_model_requires_name(home) -> None:
    with pytest.raises(FeatureRpcError):
        await dispatch("subagents.set_model", {"name": "  ", "model": "x"})


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
