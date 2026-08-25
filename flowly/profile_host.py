"""Remote manager for isolated profile gateway processes.

The public/default gateway owns this manager. Named profiles still run as
separate loopback gateway processes with independent homes; the manager only
provides a narrow lifecycle and RPC proxy for authenticated Desktop/iOS
clients.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from loguru import logger

from flowly.exec.env_scrub import sanitize_subprocess_env
from flowly.profile import (
    create_profile,
    delete_profile,
    describe_profile,
    ensure_profile_bot_id,
    get_or_create_profile_host_id,
    list_profiles,
    read_profile_settings,
    reconcile_runtime_lease,
    update_profile_metadata,
    update_profile_settings,
    validate_profile_name,
)
from flowly.profile_host_contract import (
    MAX_PROFILE_HOPS,
    MAX_PROFILE_MESSAGE_CHARS,
    PROFILE_RPC_TIMEOUTS,
    ProfileHostError,
    bounded_timeout,
    validate_profile_rpc,
)

ProfileEventCallback = Callable[[dict[str, Any]], Awaitable[None]]
PrimaryRpcCallback = Callable[[str, dict[str, Any], float], Awaitable[Any]]
PrimaryEventLeaseCallback = Callable[[bool], None]
TaskStartedCallback = Callable[[str], Awaitable[None] | None]

_READY_PREFIX = "FLOWLY_LOCAL_RUNTIME_READY "
_START_TIMEOUT_SECONDS = 90
_STOP_TIMEOUT_SECONDS = 8
_DELETE_CONFIRM_TTL_SECONDS = 60
_DELETE_CONFIRM_MAX = 128
_MAX_RUNTIMES = 4
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_SAFE_DELEGATED_TOOLS = (
    "read_file",
    "list_dir",
    "memory_search",
    "memory_get",
    "memory_recall",
    "session_search",
    "sessions_list",
    "skill_view",
)
_PROFILE_TASK_DISABLED_TOOLS = (
    "message_profile",
    "spawn",
    "delegate_to",
    "board_run",
    "board_update",
)

_PROFILE_RUNTIME_ENV_DENY = frozenset({
    "FLOWLY_CWD",
    "FLOWLY_HOME",
    "FLOWLY_PROFILE",
    "FLOWLY_SERVER_ID",
})


def _runtime_command() -> list[str]:
    """Launch the same installed Flowly build as the owning gateway."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return [sys.executable]
    return [sys.executable, "-m", "flowly.cli.entry"]


def _profile_runtime_environment() -> dict[str, str]:
    """Return a child environment without the owning gateway's credentials.

    A named profile is a separate configuration and credential scope. The
    process still needs ordinary OS/runtime variables, but it must not inherit
    Flowly-managed provider, channel, relay, or gateway secrets from the
    primary gateway. Profile-local config, ``.env`` and keychain scopes remain
    available after ``--profile`` selects the child home.
    """
    env = sanitize_subprocess_env(os.environ)
    for key in _PROFILE_RUNTIME_ENV_DENY:
        env.pop(key, None)
    return env


def _public_profile(name: str) -> dict[str, Any]:
    return ensure_profile_bot_id(name).to_public_dict()


def _public_settings(name: str) -> dict[str, Any]:
    settings = dict(read_profile_settings(name))
    settings.pop("workspace", None)
    return settings


def _validate_profile_selector(name: str) -> None:
    if name != "default":
        validate_profile_name(name)


def _validate_named_profile(name: str) -> None:
    if name == "default":
        raise ProfileHostError(
            "DEFAULT_PROFILE_DIRECT",
            "The default profile uses the host gateway directly.",
        )
    validate_profile_name(name)


def _required_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str):
        raise ProfileHostError("INVALID_PARAMS", f"{key} must be a string.")
    return value


def _optional_string(params: dict[str, Any], key: str, default: str = "") -> str:
    if key not in params:
        return default
    return _required_string(params, key)


def _profile_reply_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value.strip()
    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(value.get("text"), str):
        return str(value["text"]).strip()
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _safe_startup_message(value: str) -> str:
    plain = _ANSI_ESCAPE_RE.sub("", value).strip()
    if "Profile runtime is already active" in plain:
        return (
            "This bot is already running under another Flowly process. "
            "Close that process or restart the bot host, then try again."
        )
    if "No LLM provider available" in plain:
        return "No model provider is configured for this bot. Configure a provider and try again."
    # A traceback may contain local paths, source excerpts, or terminal
    # drawing characters. Never return it across the remote trust boundary.
    if "Traceback" in plain or "╭" in plain or "│" in plain:
        return "The bot runtime could not start. Check the bot host logs and try again."
    final_line = next((line.strip() for line in reversed(plain.splitlines()) if line.strip()), "")
    return final_line[:500] or "The bot runtime could not start."


def _safe_rpc_error_message(value: Any) -> str:
    plain = _ANSI_ESCAPE_RE.sub("", str(value or "")).strip()
    if (
        not plain
        or "Traceback" in plain
        or "\n" in plain
        or re.search(r"(?:^|\s)/(?:Users|home|private|var|tmp|etc)/", plain)
        or re.search(r"[A-Za-z]:\\", plain)
    ):
        return "The bot operation failed inside its isolated runtime."
    return plain[:500]


@dataclass(slots=True)
class _Runtime:
    profile: str
    process: asyncio.subprocess.Process | None
    session: aiohttp.ClientSession
    ws: aiohttp.ClientWebSocketResponse
    instance_id: str
    owned: bool = True
    state: str = "connected"
    active_runs: set[str] = field(default_factory=set)
    last_used_at: float = field(default_factory=time.time)
    pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    reader_task: asyncio.Task[None] | None = None
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None


class ProfileHost:
    """Own and proxy named profile gateways for authenticated clients."""

    def __init__(
        self,
        on_event: ProfileEventCallback | None = None,
        primary_rpc: PrimaryRpcCallback | None = None,
        primary_event_lease: PrimaryEventLeaseCallback | None = None,
    ):
        self.host_id = get_or_create_profile_host_id()
        self._event_subscribers: dict[str, ProfileEventCallback] = {}
        if on_event is not None:
            self._event_subscribers["owner"] = on_event
        self._primary_rpc = primary_rpc
        self._primary_event_lease = primary_event_lease
        self._default_event_leases: set[str] = set()
        self._runtimes: dict[str, _Runtime] = {}
        self._starting: dict[str, asyncio.Task[_Runtime]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._delete_confirmations: dict[str, tuple[str, float]] = {}
        self._broker_waiters: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._terminal_events: dict[tuple[str, str], dict[str, Any]] = {}
        self._broker_target_locks: dict[str, asyncio.Lock] = {}
        self._task_target_locks: dict[str, asyncio.Lock] = {}
        self._broker_sessions: dict[tuple[str, str], str] = {}
        # Private, bounded audit state for hidden Board worker turns.  These
        # events never enter public profile streams or session lists; the
        # primary Board may query a sanitized projection by opaque run id.
        self._task_audits: dict[tuple[str, str], dict[str, Any]] = {}
        self._task_audit_sessions: dict[tuple[str, str], dict[str, Any]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._capacity_lock = asyncio.Lock()
        self._closed = False
        self._manager_instance = str(uuid.uuid4())

    def capabilities(self) -> dict[str, Any]:
        return {
            "version": 1,
            "hostId": self.host_id,
            "methods": [
                "profiles.capabilities",
                "profiles.list",
                "profiles.create",
                "profiles.settings",
                "profiles.configure",
                "profiles.delete.prepare",
                "profiles.delete.commit",
                "profiles.statuses",
                "profiles.connect",
                "profiles.stop",
                "profiles.rpc",
            ],
            "profileRpcMethods": sorted(PROFILE_RPC_TIMEOUTS),
            "events": ["profile.event"],
            "storageMode": "profile-local",
            "processIsolation": "per-profile-gateway",
            "defaultProfileRpc": "direct",
            "wrappedDefaultProfileRpc": True,
            "maxConcurrentRuntimes": _MAX_RUNTIMES,
        }

    def subscribe_events(self, callback: ProfileEventCallback) -> str:
        """Register an isolated event consumer and return its opaque lease id."""
        token = secrets.token_urlsafe(18)
        self._event_subscribers[token] = callback
        return token

    def unsubscribe_events(self, token: str) -> None:
        self._event_subscribers.pop(token, None)

    def retain_default_events(self, owner: str) -> None:
        """Keep the private primary-profile event sink alive for one consumer.

        ``owner`` is assigned by the authenticated transport (never by request
        params). A set, rather than a counter, makes reconnect/disconnect cleanup
        idempotent and prevents an imbalanced release from silencing another
        device's stream.
        """
        if not owner or owner in self._default_event_leases:
            return
        was_empty = not self._default_event_leases
        self._default_event_leases.add(owner)
        if was_empty and self._primary_event_lease is not None:
            self._primary_event_lease(True)

    def release_default_events(self, owner: str) -> None:
        if owner not in self._default_event_leases:
            return
        self._default_event_leases.discard(owner)
        if not self._default_event_leases and self._primary_event_lease is not None:
            self._primary_event_lease(False)

    async def dispatch(self, method: str, params: Any) -> Any:
        """Serve one public profile-host operation from either transport."""
        if not isinstance(params, dict):
            raise ProfileHostError(
                "INVALID_PARAMS", "Profile request parameters must be an object."
            )
        if method == "profiles.capabilities":
            return self.capabilities()
        if method == "profiles.list":
            return await self.list()
        if method == "profiles.create":
            return await self.create(params)
        if method == "profiles.settings":
            return await self.settings(_required_string(params, "name").strip())
        if method == "profiles.configure":
            return await self.configure(params)
        if method == "profiles.delete.prepare":
            return await self.prepare_delete(_required_string(params, "name").strip())
        if method == "profiles.delete.commit":
            confirmation = params.get("confirmation")
            if not isinstance(confirmation, str):
                raise ProfileHostError(
                    "INVALID_PARAMS", "Bot deletion confirmation must be a string."
                )
            return await self.commit_delete(
                _required_string(params, "name").strip(), confirmation
            )
        if method == "profiles.statuses":
            return await self.statuses()
        if method == "profiles.connect":
            return await self.connect(_required_string(params, "name").strip())
        if method == "profiles.stop":
            return await self.stop(_required_string(params, "name").strip())
        if method == "profiles.rpc":
            return await self.rpc(
                _required_string(params, "name").strip(),
                params.get("method"),
                params.get("params"),
                params.get("timeoutMs"),
            )
        raise ProfileHostError(
            "METHOD_NOT_ALLOWED", "This profile-host operation is not available."
        )

    async def list(self) -> dict[str, Any]:
        return {
            "hostId": self.host_id,
            "profiles": [_public_profile(profile.name) for profile in list_profiles()],
        }

    async def statuses(self) -> dict[str, Any]:
        return {
            "hostId": self.host_id,
            "statuses": [self.status(profile.name) for profile in list_profiles()],
        }

    def status(self, name: str) -> dict[str, Any]:
        _validate_profile_selector(name)
        bot_id = str(_public_profile(name).get("botId") or "")
        if name == "default":
            return {
                "profile": name,
                "botId": bot_id,
                "state": "connected",
                "owned": False,
                "activeRuns": 0,
            }
        runtime = self._runtimes.get(name)
        if runtime is None:
            profile = describe_profile(name)
            external = reconcile_runtime_lease(profile.path, profile_name=name)
            if external:
                return {
                    "profile": name,
                    "botId": bot_id,
                    "state": "external",
                    "owned": False,
                    "activeRuns": 0,
                }
            return {
                "profile": name,
                "botId": bot_id,
                "state": "starting" if name in self._starting else "stopped",
                "owned": True,
                "activeRuns": 0,
            }
        return {
            "profile": name,
            "botId": bot_id,
            "state": runtime.state,
            "owned": runtime.owned,
            "activeRuns": len(runtime.active_runs),
            "lastUsedAt": int(runtime.last_used_at * 1000),
        }

    async def create(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _required_string(params, "name").strip()
        validate_profile_name(name)
        clone_from = params.get("cloneFrom", "default")
        if clone_from is not None:
            if not isinstance(clone_from, str):
                raise ProfileHostError("INVALID_PARAMS", "cloneFrom must be a bot name or null.")
            clone_from = clone_from.strip()
            _validate_profile_selector(clone_from)
        create_profile(
            name,
            clone_from=clone_from,
            clone_all=False,
            display_name=_optional_string(params, "displayName").strip(),
            description=_optional_string(params, "description").strip(),
            local_runtime=True,
            provider=_required_string(params, "provider").strip() if "provider" in params else None,
            model=_required_string(params, "model").strip() if "model" in params else None,
            soul=_required_string(params, "soul") if "soul" in params else None,
            mark_text=_optional_string(params, "markText"),
            mark_tone=_optional_string(params, "markTone"),
        )
        profile = _public_profile(name)
        await self._emit(name, "directory", {"action": "created", "profile": profile})
        return {"ok": True, "profile": profile}

    async def settings(self, name: str) -> dict[str, Any]:
        _validate_profile_selector(name)
        return {"settings": _public_settings(name)}

    async def configure(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _required_string(params, "name").strip()
        _validate_profile_selector(name)
        mutable_fields = {
            "provider", "model", "soul", "displayName", "description", "markText", "markTone"
        }
        if not mutable_fields.intersection(params):
            raise ProfileHostError("INVALID_PARAMS", "No bot settings were provided.")
        for field_name in mutable_fields.intersection(params):
            _required_string(params, field_name)
        if name == "default" and any(field in params for field in ("provider", "model", "soul")):
            raise ProfileHostError(
                "DEFAULT_PROFILE_DIRECT",
                "Change the default profile through the host gateway settings.",
            )
        if any(field in params for field in ("provider", "model", "soul")):
            runtime = self._runtimes.get(name)
            has_internal_turn = any(profile == name for profile, _session in self._broker_sessions)
            if (runtime and runtime.active_runs) or has_internal_turn:
                raise ProfileHostError(
                    "PROFILE_BUSY",
                    "Finish or stop the active turn before changing this profile's runtime settings.",
                    retryable=True,
                )
            await self.stop(name)
            update_profile_settings(
                name,
                provider=params["provider"].strip() if "provider" in params else None,
                model=params["model"].strip() if "model" in params else None,
                soul=params["soul"] if "soul" in params else None,
            )
        if any(field in params for field in ("displayName", "description", "markText", "markTone")):
            update_profile_metadata(
                name,
                display_name=params["displayName"] if "displayName" in params else None,
                description=params["description"] if "description" in params else None,
                mark_text=params["markText"] if "markText" in params else None,
                mark_tone=params["markTone"] if "markTone" in params else None,
            )
        profile = _public_profile(name)
        await self._emit(name, "directory", {"action": "updated", "profile": profile})
        return {"ok": True, "profile": profile, "settings": _public_settings(name)}

    async def prepare_delete(self, name: str) -> dict[str, Any]:
        _validate_profile_selector(name)
        if name == "default":
            raise ProfileHostError("DEFAULT_PROFILE", "The default profile cannot be deleted.")
        profile = _public_profile(name)
        now = time.monotonic()
        for key, (_bot_id, expires_at) in list(self._delete_confirmations.items()):
            if expires_at < now:
                self._delete_confirmations.pop(key, None)
        while len(self._delete_confirmations) >= _DELETE_CONFIRM_MAX:
            self._delete_confirmations.pop(next(iter(self._delete_confirmations)))
        nonce = secrets.token_urlsafe(32)
        self._delete_confirmations[nonce] = (
            str(profile["botId"]),
            now + _DELETE_CONFIRM_TTL_SECONDS,
        )
        return {
            "confirmation": nonce,
            "expiresInSeconds": _DELETE_CONFIRM_TTL_SECONDS,
            "profile": profile,
        }

    async def commit_delete(self, name: str, confirmation: str) -> dict[str, Any]:
        _validate_profile_selector(name)
        expected = self._delete_confirmations.pop(confirmation, None)
        current = _public_profile(name)
        if (
            expected is None
            or expected[1] < time.monotonic()
            or expected[0] != current.get("botId")
        ):
            raise ProfileHostError(
                "DELETE_CONFIRMATION_INVALID",
                "Bot deletion confirmation expired. Review the bot and try again.",
            )
        await self.stop(name)
        delete_profile(name)
        await self._emit(name, "directory", {"action": "deleted", "botId": current["botId"]})
        return {"ok": True, "deleted": name, "botId": current["botId"]}

    async def connect(self, name: str) -> dict[str, Any]:
        _validate_profile_selector(name)
        if name != "default":
            await self._ensure_runtime(name)
        return {"status": self.status(name)}

    async def stop(self, name: str) -> dict[str, Any]:
        _validate_profile_selector(name)
        if name == "default":
            return {"ok": True, "status": self.status(name)}
        async with self._lock(name):
            starting = self._starting.get(name)
            if starting:
                starting.cancel()
                await asyncio.gather(starting, return_exceptions=True)
            runtime = self._runtimes.get(name)
            if runtime is not None:
                if not runtime.owned:
                    raise ProfileHostError(
                        "PROFILE_OWNERSHIP_CONFLICT",
                        "This bot is owned by another Flowly process and cannot be stopped here.",
                    )
                self._runtimes.pop(name, None)
                await self._close_runtime(runtime)
            else:
                profile = describe_profile(name)
                if reconcile_runtime_lease(profile.path, profile_name=name):
                    raise ProfileHostError(
                        "PROFILE_OWNERSHIP_CONFLICT",
                        "This bot is running under another Flowly process and cannot be stopped here.",
                    )
        await self._emit(name, "connection", {"state": "stopped"})
        return {"ok": True, "status": self.status(name)}

    async def rpc(
        self,
        name: str,
        method: Any,
        params: Any = None,
        timeout_ms: Any = None,
    ) -> Any:
        _validate_profile_selector(name)
        method, safe = validate_profile_rpc(method, params)
        if method == "chat.send":
            directory = [profile.name for profile in list_profiles()]
            mentions = safe.get("profileMentions")
            if not isinstance(mentions, list):
                mentions = []
            safe["profileDirectory"] = directory
            safe["profileMentions"] = [
                item for item in mentions
                if isinstance(item, str) and item in directory and item != name
            ]
            # Identity and authority are assigned by the host, never trusted
            # from a remote renderer/mobile client.
            safe.pop("profileMessageContext", None)
            safe.pop("allowedTools", None)
            safe.pop("disabledTools", None)
            safe["turnOrigin"] = "user"
        result = await self._target_rpc(
            name, method, safe, bounded_timeout(method, timeout_ms)
        )
        if method == "sessions.list" and isinstance(result, dict):
            sessions = result.get("sessions")
            if isinstance(sessions, list):
                result = {
                    **result,
                    "sessions": [
                        session
                        for session in sessions
                        if isinstance(session, dict)
                        and isinstance(session.get("key"), str)
                        and session["key"].startswith(("desktop:", "web:", "ios:"))
                        and not session["key"].startswith((
                            "desktop:profile-inbox:",
                            "desktop:profile-task:",
                        ))
                    ],
                }
        return result

    async def run_task(
        self,
        name: str,
        *,
        task_id: str,
        prompt: str,
        idempotency_key: str,
        timeout: float = 1800.0,
        on_started: TaskStartedCallback | None = None,
    ) -> dict[str, Any]:
        """Run one dispatcher-owned task in a hidden named-profile session.

        This is an internal broker operation, deliberately absent from the
        public profile-host method table. The primary Board owns identity,
        retries, and completion; the worker receives only task text and its
        ordinary sandboxed profile capabilities.
        """
        _validate_named_profile(name)
        task_id = str(task_id or "").strip()
        prompt = str(prompt or "").strip()
        idempotency_key = str(idempotency_key or "").strip()
        if not task_id or len(task_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", task_id):
            raise ProfileHostError("TASK_INVALID", "The Board task identity is invalid.")
        task_prefix = (
            "You are executing an assigned Flowly Board task. Work on the "
            "task completely, use your profile capabilities when useful, "
            "and return a concise final handoff with results, files changed, "
            "verification, and any blocker. Do not delegate this task to "
            "another profile.\n\nTask:\n"
        )
        task_message = f"{task_prefix}{prompt}"
        if not prompt or len(task_message) > MAX_PROFILE_MESSAGE_CHARS:
            raise ProfileHostError(
                "TASK_INVALID",
                "The Board task must contain 1–32,000 characters.",
            )
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or any(ord(char) < 0x20 for char in idempotency_key)
        ):
            raise ProfileHostError("TASK_INVALID", "The task run identity is invalid.")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ProfileHostError("TASK_INVALID", "The task timeout is invalid.") from exc
        if not math.isfinite(timeout):
            raise ProfileHostError("TASK_INVALID", "The task timeout is invalid.")
        timeout = max(30.0, min(timeout, 3600.0))
        session_suffix = hashlib.sha256(task_id.encode()).hexdigest()[:24]
        session_key = f"desktop:profile-task:{session_suffix}"
        correlation_id = f"task:{task_id}:{idempotency_key}"

        async with self._task_target_locks.setdefault(name, asyncio.Lock()):
            self._broker_sessions[(name, session_key)] = correlation_id
            try:
                model = str(_public_settings(name).get("model") or "") or None
            except Exception:
                model = None
            audit: dict[str, Any] = {
                "runId": "",
                "startedAt": time.time(),
                "endedAt": None,
                "outcome": None,
                "error": None,
                "model": model,
                "toolTrace": [],
            }
            self._task_audit_sessions[(name, session_key)] = audit
            run_id = ""
            try:
                accepted = await self._target_rpc(name, "chat.send", {
                    "sessionKey": session_key,
                    "message": task_message,
                    "thinking": False,
                    "idempotencyKey": idempotency_key,
                    "profileDirectory": [],
                    "profileMentions": [],
                    "disabledTools": list(_PROFILE_TASK_DISABLED_TOOLS),
                    "turnOrigin": "task",
                }, 60)
                run_id = str((accepted or {}).get("runId") or "")
                if not run_id:
                    raise ProfileHostError(
                        "TASK_START_FAILED",
                        "The assigned bot did not accept the task.",
                        retryable=True,
                    )
                audit["runId"] = run_id
                self._task_audits[(name, run_id)] = audit
                while len(self._task_audits) > 256:
                    self._task_audits.pop(next(iter(self._task_audits)))
                if on_started is not None:
                    try:
                        started_result = on_started(run_id)
                        if asyncio.iscoroutine(started_result):
                            await started_result
                    except Exception as exc:
                        # Audit linkage is best-effort and must never abort an
                        # already accepted worker turn. The completion path
                        # performs the same link again as a fallback.
                        logger.warning(
                            "Could not link Board task {} to worker run {}: {}",
                            task_id,
                            run_id,
                            exc,
                        )
                key = (name, run_id)
                terminal = self._terminal_events.pop(key, None)
                if terminal is None:
                    waiter = asyncio.get_running_loop().create_future()
                    self._broker_waiters[key] = waiter
                    try:
                        terminal = await asyncio.wait_for(waiter, timeout=timeout)
                    except asyncio.TimeoutError as exc:
                        await self._target_rpc(name, "chat.abort", {"runId": run_id}, 30)
                        raise ProfileHostError(
                            "TASK_TIMEOUT",
                            "The assigned bot did not finish before the task timeout.",
                            retryable=True,
                        ) from exc
                    finally:
                        self._broker_waiters.pop(key, None)
                        self._terminal_events.pop(key, None)
                response = _profile_reply_text(terminal.get("message"))
                if not response:
                    raise ProfileHostError(
                        "TASK_EMPTY_RESPONSE",
                        "The assigned bot completed without a task handoff.",
                    )
                return {"runId": run_id, "response": response}
            except asyncio.CancelledError:
                if run_id:
                    try:
                        await self._target_rpc(name, "chat.abort", {"runId": run_id}, 30)
                    except Exception:
                        logger.debug("Could not abort cancelled Board task {}", task_id)
                raise
            finally:
                self._broker_sessions.pop((name, session_key), None)
                self._task_audit_sessions.pop((name, session_key), None)

    def task_audit(self, profile: str, run_id: str) -> dict[str, Any] | None:
        """Return the content-free audit projection for one hidden Board run."""
        audit = self._task_audits.get((profile, run_id))
        if audit is None:
            return None
        return {
            "runId": audit.get("runId"),
            "startedAt": audit.get("startedAt"),
            "endedAt": audit.get("endedAt"),
            "outcome": audit.get("outcome"),
            "error": audit.get("error"),
            "model": audit.get("model"),
            "toolTrace": [dict(item) for item in audit.get("toolTrace", [])],
        }

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        starters = list(self._starting.values())
        for task in starters:
            task.cancel()
        await asyncio.gather(*starters, return_exceptions=True)
        runtimes = list(self._runtimes.values())
        self._runtimes.clear()
        for waiter in self._broker_waiters.values():
            if not waiter.done():
                waiter.set_exception(ProfileHostError(
                    "HOST_STOPPED", "The profile host is shutting down."
                ))
        self._broker_waiters.clear()
        self._broker_sessions.clear()
        self._task_audits.clear()
        self._task_audit_sessions.clear()
        self._default_event_leases.clear()
        if self._primary_event_lease is not None:
            self._primary_event_lease(False)
        background = list(self._background_tasks)
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await asyncio.gather(*(self._close_runtime(runtime) for runtime in runtimes), return_exceptions=True)

    def _lock(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock

    async def _ensure_runtime(self, name: str) -> _Runtime:
        if self._closed:
            raise ProfileHostError("HOST_STOPPED", "The profile host is shutting down.")
        _validate_named_profile(name)
        profile = describe_profile(name)
        current = self._runtimes.get(name)
        if self._runtime_is_open(current):
            assert current is not None
            return current
        pending = self._starting.get(name)
        if pending:
            return await pending
        async with self._lock(name):
            current = self._runtimes.get(name)
            if self._runtime_is_open(current):
                assert current is not None
                return current
            pending = self._starting.get(name)
            if pending is None:
                lease = reconcile_runtime_lease(profile.path, profile_name=name)
                async with self._capacity_lock:
                    allocated = set(self._runtimes) | set(self._starting)
                    if name not in allocated and len(allocated) >= _MAX_RUNTIMES:
                        raise ProfileHostError(
                            "PROFILE_CAPACITY",
                            "Four bots are already running. Stop one and try again.",
                            retryable=True,
                        )
                    if lease:
                        pending = asyncio.create_task(
                            self._attach_runtime(name, lease),
                            name=f"profile-attach:{name}",
                        )
                    else:
                        pending = asyncio.create_task(
                            self._start_runtime(name),
                            name=f"profile-start:{name}",
                        )
                    self._starting[name] = pending
        try:
            return await pending
        finally:
            if self._starting.get(name) is pending:
                self._starting.pop(name, None)

    @staticmethod
    def _runtime_is_open(runtime: _Runtime | None) -> bool:
        return bool(
            runtime is not None
            and not runtime.ws.closed
            and (runtime.process is None or runtime.process.returncode is None)
        )

    async def _open_runtime_transport(
        self,
        *,
        port: int,
        token: str,
        error_code: str,
        error_message: str,
    ) -> tuple[aiohttp.ClientSession, aiohttp.ClientWebSocketResponse]:
        """Open one authenticated loopback socket without exposing its token."""
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        try:
            async with session.post(
                f"http://127.0.0.1:{port}/api/auth/ws-ticket",
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status != 200:
                    raise ProfileHostError(error_code, error_message, retryable=True)
                ticket = str((await response.json()).get("ticket") or "")
            if not ticket:
                raise ProfileHostError(error_code, error_message, retryable=True)
            ws = await session.ws_connect(
                f"http://127.0.0.1:{port}/ws?ticket={ticket}",
                max_msg_size=40 * 1024 * 1024,
            )
            return session, ws
        except BaseException:
            await session.close()
            raise

    async def _attach_runtime(self, name: str, lease: dict[str, Any]) -> _Runtime:
        """Attach to a live Desktop-owned gateway instead of spawning twice.

        The runtime lease is owner-only and authenticated.  Attaching never
        transfers lifecycle ownership: shutdown closes this manager's socket,
        while ``profiles.stop`` continues to reject attempts to terminate the
        Desktop-owned process.
        """
        await self._emit(name, "connection", {"state": "starting"})
        instance_id = str(lease.get("instanceId") or "")
        token = str(lease.get("authToken") or "")
        try:
            port = int(lease.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if (
            not instance_id
            or not 1 <= port <= 65_535
            or not 32 <= len(token) <= 512
            or any(ord(char) < 0x21 or ord(char) == 0x7F for char in token)
        ):
            raise ProfileHostError(
                "PROFILE_OWNERSHIP_CONFLICT",
                "This bot is running in an older Flowly Desktop runtime. Restart the bot in Desktop, then try again.",
            )

        session: aiohttp.ClientSession | None = None
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            session, ws = await self._open_runtime_transport(
                port=port,
                token=token,
                error_code="PROFILE_ATTACH_FAILED",
                error_message="The running bot did not accept a secure manager connection.",
            )
            profile = describe_profile(name)
            current = reconcile_runtime_lease(profile.path, profile_name=name)
            if (
                not current
                or str(current.get("instanceId") or "") != instance_id
                or int(current.get("port") or 0) != port
                or not secrets.compare_digest(
                    str(current.get("authToken") or ""), token
                )
            ):
                raise ProfileHostError(
                    "PROFILE_RUNTIME_CHANGED",
                    "The bot restarted while Flowly was connecting. Try again.",
                    retryable=True,
                )
            runtime = _Runtime(
                profile=name,
                process=None,
                session=session,
                ws=ws,
                instance_id=instance_id,
                owned=False,
            )
            runtime.reader_task = asyncio.create_task(
                self._read_runtime(runtime), name=f"profile-reader:{name}"
            )
            self._runtimes[name] = runtime
            await self._emit(name, "connection", {"state": "connected"})
            return runtime
        except BaseException as exc:
            if ws is not None:
                await ws.close()
            if session is not None:
                await session.close()
            message = (
                exc.message
                if isinstance(exc, ProfileHostError)
                else "The running bot could not be reached securely."
            )
            logger.warning("Could not attach to existing profile runtime {}: {}", name, message)
            await self._emit(name, "error", {"message": message})
            if isinstance(exc, ProfileHostError):
                raise
            raise ProfileHostError(
                "PROFILE_ATTACH_FAILED", message, retryable=True
            ) from exc

    async def _start_runtime(self, name: str) -> _Runtime:
        await self._emit(name, "connection", {"state": "starting"})
        env = _profile_runtime_environment()
        env.update({
            "FLOWLY_DESKTOP_MANAGER_PID": str(os.getpid()),
            "FLOWLY_DESKTOP_MANAGER_INSTANCE": self._manager_instance,
        })
        try:
            from flowly.profile import _process_identity

            identity = _process_identity(os.getpid())
            if identity:
                env["FLOWLY_DESKTOP_MANAGER_IDENTITY"] = identity
        except Exception:
            pass
        command = [*_runtime_command(), "--profile", name, "serve", "--port", "0"]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=os.name != "nt",
        )
        stderr_tail = bytearray()
        stdout_tail = bytearray()

        async def drain_stderr() -> None:
            assert process.stderr is not None
            while chunk := await process.stderr.read(4096):
                stderr_tail.extend(chunk)
                if len(stderr_tail) > 8192:
                    del stderr_tail[:-8192]

        stderr_task = asyncio.create_task(drain_stderr(), name=f"profile-stderr:{name}")
        try:
            ready = await asyncio.wait_for(
                self._read_ready(process, name, stdout_tail),
                _START_TIMEOUT_SECONDS,
            )
            token = str(ready.get("token") or "")
            port = int(ready.get("port") or 0)
            if not token or not 1 <= port <= 65535:
                raise ProfileHostError("STARTUP_INVALID", "Profile runtime returned invalid startup data.")
            session, ws = await self._open_runtime_transport(
                port=port,
                token=token,
                error_code="STARTUP_AUTH",
                error_message="Profile runtime rejected its manager connection.",
            )
            runtime = _Runtime(
                profile=name,
                process=process,
                session=session,
                ws=ws,
                instance_id=str(ready.get("instanceId") or ""),
                stderr_task=stderr_task,
            )
            runtime.stdout_task = asyncio.create_task(
                self._drain_stream(process.stdout), name=f"profile-stdout:{name}"
            )
            runtime.reader_task = asyncio.create_task(
                self._read_runtime(runtime), name=f"profile-reader:{name}"
            )
            self._runtimes[name] = runtime
            await self._emit(name, "connection", {"state": "connected"})
            return runtime
        except BaseException as exc:
            await self._terminate_process(process)
            # Once the process exits its stderr pipe reaches EOF. Let the
            # drainer consume that tail before cancellation; otherwise a fast
            # startup failure races us and the only actionable line is lost.
            await asyncio.gather(stderr_task, return_exceptions=True)
            detail = bytes(stderr_tail or stdout_tail).decode("utf-8", "replace").strip()
            message = _safe_startup_message(detail or str(exc))
            logger.error("Profile runtime {} failed during startup: {}", name, message)
            await self._emit(name, "error", {"message": message})
            if isinstance(exc, ProfileHostError):
                if exc.code == "PROFILE_START_FAILED" and detail:
                    raise ProfileHostError(
                        exc.code,
                        message,
                        retryable=exc.retryable,
                    ) from exc
                raise
            raise ProfileHostError("PROFILE_START_FAILED", message, retryable=True) from exc

    async def _read_ready(
        self,
        process: asyncio.subprocess.Process,
        name: str,
        stdout_tail: bytearray,
    ) -> dict[str, Any]:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                code = await process.wait()
                raise ProfileHostError(
                    "PROFILE_START_FAILED",
                    f"Profile runtime exited before startup ({code}).",
                    retryable=True,
                )
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith(_READY_PREFIX):
                stdout_tail.extend(line)
                if len(stdout_tail) > 8192:
                    del stdout_tail[:-8192]
                continue
            try:
                ready = json.loads(text[len(_READY_PREFIX):])
            except json.JSONDecodeError as exc:
                raise ProfileHostError("STARTUP_INVALID", "Profile runtime returned malformed startup data.") from exc
            if not isinstance(ready, dict) or ready.get("profile") != name:
                raise ProfileHostError("STARTUP_INVALID", "Profile runtime identity did not match the requested bot.")
            return ready

    async def _drain_stream(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while await stream.read(4096):
            pass

    async def _rpc(self, runtime: _Runtime, method: str, params: dict[str, Any], timeout: float) -> Any:
        if runtime.ws.closed:
            raise ProfileHostError("PROFILE_OFFLINE", "The profile runtime is offline.", retryable=True)
        request_id = secrets.token_urlsafe(18)
        future = asyncio.get_running_loop().create_future()
        runtime.pending[request_id] = future
        try:
            await runtime.ws.send_json({"type": "rpc", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ProfileHostError("PROFILE_RPC_TIMEOUT", "The profile operation timed out.", retryable=True) from exc
        finally:
            runtime.pending.pop(request_id, None)

    async def _read_runtime(self, runtime: _Runtime) -> None:
        try:
            async for message in runtime.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                try:
                    frame = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "rpc":
                    self._resolve_rpc(runtime, frame)
                elif frame.get("type") == "event":
                    await self._handle_event(runtime, str(frame.get("event") or ""), frame.get("data"))
                elif frame.get("type") == "profile_message_request":
                    self._spawn_background(
                        self._handle_broker_request(runtime, frame),
                        name=f"profile-broker:{runtime.profile}",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Profile runtime reader failed for {}", runtime.profile)
        finally:
            owns_runtime = self._runtimes.get(runtime.profile) is runtime
            if owns_runtime:
                self._runtimes.pop(runtime.profile, None)
            if owns_runtime and not self._closed:
                await self._emit(runtime.profile, "connection", {"state": "error"})
            for future in runtime.pending.values():
                if not future.done():
                    future.set_exception(ProfileHostError(
                        "PROFILE_OFFLINE", "The profile runtime disconnected.", retryable=True
                    ))
            if owns_runtime:
                await self._close_runtime(runtime)

    def _resolve_rpc(self, runtime: _Runtime, frame: dict[str, Any]) -> None:
        future = runtime.pending.get(str(frame.get("id") or ""))
        if future is None or future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            future.set_exception(ProfileHostError(
                str(error.get("code") or "PROFILE_RPC_FAILED"),
                _safe_rpc_error_message(error.get("message")),
                retryable=bool(error.get("retryable", False)),
            ))
        else:
            future.set_result(frame.get("result"))

    async def _handle_event(self, runtime: _Runtime, event: str, data: Any) -> None:
        await self._handle_profile_event(runtime.profile, event, data, runtime=runtime)

    async def handle_primary_frame(self, frame: dict[str, Any]) -> None:
        """Consume a frame from the host gateway's private in-process client."""
        if frame.get("type") == "event":
            payload = frame.get("data")
            if not isinstance(payload, dict):
                return
            session_key = str(payload.get("sessionKey") or "")
            run_id = str(payload.get("runId") or "")
            if not self._default_event_leases and (
                ("default", session_key) not in self._broker_sessions
                and ("default", run_id) not in self._broker_waiters
                and ("default", run_id) not in self._terminal_events
            ):
                return
            await self._handle_profile_event(
                "default",
                str(frame.get("event") or ""),
                payload,
            )

    async def _handle_profile_event(
        self,
        profile: str,
        event: str,
        data: Any,
        *,
        runtime: _Runtime | None = None,
    ) -> None:
        payload = data if isinstance(data, dict) else {}
        run_id = str(payload.get("runId") or "")
        session_key = str(payload.get("sessionKey") or "")
        internal_turn = bool(self._broker_sessions.get((profile, session_key)))
        if internal_turn:
            self._capture_task_audit_event(profile, session_key, event, payload)
        if runtime is not None and event == "agent" and run_id:
            runtime.active_runs.add(run_id)
        if event == "chat" and run_id:
            if payload.get("state") in ("final", "aborted", "error"):
                if runtime is not None:
                    runtime.active_runs.discard(run_id)
                key = (profile, run_id)
                waiter = self._broker_waiters.get(key)
                if waiter is not None or (profile, session_key) in self._broker_sessions:
                    self._terminal_events[key] = payload
                    if len(self._terminal_events) > 256:
                        self._terminal_events.pop(next(iter(self._terminal_events)))
                if waiter is not None and not waiter.done():
                    if payload.get("state") == "final":
                        waiter.set_result(payload)
                    else:
                        waiter.set_exception(ProfileHostError(
                            "PROFILE_COLLABORATION_FAILED",
                            f"Profile collaboration ended with {payload.get('state')}.",
                        ))
            elif runtime is not None:
                runtime.active_runs.add(run_id)
        if runtime is not None:
            runtime.last_used_at = time.time()
        if event == "exec.approval.requested" and payload.get("id"):
            if internal_turn:
                self._spawn_background(self._resolve_internal_prompt(
                    profile,
                    "exec.approval.resolve",
                    {"id": payload["id"], "decision": "deny"},
                ), name=f"profile-approval:{profile}")
        elif event == "agent.clarify.requested" and payload.get("id"):
            if internal_turn:
                self._spawn_background(self._resolve_internal_prompt(
                    profile,
                    "agent.clarify.resolve",
                    {
                        "id": payload["id"],
                        "answer": (
                            "Return the question to the source bot; no user is attached "
                            "to this internal turn."
                        ),
                    },
                ), name=f"profile-clarify:{profile}")
        # Internal collaboration and Board-worker sessions are deliberately
        # absent from public session lists. Their live events must be private
        # too, otherwise remote clients can momentarily render hidden turns or
        # count them as user conversations. The primary Board publishes the
        # durable task state and completion notification instead.
        if not internal_turn:
            await self._emit(profile, event, payload)

    def _capture_task_audit_event(
        self,
        profile: str,
        session_key: str,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        """Accumulate sanitized lifecycle metadata for a hidden Board turn."""
        audit = self._task_audit_sessions.get((profile, session_key))
        if audit is None:
            return
        if event == "chat" and payload.get("state") in ("final", "aborted", "error"):
            state = str(payload.get("state") or "error")
            audit["endedAt"] = time.time()
            audit["outcome"] = "ok" if state == "final" else state
            if state != "final":
                audit["error"] = "The assigned agent run ended before completion."
            return
        if event not in ("tool.start", "tool.complete"):
            return
        tool_call_id = str(payload.get("toolCallId") or "")
        trace = audit.setdefault("toolTrace", [])
        if event == "tool.start":
            args = payload.get("args")
            try:
                args_bytes = len(json.dumps(args, separators=(",", ":")).encode())
            except (TypeError, ValueError):
                args_bytes = None
            trace.append({
                "id": tool_call_id,
                "tool": str(payload.get("name") or "") or None,
                "args_bytes": args_bytes,
                "status": "running",
                "duration_ms": None,
            })
            if len(trace) > 256:
                del trace[:-256]
            return
        entry = next(
            (item for item in reversed(trace) if item.get("id") == tool_call_id),
            None,
        )
        if entry is None:
            entry = {
                "id": tool_call_id,
                "tool": str(payload.get("name") or "") or None,
                "args_bytes": None,
            }
            trace.append(entry)
        entry["status"] = "ok" if payload.get("success") else "error"
        duration = payload.get("durationMs")
        entry["duration_ms"] = duration if isinstance(duration, int) else None

    def _spawn_background(self, coroutine: Awaitable[Any], *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _resolve_internal_prompt(
        self,
        profile: str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        try:
            await self._target_rpc(profile, method, params, 30)
        except Exception:
            logger.debug("Could not auto-resolve internal {} for {}", method, profile)

    async def _handle_broker_request(self, source: _Runtime, frame: dict[str, Any]) -> None:
        request_id = str(frame.get("id") or "")
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        try:
            result = await self._broker(source.profile, params)
            response = {"type": "profile_message_result", "id": request_id, "result": result}
        except ProfileHostError as exc:
            response = {
                "type": "profile_message_result",
                "id": request_id,
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception:
            logger.exception("Profile collaboration failed for {}", source.profile)
            response = {
                "type": "profile_message_result",
                "id": request_id,
                "error": {
                    "code": "PROFILE_COLLABORATION_FAILED",
                    "message": "The bot collaboration could not be completed.",
                },
            }
        try:
            if not source.ws.closed:
                await source.ws.send_json(response)
        except Exception:
            logger.debug("Profile collaboration result could not return to {}", source.profile)

    async def _broker(self, source_profile: str, params: dict[str, Any]) -> dict[str, Any]:
        if str(params.get("sourceProfile") or "").strip() != source_profile:
            raise ProfileHostError("PROFILE_SOURCE_INVALID", "Profile message source failed validation.")
        target = str(params.get("targetProfile") or "").strip()
        message = str(params.get("message") or "").strip()
        source_session = str(params.get("sourceSessionKey") or "").strip()
        raw_hop = params.get("hop")
        if not isinstance(raw_hop, int) or isinstance(raw_hop, bool):
            raise ProfileHostError("PROFILE_CONTEXT_INVALID", "Profile collaboration context is invalid.")
        hop = raw_hop
        correlation_id = str(params.get("correlationId") or uuid.uuid4())
        available = {profile.name for profile in list_profiles()}
        if target not in available:
            raise ProfileHostError("PROFILE_NOT_FOUND", f"Unknown profile '{target}'.")
        if target == source_profile:
            raise ProfileHostError("PROFILE_SELF_MESSAGE", "A profile cannot message itself.")
        if not message or len(message) > MAX_PROFILE_MESSAGE_CHARS:
            raise ProfileHostError("PROFILE_MESSAGE_INVALID", "Profile message must contain 1–32,000 characters.")
        if (
            not source_session
            or len(source_session) > 256
            or not correlation_id
            or len(correlation_id) > 128
            or hop < 1
            or hop > MAX_PROFILE_HOPS
        ):
            raise ProfileHostError("PROFILE_CONTEXT_INVALID", "Profile collaboration context is invalid.")
        source_session_id = hashlib.sha256(source_session.encode()).hexdigest()[:16]
        target_session = f"desktop:profile-inbox:{source_profile}:{source_session_id}"
        async with self._broker_target_locks.setdefault(target, asyncio.Lock()):
            return await self._run_broker_turn(
                source_profile=source_profile,
                target=target,
                target_session=target_session,
                message=message,
                correlation_id=correlation_id,
                hop=hop,
                available=available,
            )

    async def _run_broker_turn(
        self,
        *,
        source_profile: str,
        target: str,
        target_session: str,
        message: str,
        correlation_id: str,
        hop: int,
        available: set[str],
    ) -> dict[str, Any]:
        self._broker_sessions[(target, target_session)] = correlation_id
        try:
            accepted = await self._target_rpc(target, "chat.send", {
                "sessionKey": target_session,
                "message": message,
                "thinking": False,
                "idempotencyKey": f"profile-{correlation_id}-{hop}",
                "profileDirectory": sorted(available),
                "profileMentions": [],
                "profileMessageContext": {
                    "sourceProfile": source_profile,
                    "correlationId": correlation_id,
                    "hop": hop,
                },
                "allowedTools": list(_SAFE_DELEGATED_TOOLS),
                "turnOrigin": "profile",
            }, 60)
            run_id = str((accepted or {}).get("runId") or "")
            if not run_id:
                raise ProfileHostError(
                    "PROFILE_COLLABORATION_FAILED",
                    "Target profile did not accept the collaboration turn.",
                )
            key = (target, run_id)
            terminal = self._terminal_events.pop(key, None)
            if terminal is None:
                waiter = asyncio.get_running_loop().create_future()
                self._broker_waiters[key] = waiter
                try:
                    terminal = await asyncio.wait_for(waiter, timeout=600)
                except asyncio.TimeoutError as exc:
                    await self._target_rpc(target, "chat.abort", {"runId": run_id}, 30)
                    raise ProfileHostError(
                        "PROFILE_RESPONSE_TIMEOUT",
                        f"Profile '{target}' did not respond in time.",
                    ) from exc
                finally:
                    self._broker_waiters.pop(key, None)
                    self._terminal_events.pop(key, None)
            response = _profile_reply_text(terminal.get("message"))
            if not response:
                raise ProfileHostError(
                    "PROFILE_EMPTY_RESPONSE",
                    f"Profile '{target}' returned an empty response.",
                )
            return {
                "ok": True,
                "targetProfile": target,
                "runId": run_id,
                "response": response,
                "correlationId": correlation_id,
                "hop": hop,
            }
        finally:
            self._broker_sessions.pop((target, target_session), None)

    async def _target_rpc(
        self,
        target: str,
        method: str,
        params: dict[str, Any],
        timeout: float,
    ) -> Any:
        if target == "default":
            if self._primary_rpc is None:
                raise ProfileHostError(
                    "DEFAULT_PROFILE_UNAVAILABLE",
                    "The default profile is not available to bot collaboration.",
                    retryable=True,
                )
            return await self._primary_rpc(method, params, timeout)
        target_runtime = await self._ensure_runtime(target)
        result = await self._rpc(target_runtime, method, params, timeout)
        if method == "chat.send" and isinstance(result, dict):
            run_id = str(result.get("runId") or "")
            if run_id:
                target_runtime.active_runs.add(run_id)
        return result

    async def _close_runtime(self, runtime: _Runtime) -> None:
        for future in runtime.pending.values():
            if not future.done():
                future.set_exception(ProfileHostError("PROFILE_STOPPED", "The profile runtime was stopped."))
        runtime.pending.clear()
        current = asyncio.current_task()
        if runtime.reader_task and runtime.reader_task is not current:
            runtime.reader_task.cancel()
        await runtime.ws.close()
        await runtime.session.close()
        if runtime.owned and runtime.process is not None:
            await self._terminate_process(runtime.process)
        tasks = [
            task for task in (runtime.reader_task, runtime.stdout_task, runtime.stderr_task)
            if task and task is not current
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name != "nt" and process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), _STOP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            try:
                if os.name != "nt" and process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    async def _emit(self, profile: str, event_type: str, data: Any) -> None:
        subscribers = tuple(self._event_subscribers.values())
        if not subscribers:
            return
        bot_id = ""
        if isinstance(data, dict):
            bot_id = str(data.get("botId") or "")
        if not bot_id:
            try:
                bot_id = str(_public_profile(profile).get("botId") or "")
            except (FileNotFoundError, ValueError):
                pass
        envelope = {
            "hostId": self.host_id,
            "profile": profile,
            "botId": bot_id,
            "type": event_type,
            "data": data,
        }
        for callback in subscribers:
            try:
                await callback(envelope)
            except Exception:
                logger.exception("Profile host event callback failed")
