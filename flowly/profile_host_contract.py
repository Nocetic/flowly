"""Wire contract and validation for remote isolated-profile control."""

from __future__ import annotations

import json
from typing import Any


class ProfileHostError(RuntimeError):
    """Stable error returned by the profile-host RPC boundary."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


MAX_REQUEST_BYTES = 40 * 1024 * 1024
MAX_PROFILE_MESSAGE_CHARS = 32_000
MAX_PROFILE_HOPS = 3
MAX_ATTACHMENT_B64_CHARS = 34 * 1024 * 1024

# Deliberately excludes config/secrets, arbitrary CLI execution, policy
# mutation, and MCP/skill installation. Those remain on the owning host.
PROFILE_RPC_TIMEOUTS: dict[str, int] = {
    "provider.list": 30_000,
    "provider.active": 30_000,
    "model.list": 60_000,
    "sessions.list": 30_000,
    "sessions.delete": 30_000,
    "sessions.model.get": 30_000,
    "sessions.model.set": 30_000,
    "chat.history": 30_000,
    "chat.inflight": 30_000,
    "chat.send": 60_000,
    "chat.abort": 30_000,
    "exec.approval.list": 30_000,
    "exec.approval.resolve": 30_000,
    "agent.clarify.list": 30_000,
    "agent.clarify.resolve": 30_000,
    "plan.get": 30_000,
    "plan.resolve": 30_000,
    "plan.resume": 60_000,
    "plan.mode.get": 30_000,
    "plan.mode.set": 30_000,
    "goal.get": 30_000,
    "goal.pause": 30_000,
    "goal.resume": 60_000,
    "goal.stop": 30_000,
    "commands.list": 30_000,
    "cron.list": 30_000,
    "cron.add": 30_000,
    "cron.update": 30_000,
    "cron.remove": 30_000,
    "cron.run": 60_000,
    "cron.output": 30_000,
}


def validate_profile_rpc(method: Any, params: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(method, str) or method not in PROFILE_RPC_TIMEOUTS:
        raise ProfileHostError(
            "METHOD_NOT_ALLOWED",
            "This operation is not available through the profile host.",
        )
    if params is None:
        value: dict[str, Any] = {}
    elif isinstance(params, dict):
        value = dict(params)
    else:
        raise ProfileHostError("INVALID_PARAMS", "Profile request parameters are invalid.")
    try:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    except (TypeError, ValueError) as exc:
        raise ProfileHostError("INVALID_PARAMS", "Profile request parameters are invalid.") from exc
    if size > MAX_REQUEST_BYTES:
        raise ProfileHostError("REQUEST_TOO_LARGE", "Profile request is too large.")
    if method == "chat.send":
        # Remote clients may upload bytes, but they may never select paths or
        # browser registrations on the machine that owns the bot host.
        if value.get("cwd") is not None or value.get("browserAccess") is not None:
            raise ProfileHostError(
                "REMOTE_HOST_ACCESS_DENIED",
                "Remote bot messages cannot select host folders or browser sessions.",
            )
        attachments = value.get("attachments")
        if attachments is None:
            attachments = []
        if not isinstance(attachments, list):
            raise ProfileHostError("INVALID_PARAMS", "Attachments must be an array.")
        if len(attachments) > 10:
            raise ProfileHostError("INVALID_PARAMS", "A message can include up to 10 files.")
        for attachment in attachments:
            if not isinstance(attachment, dict):
                raise ProfileHostError("INVALID_PARAMS", "Attachment is invalid.")
            if attachment.get("filePath"):
                raise ProfileHostError(
                    "REMOTE_FILE_PATH_DENIED",
                    "Remote profile messages must upload file content, not host file paths.",
                )
            content = attachment.get("content")
            cdn_url = attachment.get("cdnUrl")
            if content is not None and (
                not isinstance(content, str) or len(content) > MAX_ATTACHMENT_B64_CHARS
            ):
                raise ProfileHostError("INVALID_PARAMS", "Attachment content is invalid or too large.")
            if cdn_url is not None and (
                not isinstance(cdn_url, str)
                or len(cdn_url) > 4096
                or not cdn_url.startswith(("https://", "http://"))
            ):
                raise ProfileHostError("INVALID_PARAMS", "Attachment URL is invalid.")
            if not content and not cdn_url:
                raise ProfileHostError(
                    "INVALID_PARAMS",
                    "Remote attachments must include uploaded content or a media URL.",
                )
        message = value.get("message", "")
        if not isinstance(message, str) or len(message) > MAX_PROFILE_MESSAGE_CHARS:
            raise ProfileHostError(
                "INVALID_PARAMS",
                "A bot message can contain up to 32,000 characters.",
            )
        if not message and not attachments:
            raise ProfileHostError("INVALID_PARAMS", "A bot message cannot be empty.")
    return method, value


def bounded_timeout(method: str, requested: Any) -> float:
    maximum = PROFILE_RPC_TIMEOUTS[method]
    if isinstance(requested, (int, float)) and not isinstance(requested, bool):
        milliseconds = max(1_000, min(int(requested), maximum))
    else:
        milliseconds = maximum
    return milliseconds / 1000
