from __future__ import annotations

import pytest

from flowly.profile_host_contract import (
    PROFILE_RPC_TIMEOUTS,
    ProfileHostError,
    bounded_timeout,
    validate_profile_rpc,
)


def test_mobile_profile_rpc_surface_is_bot_scoped() -> None:
    for method in (
        "chat.send",
        "chat.history",
        "sessions.list",
        "sessions.model.set",
        "cron.list",
        "cron.add",
        "cron.remove",
        "exec.approval.resolve",
        "plan.resolve",
        "plan.resume",
        "plan.mode.set",
        "goal.pause",
        "goal.resume",
        "goal.stop",
    ):
        assert method in PROFILE_RPC_TIMEOUTS

    for forbidden in (
        "config.get",
        "config.set",
        "connections.secret.get",
        "exec.policy.set",
        "mcp.install",
        "skills.install",
        "cli.exec",
    ):
        with pytest.raises(ProfileHostError) as raised:
            validate_profile_rpc(forbidden, {})
        assert raised.value.code == "METHOD_NOT_ALLOWED"


def test_remote_chat_rejects_host_file_paths_and_invalid_attachments() -> None:
    with pytest.raises(ProfileHostError) as path_error:
        validate_profile_rpc("chat.send", {
            "message": "read this",
            "attachments": [{"filePath": "/etc/passwd"}],
        })
    assert path_error.value.code == "REMOTE_FILE_PATH_DENIED"

    with pytest.raises(ProfileHostError, match="up to 10 files"):
        validate_profile_rpc("chat.send", {"attachments": [{}] * 11})

    with pytest.raises(ProfileHostError, match="Attachment is invalid"):
        validate_profile_rpc("chat.send", {"attachments": ["not-an-object"]})

    with pytest.raises(ProfileHostError) as cwd_error:
        validate_profile_rpc("chat.send", {"message": "hello", "cwd": "/tmp/project"})
    assert cwd_error.value.code == "REMOTE_HOST_ACCESS_DENIED"

    with pytest.raises(ProfileHostError, match="cannot be empty"):
        validate_profile_rpc("chat.send", {})


def test_profile_rpc_validation_copies_params_and_bounds_timeouts() -> None:
    original = {"sessionKey": "desktop:chat-1"}
    method, safe = validate_profile_rpc("chat.history", original)
    safe["sessionKey"] = "changed"

    assert method == "chat.history"
    assert original["sessionKey"] == "desktop:chat-1"
    assert bounded_timeout("chat.history", 1) == 1.0
    assert bounded_timeout("chat.history", 999_999) == 30.0
    assert bounded_timeout("chat.history", True) == 30.0


def test_profile_rpc_rejects_non_object_or_non_json_params() -> None:
    with pytest.raises(ProfileHostError) as non_object:
        validate_profile_rpc("chat.history", [])
    assert non_object.value.code == "INVALID_PARAMS"

    with pytest.raises(ProfileHostError) as non_json:
        validate_profile_rpc("chat.history", {"bad": object()})
    assert non_json.value.code == "INVALID_PARAMS"
