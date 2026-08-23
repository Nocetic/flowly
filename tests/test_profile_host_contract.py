from __future__ import annotations

import base64

import pytest

import flowly.profile_host_contract as contract
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
            "sessionKey": "ios:thread-1",
            "message": "read this",
            "attachments": [{"filePath": "/etc/passwd"}],
        })
    assert path_error.value.code == "REMOTE_FILE_PATH_DENIED"

    with pytest.raises(ProfileHostError, match="up to 10 files"):
        validate_profile_rpc("chat.send", {
            "sessionKey": "ios:thread-1",
            "attachments": [{}] * 11,
        })

    with pytest.raises(ProfileHostError, match="Attachment is invalid"):
        validate_profile_rpc("chat.send", {
            "sessionKey": "ios:thread-1",
            "attachments": ["not-an-object"],
        })

    with pytest.raises(ProfileHostError) as cwd_error:
        validate_profile_rpc("chat.send", {
            "sessionKey": "ios:thread-1",
            "message": "hello",
            "cwd": "/tmp/project",
        })
    assert cwd_error.value.code == "REMOTE_HOST_ACCESS_DENIED"

    with pytest.raises(ProfileHostError, match="cannot be empty"):
        validate_profile_rpc("chat.send", {"sessionKey": "ios:thread-1"})


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


def test_remote_chat_sanitizes_inline_attachments() -> None:
    original = {
        "sessionKey": "ios:thread-1",
        "message": "Review this",
        "attachments": [{
            "fileName": "draft.txt",
            "mimeType": "text/plain",
            "content": "SGVsbG8=",
            "thumbnail": "must-not-cross-the-host-boundary",
            "s3Key": "must-not-cross-the-host-boundary",
        }],
    }

    _method, safe = validate_profile_rpc("chat.send", original)

    assert safe["attachments"] == [{
        "fileName": "draft.txt",
        "mimeType": "text/plain",
        "content": "SGVsbG8=",
    }]
    assert original["attachments"][0]["thumbnail"]


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example.com/file.png",
        "https://localhost/file.png",
        "https://127.0.0.1/file.png",
        "https://10.0.0.1/file.png",
        "https://host.internal/file.png",
        "https://user:pass@cdn.example.com/file.png",
        "https://cdn.example.com:8443/file.png",
    ],
)
def test_remote_chat_rejects_non_public_attachment_urls(url: str) -> None:
    with pytest.raises(ProfileHostError) as raised:
        validate_profile_rpc("chat.send", {
            "sessionKey": "ios:thread-1",
            "message": "Review",
            "attachments": [{
                "fileName": "draft.png",
                "mimeType": "image/png",
                "cdnUrl": url,
            }],
        })
    assert raised.value.code == "REMOTE_ATTACHMENT_URL_DENIED"


def test_remote_chat_accepts_public_https_attachment_url() -> None:
    _method, safe = validate_profile_rpc("chat.send", {
        "sessionKey": "ios:thread-1",
        "message": "Review",
        "attachments": [{
            "fileName": "draft.png",
            "mimeType": "image/png",
            "cdnUrl": "https://cdn.example.com/media/draft.png?signature=opaque",
        }],
    })
    assert safe["attachments"][0]["cdnUrl"].startswith("https://")


@pytest.mark.parametrize(
    "params",
    [
        {"message": "missing conversation"},
        {"sessionKey": "", "message": "empty conversation"},
        {"sessionKey": "x" * 257, "message": "long conversation"},
        {"sessionKey": "ios:thread-1", "message": "x", "idempotencyKey": ""},
        {"sessionKey": "ios:thread-1", "message": "x", "idempotencyKey": "x" * 129},
    ],
)
def test_remote_chat_requires_bounded_message_identity(params: dict) -> None:
    with pytest.raises(ProfileHostError) as raised:
        validate_profile_rpc("chat.send", params)
    assert raised.value.code == "INVALID_PARAMS"


def test_remote_chat_rejects_malformed_or_oversized_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common = {
        "sessionKey": "ios:thread-1",
        "message": "Review",
        "attachments": [{
            "fileName": "draft.bin",
            "mimeType": "application/octet-stream",
        }],
    }
    for content in ("not base64!", "data:text/plain,not-base64"):
        params = {**common, "attachments": [{**common["attachments"][0], "content": content}]}
        with pytest.raises(ProfileHostError) as raised:
            validate_profile_rpc("chat.send", params)
        assert raised.value.code == "INVALID_PARAMS"

    monkeypatch.setattr(contract, "MAX_ATTACHMENT_BYTES", 4)
    oversized = b"x" * 5
    params = {
        **common,
        "attachments": [{
            **common["attachments"][0],
            "content": base64.b64encode(oversized).decode("ascii"),
        }],
    }
    with pytest.raises(ProfileHostError) as raised:
        validate_profile_rpc("chat.send", params)
    assert raised.value.code == "REQUEST_TOO_LARGE"


@pytest.mark.parametrize(
    "method, params",
    [
        ("chat.history", {"sessionKey": "cli:private"}),
        ("chat.inflight", {"sessionKey": "desktop:profile-inbox:writer:source"}),
        ("sessions.model.get", {"sessionKey": "cron:job"}),
        ("sessions.delete", {"key": "desktop:profile-inbox:writer:source"}),
    ],
)
def test_remote_profile_rpc_denies_internal_session_namespaces(
    method: str,
    params: dict,
) -> None:
    with pytest.raises(ProfileHostError) as raised:
        validate_profile_rpc(method, params)
    assert raised.value.code == "REMOTE_SESSION_DENIED"


def test_profile_media_windows_are_bounded_and_basename_only() -> None:
    method, safe = validate_profile_rpc("media.read", {
        "mediaId": "generated-image.png",
        "offset": 1024,
        "length": 1024 * 1024,
        "ignored": "removed",
    })
    assert method == "media.read"
    assert safe == {
        "mediaId": "generated-image.png",
        "offset": 1024,
        "length": 1024 * 1024,
    }

    for media_id in ("../secret", ".hidden", "folder/file.png", "bad\n.png"):
        with pytest.raises(ProfileHostError) as raised:
            validate_profile_rpc("media.read", {
                "mediaId": media_id,
                "offset": 0,
                "length": 1,
            })
        assert raised.value.code == "INVALID_PARAMS"

    with pytest.raises(ProfileHostError) as raised:
        validate_profile_rpc("media.read", {
            "mediaId": "image.png",
            "offset": 0,
            "length": 1024 * 1024 + 1,
        })
    assert raised.value.code == "INVALID_PARAMS"
