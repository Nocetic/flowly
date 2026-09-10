"""Wire contract and validation for remote isolated-profile control."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit


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
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

_NON_PUBLIC_ATTACHMENT_HOST_SUFFIXES = (
    ".internal",
    ".invalid",
    ".local",
    ".localhost",
    ".test",
    ".home.arpa",
)

# Deliberately excludes raw global config/secrets, arbitrary CLI execution, and
# legacy MCP/skill installation. Modern MCP setup stays in the selected runtime's
# staged owner-confirmation service; it never falls back to direct config writes.
# The small access-policy projection below is safe to
# expose because it accepts only closed enums and a deny-only toolset list.
PROFILE_RPC_TIMEOUTS: dict[str, int] = {
    "memory.editor.list": 30_000,
    "memory.editor.document": 30_000,
    "memory.editor.save": 30_000,
    "mcp.capabilities": 30_000,
    "mcp.connections.list": 30_000,
    "mcp.connections.action": 60_000,
    "mcp.setup.begin": 60_000,
    "mcp.setup.status": 30_000,
    "mcp.setup.pending": 30_000,
    "mcp.setup.confirm": 30_000,
    "mcp.setup.callback": 30_000,
    "mcp.setup.cancel": 30_000,
    "mcp.setup.cancel_request": 30_000,
    "mcp.chat.pending": 30_000,
    "mcp.chat.cancel": 30_000,
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
    "media.read": 30_000,
    "exec.approval.list": 30_000,
    "exec.approval.resolve": 30_000,
    "exec.policy.get": 30_000,
    "exec.policy.set": 30_000,
    "codex.policy.get": 30_000,
    "codex.policy.set": 30_000,
    "tools.access.get": 30_000,
    "tools.access.set": 30_000,
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

_EXEC_SECURITY = {"deny", "allowlist", "full"}
_EXEC_ASK = {"off", "on-miss", "always"}
_CODEX_APPROVAL = {"on-request", "never", "auto-review", "granular"}
_CODEX_SANDBOX = {"read-only", "workspace-write", "full-access"}


_REMOTE_SESSION_PREFIXES = ("desktop:", "web:", "ios:")
_INTERNAL_PROFILE_SESSION_PREFIXES = (
    "desktop:profile-inbox:",
    "desktop:profile-room:",
    "desktop:profile-task:",
)


def is_internal_profile_session(value: Any) -> bool:
    """Return whether a session belongs to host-only profile orchestration."""
    return isinstance(value, str) and value.startswith(_INTERNAL_PROFILE_SESSION_PREFIXES)


def _validate_session_key(value: Any, *, required: bool = False) -> None:
    if value is None and not required:
        return
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise ProfileHostError(
            "INVALID_PARAMS",
            "Profile conversation identity is invalid.",
        )
    if (
        not value.startswith(_REMOTE_SESSION_PREFIXES)
        or is_internal_profile_session(value)
    ):
        raise ProfileHostError(
            "REMOTE_SESSION_DENIED",
            "This profile conversation is not available to remote clients.",
        )


def _validate_remote_attachment_url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) > 4096
        or any(ord(char) < 0x20 for char in value)
    ):
        raise ProfileHostError("INVALID_PARAMS", "Attachment URL is invalid.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProfileHostError("INVALID_PARAMS", "Attachment URL is invalid.") from exc
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
        or hostname == "localhost"
        or "." not in hostname
        or hostname.endswith(_NON_PUBLIC_ATTACHMENT_HOST_SUFFIXES)
    ):
        raise ProfileHostError(
            "REMOTE_ATTACHMENT_URL_DENIED",
            "Remote bot attachments must use a public HTTPS media URL.",
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ProfileHostError(
                "REMOTE_ATTACHMENT_URL_DENIED",
                "Remote bot attachments cannot target a private network address.",
            )
    return value


def _validate_attachment_content(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_ATTACHMENT_B64_CHARS:
        raise ProfileHostError(
            "INVALID_PARAMS",
            "Attachment content is invalid or too large.",
        )
    encoded = value
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ProfileHostError("INVALID_PARAMS", "Attachment content is invalid.")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProfileHostError("INVALID_PARAMS", "Attachment content is invalid.") from exc
    if len(decoded) > MAX_ATTACHMENT_BYTES:
        raise ProfileHostError(
            "REQUEST_TOO_LARGE",
            "A remote attachment can contain up to 25 MB.",
        )
    return value


def _sanitize_remote_attachment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileHostError("INVALID_PARAMS", "Attachment is invalid.")
    if value.get("filePath"):
        raise ProfileHostError(
            "REMOTE_FILE_PATH_DENIED",
            "Remote profile messages must upload file content, not host file paths.",
        )
    filename = value.get("fileName", "")
    mime_type = value.get("mimeType", "application/octet-stream")
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 255
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in filename)
        or "/" in filename
        or "\\" in filename
        or not isinstance(mime_type, str)
        or not mime_type
        or len(mime_type) > 255
        or any(ord(char) < 0x20 for char in mime_type)
    ):
        raise ProfileHostError("INVALID_PARAMS", "Attachment metadata is invalid.")
    content = value.get("content")
    cdn_url = value.get("cdnUrl")
    if bool(content) == bool(cdn_url):
        raise ProfileHostError(
            "INVALID_PARAMS",
            "Remote attachments must include either uploaded content or a media URL.",
        )
    sanitized: dict[str, Any] = {"fileName": filename, "mimeType": mime_type}
    if content:
        sanitized["content"] = _validate_attachment_content(content)
    else:
        sanitized["cdnUrl"] = _validate_remote_attachment_url(cdn_url)
    return sanitized


def _require_exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ProfileHostError(
            "INVALID_PARAMS",
            "Profile policy request parameters are invalid.",
        )


def _validate_access_policy(method: str, value: dict[str, Any]) -> dict[str, Any]:
    if method in {"tools.access.get", "exec.policy.get", "codex.policy.get"}:
        _require_exact_keys(value, set())
        return {}
    if method == "tools.access.set":
        _require_exact_keys(value, {"disabledToolsets"})
        disabled = value.get("disabledToolsets")
        if (
            not isinstance(disabled, list)
            or len(disabled) > 64
            or any(
                not isinstance(name, str)
                or not name
                or name != name.strip()
                or len(name) > 64
                or any(
                    not (char.isascii() and (char.isalnum() or char in "._-"))
                    for char in name
                )
                for name in disabled
            )
        ):
            raise ProfileHostError(
                "INVALID_PARAMS",
                "Tool access policy is invalid.",
            )
        return {"disabledToolsets": list(dict.fromkeys(disabled))}
    if method == "exec.policy.set":
        _require_exact_keys(value, {"security", "ask"})
        if value.get("security") not in _EXEC_SECURITY or value.get("ask") not in _EXEC_ASK:
            raise ProfileHostError(
                "INVALID_PARAMS",
                "Execution approval policy is invalid.",
            )
        return {"security": value["security"], "ask": value["ask"]}
    if method == "codex.policy.set":
        _require_exact_keys(value, {"approvalPolicy", "sandbox"})
        if (
            value.get("approvalPolicy") not in _CODEX_APPROVAL
            or value.get("sandbox") not in _CODEX_SANDBOX
        ):
            raise ProfileHostError(
                "INVALID_PARAMS",
                "Codex approval policy is invalid.",
            )
        return {
            "approvalPolicy": value["approvalPolicy"],
            "sandbox": value["sandbox"],
        }
    return value


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
    value = _validate_access_policy(method, value)
    session_key_methods = {
        "chat.history",
        "chat.inflight",
        "chat.send",
        "sessions.model.get",
        "sessions.model.set",
    }
    if method in session_key_methods:
        _validate_session_key(value.get("sessionKey"), required=True)
    elif "sessionKey" in value:
        _validate_session_key(value.get("sessionKey"))
    if method == "sessions.delete":
        _validate_session_key(value.get("key"), required=True)
    if method == "media.read":
        media_id = value.get("mediaId")
        offset = value.get("offset", 0)
        length = value.get("length", 0)
        if (
            not isinstance(media_id, str)
            or not media_id
            or len(media_id) > 255
            or media_id.startswith(".")
            or ".." in media_id
            or "/" in media_id
            or "\\" in media_id
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in media_id)
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(length, int)
            or isinstance(length, bool)
            or not 0 <= length <= 1024 * 1024
        ):
            raise ProfileHostError(
                "INVALID_PARAMS",
                "Profile media request is invalid.",
            )
        value = {"mediaId": media_id, "offset": offset, "length": length}
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
        value["attachments"] = [
            _sanitize_remote_attachment(attachment) for attachment in attachments
        ]
        message = value.get("message", "")
        if not isinstance(message, str) or len(message) > MAX_PROFILE_MESSAGE_CHARS:
            raise ProfileHostError(
                "INVALID_PARAMS",
                "A bot message can contain up to 32,000 characters.",
            )
        if not message and not attachments:
            raise ProfileHostError("INVALID_PARAMS", "A bot message cannot be empty.")
        idempotency_key = value.get("idempotencyKey")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or idempotency_key != idempotency_key.strip()
            or len(idempotency_key) > 128
            or any(
                ord(char) < 0x20 or ord(char) == 0x7F
                for char in idempotency_key
            )
        ):
            raise ProfileHostError("INVALID_PARAMS", "Message identity is invalid.")
    return method, value


def bounded_timeout(method: str, requested: Any) -> float:
    maximum = PROFILE_RPC_TIMEOUTS[method]
    if isinstance(requested, (int, float)) and not isinstance(requested, bool):
        milliseconds = max(1_000, min(int(requested), maximum))
    else:
        milliseconds = maximum
    return milliseconds / 1000
