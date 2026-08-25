from __future__ import annotations

import pytest

from flowly.agent.loop import resolve_capability_disabled_tools
from flowly.agent.loop import AgentLoop
from flowly.bus.queue import MessageBus
from flowly.channels import feature_rpc
from flowly.cli.gateway_cmd import _local_runtime_ready_payload
from flowly.config.schema import Config
from flowly.providers.base import LLMProvider, LLMResponse
from flowly.runtime_capabilities import (
    RuntimeRole,
    resolve_runtime_capabilities,
)


class _NoopProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    def get_default_model(self) -> str:
        return "test/model"

    async def chat(self, *args, **kwargs) -> LLMResponse:
        raise AssertionError("No model call expected")


def test_positive_tool_grant_becomes_hard_deny_list() -> None:
    resolved = resolve_capability_disabled_tools(
        {"read_file", "memory_search", "system", "exec", "message_profile"},
        ["message_profile"],
        ["read_file", "memory_search"],
    )
    assert resolved == ["exec", "message_profile", "system"]


def test_missing_positive_grant_preserves_existing_policy() -> None:
    original = ["message_profile"]
    assert resolve_capability_disabled_tools({"read_file"}, original, None) is original


def test_local_runtime_handshake_advertises_required_security_contract() -> None:
    payload = _local_runtime_ready_payload(
        profile="alpha",
        host="127.0.0.1",
        port=12345,
        token="x" * 48,
        pid=42,
        instance_id="runtime-1",
    )
    assert payload["protocolVersion"] == 2
    assert set(payload["capabilities"]) == {
        "profile-rpc-v2",
        "allowed-tools-v1",
        "manager-lease-v2",
        "profile-cron-v1",
    }


def test_runtime_capabilities_keep_shared_services_on_primary() -> None:
    primary = resolve_runtime_capabilities(profile_name="default")
    named = resolve_runtime_capabilities(profile_name="research")

    assert primary.role is RuntimeRole.PRIMARY
    assert primary.owns_shared_board is True
    assert primary.owns_flowlets is True
    assert primary.dispatches_profile_tasks is True
    assert primary.accepts_profile_tasks is False

    assert named.role is RuntimeRole.NAMED_PROFILE
    assert named.owns_shared_board is False
    assert named.owns_flowlets is False
    assert named.dispatches_profile_tasks is False
    assert named.accepts_profile_tasks is True


def test_primary_role_cannot_be_forged_for_named_profile() -> None:
    with pytest.raises(ValueError, match="default profile"):
        resolve_runtime_capabilities(
            profile_name="research",
            role=RuntimeRole.PRIMARY,
        )


def test_named_agent_loop_does_not_open_board_or_flowlet_stores(
    tmp_path,
    monkeypatch,
) -> None:
    profile_home = tmp_path / "profiles" / "research"
    workspace = profile_home / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("FLOWLY_HOME", str(profile_home))
    capabilities = resolve_runtime_capabilities(profile_name="research")

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_NoopProvider(),
        workspace=workspace,
        state_dir=profile_home,
        main_config=Config(),
        runtime_capabilities=capabilities,
    )

    assert loop._board_store is None
    assert loop._board_orchestrator is None
    assert loop._flowlet_store is None
    assert "flowlet" not in loop.tools.tool_names
    assert not any(name.startswith("board_") for name in loop.tools.tool_names)
    assert not (profile_home / "board.db").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["board.snapshot", "flowlets.list"])
async def test_named_runtime_feature_rpc_rejects_primary_surfaces(
    method,
    monkeypatch,
) -> None:
    capabilities = resolve_runtime_capabilities(profile_name="research")
    monkeypatch.setattr(
        "flowly.runtime_capabilities.resolve_runtime_capabilities",
        lambda: capabilities,
    )

    with pytest.raises(feature_rpc.FeatureRpcError) as raised:
        await feature_rpc.dispatch(method, {})

    assert raised.value.code == "UNAVAILABLE"
