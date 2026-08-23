from flowly.agent.loop import resolve_capability_disabled_tools
from flowly.cli.gateway_cmd import _local_runtime_ready_payload


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
