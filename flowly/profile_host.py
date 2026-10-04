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
from weakref import WeakValueDictionary
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from loguru import logger

from flowly.exec.env_scrub import sanitize_subprocess_env
from flowly.gateway_logs.notable import notable
from flowly.profile import (
    MAX_NAMED_PROFILES,
    ProfileLimitError,
    create_profile,
    delete_profile,
    describe_profile,
    ensure_profile_bot_id,
    get_or_create_profile_host_id,
    list_profiles,
    profile_display_names,
    read_profile_settings,
    reconcile_runtime_lease,
    resolve_profile_reference,
    set_profile_stopped_by_user,
    update_profile_metadata,
    update_profile_settings,
    validate_profile_name,
)
from flowly.profile_host_contract import (
    MAX_PROFILE_HOPS,
    MAX_PROFILE_MESSAGE_CHARS,
    PROFILE_RPC_TIMEOUTS,
    REMOTE_SESSION_PREFIXES,
    ProfileHostError,
    bounded_timeout,
    is_internal_profile_session,
    validate_profile_rpc,
)
from flowly.session.attention import KINDS as _ATTENTION_KINDS, clean_subject, most_urgent

ProfileEventCallback = Callable[[dict[str, Any]], Awaitable[None]]
ProfileRoomEventCallback = Callable[[dict[str, Any]], Awaitable[None]]
PrimaryRpcCallback = Callable[[str, dict[str, Any], float], Awaitable[Any]]
PrimaryEventLeaseCallback = Callable[[bool], None]
TaskStartedCallback = Callable[[str], Awaitable[None] | None]

_READY_PREFIX = "FLOWLY_LOCAL_RUNTIME_READY "
_START_TIMEOUT_SECONDS = 90
_STOP_TIMEOUT_SECONDS = 8
_DELETE_CONFIRM_TTL_SECONDS = 60
_DELETE_CONFIRM_MAX = 128
# Keeping the agents running. Flowly Desktop manages its machine's agents (it
# runs them in its sandbox) and says so by renewing a short claim; without a
# current claim this host keeps them itself. The grace gives a Desktop that is
# open time to claim before the host starts anything.
_KEEPER_STARTUP_GRACE_SECONDS = 10.0
_KEEPER_INTERVAL_SECONDS = 30.0
_KEEPER_RETRY_MAX_SECONDS = 600.0
_MANAGER_CLAIM_MIN_MS = 10_000
_MANAGER_CLAIM_MAX_MS = 300_000
# A runtime that answers ``sessions.attention`` advertises this.
_ATTENTION_CAPABILITY = "session-attention-v1"
# Events after which what a bot waits on may have changed. A chat terminal is
# in the list because a chat connection request has no event of its own and
# ends with its turn.
_ATTENTION_EVENTS = frozenset({
    "exec.approval.requested",
    "exec.approval.closed",
    "agent.clarify.requested",
    "agent.clarify.closed",
    "plan.approval.requested",
    "plan.updated",
})
_ATTENTION_CHAT_STATES = frozenset({"final", "aborted", "error"})
# Collapse a burst of events (an approval plus its turn ending) into one read.
_ATTENTION_DEBOUNCE_SECONDS = 0.3
_ATTENTION_RPC_TIMEOUT_SECONDS = 10
_ATTENTION_MAX_SESSIONS = 512
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
    # Internal collaboration has no attached approval UI, so it remains
    # read-only. Named profiles can still inspect the installation Board via
    # the authenticated shared-service adapter.
    "board_list",
    "board_get",
    # Correlated follow-ups are safe here: the gateway enforces an explicit
    # profile directory, self-message denial, per-target serialization and a
    # hard three-hop ceiling before forwarding another turn.
    "message_profile",
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


def _autostart_reason(exc: BaseException) -> str:
    """What went wrong starting an agent, in the words the error carries."""
    message = exc.message if isinstance(exc, ProfileHostError) else str(exc)
    return message.strip() or type(exc).__name__


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


def _profile_terminal_state(payload: dict[str, Any]) -> str:
    """The gateway emits interrupted turns as final + aborted, with partial text."""
    if payload.get("aborted") is True or payload.get("state") == "aborted":
        return "aborted"
    if payload.get("failed") is True:
        return "error"
    return str(payload.get("state") or "error")


def _check_profile_terminal(payload: dict[str, Any]) -> None:
    state = _profile_terminal_state(payload)
    if state != "final":
        raise ProfileHostError(
            "PROFILE_COLLABORATION_FAILED",
            f"Profile collaboration ended with {state}.",
            terminal_state=state,
        )


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


def _clean_attention(result: Any) -> dict[str, dict[str, Any]]:
    """Keep only well-formed, owner-visible waits from ``sessions.attention``."""
    sessions = result.get("sessions") if isinstance(result, dict) else None
    if not isinstance(sessions, dict):
        return {}
    clean: dict[str, dict[str, Any]] = {}
    for key, wait in sessions.items():
        if len(clean) >= _ATTENTION_MAX_SESSIONS:
            break
        # The same conversations ``sessions.list`` shows through the host: a
        # client can open what it is told needs the owner. Host-only
        # orchestration turns are answered by the host itself.
        if (not isinstance(key, str) or not key.startswith(REMOTE_SESSION_PREFIXES)
                or is_internal_profile_session(key)):
            continue
        if not isinstance(wait, dict) or wait.get("kind") not in _ATTENTION_KINDS:
            continue
        since, count = wait.get("since"), wait.get("count")
        subject = clean_subject(wait.get("subject"))
        clean[key] = {
            "kind": wait["kind"],
            "since": since if isinstance(since, int) and not isinstance(since, bool) and since >= 0 else 0,
            "count": count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 1,
            **({"subject": subject} if subject else {}),
        }
    return clean


@dataclass(slots=True)
class _Runtime:
    profile: str
    process: asyncio.subprocess.Process | None
    session: aiohttp.ClientSession
    ws: aiohttp.ClientWebSocketResponse
    instance_id: str
    capabilities: frozenset[str] = frozenset()
    owned: bool = True
    state: str = "connected"
    active_runs: set[str] = field(default_factory=set)
    last_used_at: float = field(default_factory=time.time)
    pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    reader_task: asyncio.Task[None] | None = None
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    voice_parent_key: str = field(default='', repr=False)
    event_authority: Any = field(default=None, repr=False)
    # What this bot's conversations wait on from the owner, as last read from
    # the runtime's ``sessions.attention``; see ``_refresh_attention``.
    attention: dict[str, Any] = field(default_factory=dict)
    attention_task: asyncio.Task[None] | None = None
    attention_dirty: bool = False


#: Further pages read to fill one remote `sessions.list` page past internal rows.
_SESSIONS_FILL_ROUNDS = 8


def _remote_sessions(sessions: list) -> list:
    """The conversations a remote client may open: no internal collaboration."""
    return [
        session
        for session in sessions
        if isinstance(session, dict)
        and isinstance(session.get("key"), str)
        and session["key"].startswith(REMOTE_SESSION_PREFIXES)
        and not is_internal_profile_session(session["key"])
    ]


class ProfileHost:
    """Own and proxy named profile gateways for authenticated clients."""

    TASK_GOAL_POLL_SECONDS = 1.0

    def __init__(
        self,
        on_event: ProfileEventCallback | None = None,
        on_room_event: ProfileRoomEventCallback | None = None,
        primary_rpc: PrimaryRpcCallback | None = None,
        primary_event_lease: PrimaryEventLeaseCallback | None = None,
        autostart: bool = False,
    ):
        self.host_id = get_or_create_profile_host_id()
        # Whether this host keeps the owner's agents running: it starts them
        # when it comes up and brings back any that stop on their own, unless
        # Flowly Desktop currently claims them (``profiles.manager.claim``).
        self._autostart = autostart
        self._keeper_task: asyncio.Task[None] | None = None
        self._manager_claim_until = 0.0
        # name -> (reason last reported, monotonic time of the next try, delay)
        self._keeper_failures: dict[str, tuple[str, float, float]] = {}
        self._event_subscribers: dict[str, ProfileEventCallback] = {}
        if on_event is not None:
            self._event_subscribers["owner"] = on_event
        self._primary_rpc = primary_rpc
        self._primary_event_lease = primary_event_lease
        self._default_event_leases: set[str] = set()
        self._runtimes: dict[str, _Runtime] = {}
        self._starting: dict[str, asyncio.Task[_Runtime]] = {}
        self._closing: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._delete_confirmations: dict[str, tuple[str, float]] = {}
        self._broker_waiters: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._interactive_task_sessions: dict[tuple[str, str], str] = {}
        self._terminal_events: dict[tuple[str, str], dict[str, Any]] = {}
        self._broker_target_locks: dict[str, asyncio.Lock] = {}
        self._profile_message_requests: dict[
            tuple[str, str], tuple[str, str, asyncio.Task[Any]]
        ] = {}
        self._task_target_locks: dict[str, asyncio.Lock] = {}
        self._voice_task_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()
        self._shared_service_locks: dict[str, asyncio.Lock] = {}
        self._broker_sessions: dict[tuple[str, str], str] = {}
        # Private, bounded audit state for hidden Board worker turns.  These
        # events never enter public profile streams or session lists; the
        # primary Board may query a sanitized projection by opaque run id.
        self._task_audits: dict[tuple[str, str], dict[str, Any]] = {}
        self._task_audit_sessions: dict[tuple[str, str], dict[str, Any]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._manager_instance = str(uuid.uuid4())
        # Room updates fan out to every attached transport. The constructor
        # slot stays as the owning gateway's subscription so nothing has to
        # register itself twice; other transports add their own.
        self._room_event_subscribers: dict[str, ProfileRoomEventCallback] = {}
        if on_room_event is not None:
            self._room_event_subscribers["owner"] = on_room_event
        from flowly.profile_rooms import ProfileRoomService

        self._rooms = ProfileRoomService(
            target_rpc=self._target_rpc,
            target_prepare=self._prepare_room_member,
            profile_directory=lambda: [profile.name for profile in list_profiles()],
            # The names their owner reads, so a group's members address each
            # other the way the person watching them does.
            profile_labels=profile_display_names,
            on_event=self._emit_room,
            target_is_running=lambda name: self._runtime_is_open(
                self._runtimes.get(name)
            ),
        )

    def collaboration_binding(
        self,
        source: str,
        session_key: str,
        run_id: str,
        *,
        correlation_id: str | None = None,
        hop: int = 0,
        turn_origin: str = "user",
    ):
        """Mint host authority without relying on the originating application."""
        from flowly.profile_collaboration import ProfileRunBinding

        if self._closed:
            raise ProfileHostError("PROFILE_HOST_UNAVAILABLE", "The agent host is stopping.")
        directory = tuple(profile.name for profile in list_profiles())
        if source not in directory:
            raise ProfileHostError("PROFILE_SOURCE_INVALID", "The source profile is not on this host.")
        return ProfileRunBinding(
            client_id="", session_key=session_key, current_profile=source,
            available_profiles=directory, correlation_id=correlation_id or run_id,
            hop=hop, turn_origin=turn_origin, run_id=run_id, broker=self._broker,
        )

    def capabilities(self) -> dict[str, Any]:
        room_capabilities = self._rooms.capabilities()
        return {
            "version": 2,
            "identityGuardVersion": 1,
            "workOutputVersion": 1,
            "goalBindingVersion": 1,
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
                "profiles.manager.claim",
                "profiles.rpc",
                "profiles.cron.notify",
                *self._rooms.methods,
            ],
            "profileRpcMethods": sorted(PROFILE_RPC_TIMEOUTS),
            "rpcIdentityGuard": True,
            "events": ["profile.event", "profile.room"],
            "storageMode": "profile-local",
            "processIsolation": "per-profile-gateway",
            "defaultProfileRpc": "direct",
            "wrappedDefaultProfileRpc": True,
            # Every agent may run at once; the only bound is how many exist.
            "maxConcurrentRuntimes": MAX_NAMED_PROFILES,
            "maxNamedProfiles": MAX_NAMED_PROFILES,
            "profileReadiness": True,
            "profileMessaging": {"authority": "gateway", "version": 1},
            "credentialPolicies": {
                "namedProfile": "isolated",
                "supported": ["isolated"],
                "sharedCredentialBroker": False,
            },
            # An agent runs until its owner stops it: it is never stopped for
            # being idle or to make room, and an owner's stop survives restarts
            # (only profiles.connect starts a stopped agent again). Agents run
            # alongside this host and stop with it.
            "runtimePolicy": {
                "strategy": "always-on",
                "queuesWhileBusy": True,
                "idleEviction": False,
                "autostart": self._autostart,
                "managerClaim": True,
                "stickyStop": True,
            },
            "roomModes": room_capabilities["modes"],
            "roomLimits": {
                "maxMembers": room_capabilities["maxMembers"],
                "councilRounds": room_capabilities["councilRounds"],
                "councilTurns": room_capabilities["councilTurns"],
            },
            "roomStorage": {
                "engine": room_capabilities["storage"],
                "legacyMigration": room_capabilities["legacyJsonMigration"],
            },
            "roomPrewarm": True,
            "profileFeatures": [
                "isolated-workspace",
                "isolated-memory",
                "isolated-sessions",
                "isolated-skills",
                "isolated-credentials",
                "isolated-routines",
                "shared-board",
                "shared-artifacts",
                "profile-messaging",
                "access-policy",
            ],
        }

    def subscribe_events(self, callback: ProfileEventCallback) -> str:
        """Register an isolated event consumer and return its opaque lease id."""
        token = secrets.token_urlsafe(18)
        self._event_subscribers[token] = callback
        return token

    def unsubscribe_events(self, token: str) -> None:
        self._event_subscribers.pop(token, None)

    def subscribe_room_events(self, callback: ProfileRoomEventCallback) -> str:
        """Register a transport that carries group updates, by lease id.

        Groups are watched from more than one place at once — a desktop on
        the gateway socket, a phone on the relay — so every attached
        transport gets its own subscription rather than competing for one.
        """
        token = secrets.token_urlsafe(18)
        self._room_event_subscribers[token] = callback
        return token

    def unsubscribe_room_events(self, token: str) -> None:
        self._room_event_subscribers.pop(token, None)

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
            # ``explicit: false`` is an app opening something of the agent (its
            # chat), not the owner pressing Start: it must not undo their stop.
            return await self.connect(
                _required_string(params, "name").strip(),
                explicit=params.get("explicit") is not False,
            )
        if method == "profiles.manager.claim":
            return self.claim_management(params.get("ttlMs") if isinstance(params, dict) else None)
        if method == "profiles.stop":
            return await self.stop(_required_string(params, "name").strip(), by_user=True)
        if method == "profiles.cron.notify":
            from flowly.push.profile_cron_push import notify_profile_cron

            return await notify_profile_cron(
                self.host_id,
                _required_string(params, "name").strip(),
                _required_string(params, "jobId"),
                _required_string(params, "runId"),
            )
        if method == "profiles.rpc":
            return await self.rpc(
                _required_string(params, "name").strip(),
                params.get("method"),
                params.get("params"),
                params.get("timeoutMs"),
                expected_host_id=params.get("expectedHostId"),
                expected_bot_id=params.get("expectedBotId"),
            )
        if method in self._rooms.methods:
            result = await self._rooms.dispatch(method, params)
            return {"hostId": self.host_id, **result}
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
            if name in self._closing:
                return {
                    "profile": name,
                    "botId": bot_id,
                    "state": "stopping",
                    "owned": True,
                    "activeRuns": 0,
                }
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
            **({"needsInput": urgent} if (urgent := most_urgent(runtime.attention)) else {}),
        }

    async def create(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _required_string(params, "name").strip()
        validate_profile_name(name)
        credential_policy = _optional_string(
            params, "credentialPolicy", "isolated"
        ).strip()
        if credential_policy != "isolated":
            raise ProfileHostError(
                "CREDENTIAL_POLICY_UNSUPPORTED",
                "Named bots currently require isolated credentials.",
            )
        clone_from = params.get("cloneFrom", "default")
        if clone_from is not None:
            if not isinstance(clone_from, str):
                raise ProfileHostError("INVALID_PARAMS", "cloneFrom must be a bot name or null.")
            clone_from = clone_from.strip()
            _validate_profile_selector(clone_from)
        try:
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
                mark_seed=params.get("markSeed"),
                mark_shape=_optional_string(params, "markShape"),
            )
        except ProfileLimitError as exc:
            raise ProfileHostError("PROFILE_LIMIT", str(exc)) from exc
        profile = _public_profile(name)
        settings = _public_settings(name)
        provider = str(settings.get("provider") or "").strip()
        readiness = (
            {
                "runnable": False,
                "authStatus": "login-required",
                "needsLogin": True,
                "missingCapabilities": ["provider-auth"],
                "credentialPolicy": credential_policy,
            }
            if provider == "xai_oauth"
            else {
                # API-key and account-backed providers are resolved by the
                # runtime because their cascade may span config, environment
                # and account state. Do not claim a credential was verified
                # when creation intentionally performs no provider calls.
                "runnable": None,
                "authStatus": "runtime-check-required",
                "needsLogin": False,
                "missingCapabilities": [],
                "credentialPolicy": credential_policy,
            }
        )
        await self._emit(name, "directory", {"action": "created", "profile": profile})
        return {"ok": True, "profile": profile, "readiness": readiness}

    async def settings(self, name: str) -> dict[str, Any]:
        _validate_profile_selector(name)
        return {"settings": _public_settings(name)}

    async def configure(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _required_string(params, "name").strip()
        _validate_profile_selector(name)
        mutable_fields = {
            "provider", "model", "soul", "displayName", "description", "markText", "markTone",
            "markShape",
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
        if any(
            field in params
            for field in ("displayName", "description", "markText", "markTone", "markSeed", "markShape")
        ):
            update_profile_metadata(
                name,
                display_name=params["displayName"] if "displayName" in params else None,
                description=params["description"] if "description" in params else None,
                mark_text=params["markText"] if "markText" in params else None,
                mark_tone=params["markTone"] if "markTone" in params else None,
                mark_seed=params["markSeed"] if "markSeed" in params else None,
                mark_shape=params["markShape"] if "markShape" in params else None,
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
        await self._rooms.remove_profile(name)
        delete_profile(name)
        await self._emit(name, "directory", {"action": "deleted", "botId": current["botId"]})
        return {"ok": True, "deleted": name, "botId": current["botId"]}

    async def connect(self, name: str, *, explicit: bool = True) -> dict[str, Any]:
        """Make the agent ready. ``explicit`` is the owner's own start, the one
        way an agent they stopped runs again; otherwise a stopped agent stays
        stopped and this raises PROFILE_STOPPED."""
        _validate_profile_selector(name)
        if name == "default":
            if self._primary_rpc is None:
                raise ProfileHostError(
                    "DEFAULT_PROFILE_UNAVAILABLE",
                    "The default profile is not available right now.",
                    retryable=True,
                )
        else:
            await self._ensure_runtime(name, explicit=explicit)
        return {"status": self.status(name)}

    async def _prepare_room_member(self, name: str) -> dict[str, Any]:
        """Ready a group member without overriding its owner's stop."""
        _validate_profile_selector(name)
        if name != "default":
            await self._ensure_runtime(name)
        return {"status": self.status(name)}

    def claim_management(self, ttl_ms: Any) -> dict[str, Any]:
        """Flowly Desktop manages this machine's agents for the next ``ttl_ms``.

        Desktop runs its agents in its own sandbox, so while its claim is
        current this host leaves starting them to it. Desktop renews the claim
        while it is connected; once it lapses (Desktop quit, or lost the
        gateway) this host keeps the agents running itself.
        """
        try:
            ttl = int(ttl_ms)
        except (TypeError, ValueError):
            raise ProfileHostError("INVALID_PARAMS", "ttlMs must be a number of milliseconds.") from None
        ttl = max(_MANAGER_CLAIM_MIN_MS, min(ttl, _MANAGER_CLAIM_MAX_MS))
        self._manager_claim_until = time.monotonic() + ttl / 1000
        return {"ok": True, "ttlMs": ttl}

    def _desktop_manages_agents(self) -> bool:
        return time.monotonic() < self._manager_claim_until

    async def stop(self, name: str, *, by_user: bool = False) -> dict[str, Any]:
        """Stop an agent's runtime.

        ``by_user`` is the owner's own stop: the agent then stays stopped,
        across restarts, until it is started again. A stop Flowly makes for
        its own reasons (before a delete or a change) is not remembered.
        """
        _validate_profile_selector(name)
        await self._rooms.stop_for_profile(name)
        if name == "default":
            return {"ok": True, "status": self.status(name)}
        if name not in self._runtimes:
            profile = describe_profile(name)
            if reconcile_runtime_lease(profile.path, profile_name=name):
                # Attach through the authenticated lease first.  This gives us
                # compare-and-stop semantics instead of signalling an
                # unverified PID from a second manager.
                await self._ensure_runtime(name)
        closing = self._closing.get(name)
        if closing is not None:
            await asyncio.gather(asyncio.shield(closing), return_exceptions=True)
        async with self._lock(name):
            starting = self._starting.get(name)
            if starting:
                starting.cancel()
                await asyncio.gather(starting, return_exceptions=True)
            runtime = self._runtimes.get(name)
            if runtime is not None:
                if runtime.active_runs:
                    raise ProfileHostError(
                        "PROFILE_BUSY",
                        "Finish or stop the active turn before changing this bot.",
                        retryable=True,
                    )
                # Even the process owner can miss a turn started by another
                # authenticated manager.  Ask the runtime itself before every
                # mutation/explicit stop; its active-task registry is the only
                # complete authority across all connected clients.
                await self._request_runtime_stop(runtime)
                self._runtimes.pop(name, None)
                await self._close_runtime(runtime)
                if not runtime.owned:
                    await self._wait_for_runtime_release(name, runtime.instance_id)
            else:
                profile = describe_profile(name)
                if reconcile_runtime_lease(profile.path, profile_name=name):
                    raise ProfileHostError(
                        "PROFILE_RUNTIME_CHANGED",
                        "The bot runtime changed while Flowly was preparing to stop it. Try again.",
                        retryable=True,
                    )
        if by_user:
            await asyncio.to_thread(set_profile_stopped_by_user, name, True)
        await self._emit(name, "connection", {"state": "stopped", **({"byUser": True} if by_user else {})})
        return {"ok": True, "status": self.status(name)}

    async def _request_runtime_stop(self, runtime: _Runtime) -> None:
        """Ask the exact runtime to stop itself after an authenticated ACK."""
        if "cooperative-stop-v1" not in runtime.capabilities:
            raise ProfileHostError(
                "PROFILE_RUNTIME_UPGRADE_REQUIRED",
                "Restart this bot with the latest Flowly Desktop before changing it.",
            )
        result = await self._rpc(
            runtime,
            "runtime.stop",
            {"instanceId": runtime.instance_id, "reason": "profile-mutation"},
            15,
        )
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or result.get("stopping") is not True
            or str(result.get("instanceId") or "") != runtime.instance_id
        ):
            raise ProfileHostError(
                "PROFILE_STOP_UNCONFIRMED",
                "The bot did not confirm a safe runtime stop.",
                retryable=True,
            )

    async def _wait_for_runtime_release(self, name: str, instance_id: str) -> None:
        """Wait for the acknowledged process to release its lease, fail closed on replacement."""
        profile = describe_profile(name)
        deadline = asyncio.get_running_loop().time() + 15
        while True:
            lease = reconcile_runtime_lease(profile.path, profile_name=name)
            if lease is None:
                return
            if str(lease.get("instanceId") or "") != instance_id:
                raise ProfileHostError(
                    "PROFILE_RUNTIME_CHANGED",
                    "The bot restarted while Flowly was preparing the change. Try again.",
                    retryable=True,
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise ProfileHostError(
                    "PROFILE_STOP_TIMEOUT",
                    "The bot did not stop in time. Try again.",
                    retryable=True,
                )
            await asyncio.sleep(0.05)

    async def rpc(
        self,
        name: str,
        method: Any,
        params: Any = None,
        timeout_ms: Any = None,
        *,
        expected_host_id: Any = None,
        expected_bot_id: Any = None,
    ) -> Any:
        _validate_profile_selector(name)
        method, safe = validate_profile_rpc(method, params)
        if method in {"subagents.list", "subagents.get"}:
            # The shared runtime socket carries canonical events internally.
            # Each external gateway/relay reader selects its own wire version.
            # Old runtimes ignore the additive parameter and keep legacy events.
            safe["eventVersion"] = 2
        guarded = expected_host_id is not None or expected_bot_id is not None
        if method.startswith(("mcp.", "gmail.", "memory.editor.")) and not guarded:
            raise ProfileHostError("INVALID_PARAMS", "This operation requires the selected profile identities.")
        if guarded:
            if not isinstance(expected_host_id, str) or not expected_host_id or not isinstance(expected_bot_id, str) or not expected_bot_id:
                raise ProfileHostError("INVALID_PARAMS", "Both expected profile identities are required.")
            if expected_host_id != self.host_id:
                raise ProfileHostError("PROFILE_IDENTITY_CHANGED", "The selected host identity changed.")
            self._check_rpc_profile_identity(name, expected_bot_id)
            safe['expectedBotId'] = expected_bot_id
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
        identity_options = {"expected_bot_id": expected_bot_id} if guarded else {}
        result = await self._target_rpc(
            name, method, safe, bounded_timeout(method, timeout_ms), **identity_options
        )
        if expected_bot_id is not None and ensure_profile_bot_id(name).bot_id != expected_bot_id:
            raise ProfileHostError('PROFILE_IDENTITY_CHANGED', 'The selected agent has changed.')
        if method == "sessions.list" and isinstance(result, dict):
            sessions = result.get("sessions")
            if isinstance(sessions, list):
                rows = _remote_sessions(sessions)
                # A page is cut before this filter, so a page of internal
                # sessions would come back short or empty while older
                # conversations remain. Read on until it is full; the
                # cursor stays the agent's, so no row is skipped or repeated.
                limit, cursor = safe.get("limit"), result.get("next")
                for _ in range(_SESSIONS_FILL_ROUNDS):
                    if not isinstance(limit, int) or len(rows) >= limit or not isinstance(cursor, str):
                        break
                    more = await self._target_rpc(
                        name, method, {**safe, "before": cursor}, bounded_timeout(method, timeout_ms),
                        **identity_options,
                    )
                    if not isinstance(more, dict) or not isinstance(more.get("sessions"), list):
                        break
                    rows += _remote_sessions(more["sessions"])
                    cursor = more.get("next")
                if expected_bot_id is not None and ensure_profile_bot_id(name).bot_id != expected_bot_id:
                    raise ProfileHostError('PROFILE_IDENTITY_CHANGED', 'The selected agent has changed.')
                result = {**result, "sessions": rows, **({"next": cursor} if "next" in result else {})}
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
        interactive: bool = False,
        expected_bot_id: str | None = None,
        on_next_instruction: Callable[[str], dict | None] | None = None,
    ) -> dict[str, Any]:
        """Run a dispatcher-owned task in the selected profile.

        This is an internal broker operation, deliberately absent from the
        public profile-host method table. The primary Board owns identity,
        retries, and completion; the worker receives only task text and its
        ordinary sandboxed profile capabilities.
        Interactive voice work uses a visible session, including on the
        default profile, and keeps ordinary user questions and approvals.
        """
        if name != "default" or not interactive:
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
        task_message = prompt if interactive else f"{task_prefix}{prompt}"
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
        session_key = f"desktop:voice-work:{task_id}" if interactive else f"desktop:profile-task:{session_suffix}"
        correlation_id = f"task:{task_id}:{idempotency_key}"
        task_scope = {
            "sessionKey": session_key,
            **({"expectedBotId": expected_bot_id} if expected_bot_id is not None else {}),
        }

        # Keep repeated instructions for one chat ordered, while independent
        # chats on this agent can run together. Weak entries retire idle chats.
        task_lock = (self._voice_task_locks.setdefault((name, session_key), asyncio.Lock())
                     if interactive else self._task_target_locks.setdefault(name, asyncio.Lock()))
        async with task_lock:
            if expected_bot_id is not None:
                try:
                    matches = ensure_profile_bot_id(name).bot_id == expected_bot_id
                except (ValueError, FileNotFoundError):
                    matches = False
                if not matches:
                    raise ProfileHostError("TASK_TARGET_CHANGED", "The assigned agent identity has changed.")
            if interactive:
                reservation = await self._target_rpc(name, 'runtime.voice.reserve', task_scope, 30)
                if (not isinstance(reservation, dict) or reservation.get('reserved') is not True
                        or reservation.get('sessionKey') != session_key):
                    raise ProfileHostError('VOICE_AUTH_UNAVAILABLE', 'The task session owner could not be established.')
            tracked_sessions = self._interactive_task_sessions if interactive else self._broker_sessions
            tracked_sessions[(name, session_key)] = correlation_id
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
                "awaitingGoal": interactive,
            }
            self._task_audit_sessions[(name, session_key)] = audit
            run_id = ""
            try:
                while True:
                    deadline = asyncio.get_running_loop().time() + timeout
                    accepted = await self._target_rpc(name, "chat.send", {
                        "sessionKey": session_key,
                        "message": task_message,
                        "thinking": False,
                        **({"queueForNextTurn": True} if interactive else {}),
                        "idempotencyKey": idempotency_key,
                        "profileDirectory": [],
                        "profileMentions": [],
                        "disabledTools": [] if interactive else list(_PROFILE_TASK_DISABLED_TOOLS),
                        "turnOrigin": "user" if interactive else "task",
                        **({"expectedBotId": expected_bot_id} if expected_bot_id is not None else {}),
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
                    receipt_status = (accepted or {}).get("status")
                    if receipt_status == "status_unknown":
                        raise ProfileHostError(
                            "TASK_RESULT_UNKNOWN",
                            "The accepted task's outcome could not be verified. Check its work conversation.",
                        )
                    key = (name, run_id)
                    if interactive:
                        if receipt_status not in {None, 'accepted', 'running', 'completed', 'aborted', 'error'}:
                            raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The queued task receipt is not supported.')
                        while receipt_status in {None, 'accepted'} and key not in self._terminal_events:
                            remaining = deadline - asyncio.get_running_loop().time()
                            if remaining <= 0:
                                await self._target_rpc(name, 'chat.abort', {**task_scope, 'runId': run_id}, 30)
                                raise ProfileHostError('TASK_TIMEOUT', 'The queued task did not start before its timeout.')
                            await asyncio.sleep(min(self.TASK_GOAL_POLL_SECONDS, remaining))
                            receipt = await self._target_rpc(name, 'chat.command', {**task_scope, 'runId': run_id}, 30)
                            receipt_status = receipt.get('status')
                            if receipt_status not in {'accepted', 'running', 'completed', 'aborted', 'error'}:
                                raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The queued task could not be verified.')
                    cached_terminal = self._terminal_events.get(key)
                    can_ack_start = not interactive or (
                        receipt_status not in {'aborted', 'error'}
                        and (cached_terminal is None or _profile_terminal_state(cached_terminal) == 'final')
                    )
                    if on_started is not None and can_ack_start:
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
                    if receipt_status in ("aborted", "error"):
                        terminal = {"state": receipt_status}
                    if receipt_status == "completed" and terminal is None:
                        history = await self._target_rpc(name, "chat.history", task_scope, 30)
                        messages = history.get("messages", []) if isinstance(history, dict) else []
                        completed = next((m for m in reversed(messages) if isinstance(m, dict)
                            and m.get("role") == "assistant" and m.get("runId") == run_id), None)
                        if completed is None:
                            raise ProfileHostError("TASK_RESULT_UNKNOWN", "The completed task's response is unavailable.")
                        terminal = {"state": "final", "message": completed, "aborted": completed.get("aborted", False)}
                    if terminal is None:
                        waiter = asyncio.get_running_loop().create_future()
                        self._broker_waiters[key] = waiter
                        try:
                            terminal = await asyncio.wait_for(waiter, timeout=timeout)
                        except asyncio.TimeoutError as exc:
                            await self._target_rpc(name, "chat.abort", {**task_scope, "runId": run_id}, 30)
                            raise ProfileHostError(
                                "TASK_TIMEOUT",
                                "The assigned bot did not finish before the task timeout.",
                                retryable=True,
                            ) from exc
                        finally:
                            self._broker_waiters.pop(key, None)
                            self._terminal_events.pop(key, None)
                    _check_profile_terminal(terminal)
                    response = _profile_reply_text(terminal.get("message"))
                    if not response:
                        raise ProfileHostError('TASK_EMPTY_RESPONSE', 'The assigned agent completed without a task handoff.')
                    completed_run_id = run_id
                    if interactive:
                        goal_binding = await self._task_goal_binding(name, task_scope, run_id)
                        response, completed_run_id, next_command = await self._follow_task_goal(
                            name, task_scope, goal_binding=goal_binding,
                            run_id=run_id, response=response, deadline=deadline,
                            next_instruction=(lambda: on_next_instruction(idempotency_key)) if on_next_instruction else None,
                        )
                        if next_command is not None:
                            idempotency_key = next_command['commandId']
                            task_message = next_command['text']
                            continue
                    break
                if not response:
                    raise ProfileHostError(
                        "TASK_EMPTY_RESPONSE",
                        "The assigned bot completed without a task handoff.",
                    )
                if audit["endedAt"] is None:
                    audit.update({"outcome": "ok", "endedAt": time.time()})
                return {
                    "runId": run_id, "response": response,
                    **({"completedRunId": completed_run_id} if completed_run_id != run_id else {}),
                }
            except ProfileHostError as exc:
                if audit["endedAt"] is None:
                    audit.update({
                        "outcome": exc.terminal_state or (
                            "unknown" if exc.code == "TASK_RESULT_UNKNOWN" else "error"
                        ),
                        "endedAt": time.time(), "error": exc.code,
                    })
                raise
            except asyncio.CancelledError:
                if run_id:
                    try:
                        await self._target_rpc(name, "chat.abort", {**task_scope, "runId": run_id}, 30)
                    except Exception:
                        logger.debug("Could not abort cancelled Board task {}", task_id)
                raise
            finally:
                tracked_sessions.pop((name, session_key), None)
                self._task_audit_sessions.pop((name, session_key), None)

    async def _task_goal_snapshot(self, profile: str, scope: dict) -> dict | None:
        result = await self._target_rpc(profile, 'goal.get', scope, 30)
        if not isinstance(result, dict) or 'goal' not in result:
            raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The task goal could not be verified.')
        goal = result['goal']
        if goal is not None and (
            not isinstance(goal, dict) or not isinstance(goal.get('goalId'), str)
            or not goal['goalId'] or goal.get('status') not in {'active', 'paused', 'done', 'cleared'}
            or type(goal.get('revision')) is not int or goal['revision'] < 0
        ):
            raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The task goal could not be verified.')
        return goal

    async def _follow_task_goal(
        self, profile: str, scope: dict, *, goal_binding: dict,
        run_id: str, response: str, deadline: float,
        next_instruction: Callable[[], dict | None] | None = None,
    ) -> tuple[str, str, dict | None]:
        """Keep the task open beyond its first reply, using durable goal proof.

        Audio/connection state is irrelevant here. A goal cleared or replaced
        by another surface is never inferred to have succeeded, and history
        fallback resolves only the run the goal evaluator actually consumed.
        """
        def next_command() -> dict | None:
            command = next_instruction() if next_instruction else None
            if command is not None and (
                not isinstance(command, dict) or not isinstance(command.get('commandId'), str)
                or not 1 <= len(command['commandId']) <= 128
                or not isinstance(command.get('text'), str) or not command['text'].strip()
                or len(command['text']) > MAX_PROFILE_MESSAGE_CHARS
            ):
                raise ProfileHostError('TASK_INVALID', 'The queued task instruction is invalid.')
            return command

        command = next_command()
        if command is not None:
            return response, run_id, command
        if goal_binding['state'] == 'none':
            return response, run_id, None
        goal_id = goal_binding['goalId']

        async def bound_goal() -> dict:
            current = await self._task_goal_snapshot(profile, scope)
            if current is None or current['goalId'] != goal_id:
                current = await self._task_goal_snapshot(profile, {**scope, 'goalId': goal_id})
                if current is None or current['status'] not in {'done', 'cleared'}:
                    raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The task goal changed. Check its work conversation.')
            if current['goalId'] != goal_id or current['revision'] < goal_binding['revision']:
                raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The task goal could not be verified.')
            return current

        goal = await bound_goal()
        while True:
            if goal is None or goal['goalId'] != goal_id:
                raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The task goal changed. Check its work conversation.')
            if goal['status'] == 'paused':
                raise ProfileHostError('TASK_GOAL_PAUSED', 'The task goal is paused. Review its work conversation before continuing.')
            if goal['status'] == 'cleared':
                raise ProfileHostError('TASK_GOAL_CANCELLED', 'The task goal was stopped.', terminal_state='aborted')
            if goal['status'] == 'done':
                final_run_id = goal.get('lastRunId')
                if not isinstance(final_run_id, str) or not final_run_id:
                    raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The completed goal has no verified response identity.')
                final_binding = await self._task_goal_binding(profile, scope, final_run_id)
                if final_binding.get('goalId') != goal_id or final_binding['state'] != 'goal':
                    raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The goal response belongs to a different execution.')
                terminal = ({'state': 'final', 'message': {'content': response}} if final_run_id == run_id else
                            self._terminal_events.pop((profile, final_run_id), None))
                if terminal is None:
                    history = await self._target_rpc(profile, 'chat.history', scope, 30)
                    messages = history.get('messages', []) if isinstance(history, dict) else []
                    message = next((m for m in reversed(messages) if isinstance(m, dict)
                        and m.get('role') == 'assistant' and m.get('runId') == final_run_id), None)
                    if message is None:
                        raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The completed goal response is unavailable.')
                    terminal = {
                        'state': 'final', 'message': message,
                        'aborted': message.get('aborted', False), 'failed': message.get('failed', False),
                    }
                _check_profile_terminal(terminal)
                fresh = await bound_goal()
                if (fresh['revision'] == goal['revision'] and fresh['status'] == 'done'
                        and fresh.get('lastRunId') == final_run_id):
                    return _profile_reply_text(terminal.get('message')), final_run_id, None
                goal = fresh
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The task goal is still active. Check its work conversation.')
            await asyncio.sleep(min(self.TASK_GOAL_POLL_SECONDS, remaining))
            command = next_command()
            if command is not None:
                return response, run_id, command
            goal = await bound_goal()

    async def _task_goal_binding(self, profile: str, scope: dict, run_id: str) -> dict:
        receipt = await self._target_rpc(profile, 'chat.command', {**scope, 'runId': run_id}, 30)
        if not isinstance(receipt, dict) or receipt.get('runId') != run_id:
            raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The task execution receipt could not be verified.')
        if receipt.get('status') in {'error', 'aborted'}:
            _check_profile_terminal({'state': receipt['status']})
        binding = receipt.get('goalBinding')
        if (receipt.get('status') != 'completed' or not isinstance(binding, dict)
                or type(binding.get('version')) is not int or binding['version'] != 1
                or binding.get('state') not in {'none', 'goal'}):
            raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The task goal binding is unavailable. Check its work conversation.')
        if binding['state'] == 'goal' and (
            not isinstance(binding.get('goalId'), str) or not binding['goalId'] or len(binding['goalId']) > 512
            or type(binding.get('revision')) is not int or binding['revision'] < 0
        ):
            raise ProfileHostError('TASK_RESULT_UNKNOWN', 'The task goal binding could not be verified.')
        return binding

    async def reconcile_task(self, *, profile: str, task_id: str, run_id: str, expected_bot_id: str) -> dict:
        """Inspect a voice task without starting a profile, model or new command."""
        from flowly.live_voice.recovery import read_task_result
        from flowly.profile import describe_profile

        if (not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', task_id)
                or not isinstance(run_id, str) or not 1 <= len(run_id) <= 512
                or any(ord(char) < 32 or ord(char) == 127 for char in run_id)):
            raise ProfileHostError('TASK_INVALID', 'Task identity is invalid.')
        before = describe_profile(profile)
        if not expected_bot_id or before.bot_id != expected_bot_id:
            raise ProfileHostError('TASK_TARGET_CHANGED', 'The assigned agent identity has changed.')
        try:
            result = await asyncio.to_thread(read_task_result, before.path, f'desktop:voice-work:{task_id}', run_id)
        except Exception:
            logger.debug('Could not verify durable evidence for task {}', task_id, exc_info=True)
            result = {'runId': run_id, 'status': 'status_unknown'}
        after = describe_profile(profile)
        if after.bot_id != expected_bot_id or before.path != after.path:
            raise ProfileHostError('TASK_TARGET_CHANGED', 'The assigned agent identity has changed.')
        return result

    async def stop_task(self, *, profile: str, task_id: str, run_id: str | None,
                        expected_bot_id: str, goal_binding: dict | None = None,
                        on_goal_binding: Callable[[dict], dict] | None = None) -> dict:
        """Stop the task's goal and verify its worker, without cancelling its waiter."""
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', task_id):
            raise ProfileHostError('TASK_INVALID', 'Task identity is invalid.')
        if ensure_profile_bot_id(profile).bot_id != expected_bot_id:
            raise ProfileHostError('TASK_TARGET_CHANGED', 'The assigned agent identity has changed.')
        params = {'sessionKey': f'desktop:voice-work:{task_id}', 'expectedBotId': expected_bot_id}
        if not run_id:
            return {'status': 'status_unknown'}
        lookup = {**params, 'runId': run_id}
        receipt = await self._target_rpc(profile, 'chat.command', lookup, 30)
        if receipt.get('status') == 'not_found':
            return {'status': 'status_unknown'}
        if receipt.get('status') in {'accepted', 'running'}:
            await self._target_rpc(profile, 'chat.abort', lookup, 30)
            receipt = await self._target_rpc(profile, 'chat.command', lookup, 30)
        goal = await self._task_goal_snapshot(profile, params)
        execution = receipt.get('goalBinding')
        if (isinstance(execution, dict) and type(execution.get('version')) is int
                and execution['version'] == 1 and execution.get('state') in {'none', 'goal', 'pending'}):
            if execution['state'] == 'none':
                if goal_binding is not None and goal_binding.get('goalId') is not None:
                    return {'status': 'status_unknown'}
                # This command never owned a standing goal. A later goal or
                # independent user turn must neither be stopped nor adopted.
                snapshot = await self._target_rpc(profile, 'chat.inflight', params, 30)
                active = snapshot.get('inflight')
                if active and active.get('runId') == run_id:
                    return {'status': 'stopping'}
                worker_status = receipt.get('status')
                if worker_status in {'completed', 'aborted', 'error'}:
                    return {'status': 'stopped', 'workerStatus': worker_status}
                return {'status': 'stopping' if worker_status in {'accepted', 'running'} else 'status_unknown'}
            bound_id = execution.get('goalId')
            if execution['state'] == 'pending' and not bound_id and goal and goal.get('createdByRunId') == run_id:
                bound_id = goal['goalId']
            if not isinstance(bound_id, str) or not bound_id or len(bound_id) > 512:
                return {'status': 'stopping' if receipt.get('status') in {'accepted', 'running'} else 'status_unknown'}
            if goal_binding is not None and goal_binding.get('goalId') != bound_id:
                return {'status': 'status_unknown'}
            if goal is None or goal['goalId'] != bound_id:
                proof = await self._task_goal_snapshot(profile, {**params, 'goalId': bound_id})
                if (proof is None or proof['goalId'] != bound_id or proof['status'] not in {'done', 'cleared'}
                        or receipt.get('status') not in {'completed', 'aborted', 'error'}):
                    return {'status': 'status_unknown'}
                if proof['status'] == 'done':
                    final_run = proof.get('lastRunId')
                    if not isinstance(final_run, str) or not final_run:
                        return {'status': 'status_unknown'}
                    final_binding = await self._task_goal_binding(profile, params, final_run)
                    if final_binding.get('goalId') != bound_id:
                        return {'status': 'status_unknown'}
                    return {'status': 'stopped', 'workerStatus': 'completed'}
                return {'status': 'stopped', 'workerStatus': 'aborted'}
        elif goal_binding is None:
            # A legacy receipt cannot authorize stopping whatever goal happens
            # to be current. Already persisted cancellation bindings stay pinned.
            return {'status': 'status_unknown'}
        if goal_binding is None:
            candidate = {
                'goalId': goal['goalId'] if goal else None,
                'statusAtRequest': goal['status'] if goal else None,
            }
            goal_binding = on_goal_binding(candidate) if on_goal_binding else candidate
        if (goal['goalId'] if goal else None) != goal_binding.get('goalId'):
            return {'status': 'status_unknown'}
        if goal and goal['status'] in {'active', 'paused'}:
            try:
                await self._target_rpc(profile, 'goal.stop', {
                    **params, 'expectedGoalId': goal['goalId'], 'expectedRevision': goal['revision'],
                }, 30)
            except ProfileHostError as exc:
                if exc.code in {'GOAL_NOT_FOUND', 'GOAL_STATE_CHANGED'}:
                    # Reconciliation may re-read the pinned generation. It
                    # must never retry by silently adopting a replacement.
                    return {'status': 'status_unknown'}
                raise
        snapshot = await self._target_rpc(profile, 'chat.inflight', params, 30)
        goal = snapshot.get('goal')
        if (goal.get('goalId') if goal else None) != goal_binding.get('goalId'):
            return {'status': 'status_unknown'}
        active = snapshot.get('inflight')
        owns_active_turn = active and (active.get('runId') == run_id or active.get('goalRun') is not False)
        if owns_active_turn or (goal and goal.get('status') not in {'done', 'cleared'}):
            return {'status': 'stopping'}
        worker_status = receipt.get('status')
        if goal and goal.get('status') == 'cleared' and worker_status == 'completed':
            if goal_binding.get('statusAtRequest') not in {'active', 'paused'}:
                return {'status': 'status_unknown'}
            # The first turn finished but the task's standing goal was stopped.
            worker_status = 'aborted'
        if worker_status in {'completed', 'aborted', 'error'}:
            return {'status': 'stopped', 'workerStatus': worker_status}
        return {'status': 'stopping' if worker_status in {'accepted', 'running'} else 'status_unknown'}

    def task_audit(self, profile: str, run_id: str) -> dict[str, Any] | None:
        """Return the content-free audit projection for one Board run."""
        audit = self._task_audits.get((profile, run_id))
        if audit is None:
            return None
        return {
            "runId": run_id,
            "startedAt": audit.get("startedAt"),
            "endedAt": audit.get("endedAt"),
            "outcome": audit.get("outcome"),
            "error": audit.get("error"),
            "model": audit.get("model"),
            "toolTrace": [dict(item) for item in audit.get("toolTrace", [])],
        }

    def start_keeping_agents(self) -> None:
        """Keep the owner's agents running while this host runs.

        Called once the gateway is serving. After a short grace (time for an
        open Desktop to claim them), and then every so often, it starts every
        agent its owner has not stopped that is not running: on boot, after
        a crash, or once Desktop has quit. Does nothing on a host that does
        not keep agents (see ``autostart``).
        """
        if not self._autostart or self._closed or self._keeper_task is not None:
            return
        self._keeper_task = asyncio.create_task(self._keep_agents(), name="profile-keeper")
        self._background_tasks.add(self._keeper_task)
        self._keeper_task.add_done_callback(self._background_tasks.discard)

    async def _keep_agents(self) -> None:
        await asyncio.sleep(_KEEPER_STARTUP_GRACE_SECONDS)
        while not self._closed:
            if not self._desktop_manages_agents():
                await self._start_wanted_agents()
            await asyncio.sleep(_KEEPER_INTERVAL_SECONDS)

    async def _start_wanted_agents(self) -> None:
        """One pass: start every agent that should run and does not."""
        try:
            profiles = await asyncio.to_thread(list_profiles)
        except Exception as exc:  # noqa: BLE001 — an unreadable directory must not stop the gateway
            self._report_keeper_failure("", f"{type(exc).__name__}: {exc}")
            return
        self._keeper_failures.pop("", None)
        wanted = [
            profile.name for profile in profiles
            if not profile.is_default and profile.has_config and not profile.stopped_by_user
        ]
        # Forget agents that were deleted or stopped by their owner.
        for name in list(self._keeper_failures):
            if name and name not in wanted:
                self._keeper_failures.pop(name, None)
        started = 0
        # One at a time: each start spawns a process and loads a model client,
        # and a host coming up with every agent at once would stall itself.
        for name in wanted:
            if self._closed or self._desktop_manages_agents():
                return
            if self._runtime_is_open(self._runtimes.get(name)) or name in self._starting:
                continue
            failure = self._keeper_failures.get(name)
            if failure is not None and time.monotonic() < failure[1]:
                continue
            try:
                # The owner may have stopped (or deleted) it while the agents
                # before it were starting; their word wins over this list.
                if (await asyncio.to_thread(describe_profile, name)).stopped_by_user:
                    continue
                await self._ensure_runtime(name)
            except asyncio.CancelledError:
                raise
            except FileNotFoundError:
                continue  # deleted meanwhile: nothing to start, nothing wrong
            except Exception as exc:  # noqa: BLE001 — one agent must not keep the others down
                self._report_keeper_failure(name, _autostart_reason(exc))
                continue
            self._keeper_failures.pop(name, None)
            started += 1
        if started:
            notable("agents.started", count=started)

    def _report_keeper_failure(self, name: str, reason: str) -> None:
        """Record a failed start and back off; log it only when it changes.

        An agent that cannot start (a missing key, say) would otherwise write
        the same problem into the log every pass. It is retried less and less
        often, and reported again only if the reason changes.
        """
        previous = self._keeper_failures.get(name)
        delay = _KEEPER_INTERVAL_SECONDS if previous is None else min(previous[2] * 2, _KEEPER_RETRY_MAX_SECONDS)
        self._keeper_failures[name] = (reason, time.monotonic() + delay, delay)
        if previous is not None and previous[0] == reason:
            return
        if name:
            notable("agent.start_failed", name=name, reason=reason)
        else:
            notable("agents.autostart_failed", reason=reason)

    async def shutdown(self) -> None:
        if self._closed:
            return
        await self._rooms.shutdown()
        self._closed = True
        starters = list(self._starting.values())
        for task in starters:
            task.cancel()
        await asyncio.gather(*starters, return_exceptions=True)
        closers = list(self._closing.values())
        await asyncio.gather(*closers, return_exceptions=True)
        self._closing.clear()
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

    async def _ensure_runtime(self, name: str, *, explicit: bool = False) -> _Runtime:
        """The agent's runtime: the open one, an attach to a live one, or a start.

        Only an ``explicit`` start (the owner's own) runs an agent its owner
        stopped. Anything else (a read, a delegated task, a group message, the
        keeper) gets PROFILE_STOPPED instead, so a stop stays a stop.
        """
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
            while True:
                current = self._runtimes.get(name)
                if self._runtime_is_open(current):
                    assert current is not None
                    return current
                closing = self._closing.get(name)
                if closing is not None:
                    await asyncio.gather(asyncio.shield(closing), return_exceptions=True)
                    continue
                pending = self._starting.get(name)
                if pending is not None:
                    break
                lease = reconcile_runtime_lease(profile.path, profile_name=name)
                if lease is None and profile.stopped_by_user and not explicit:
                    raise ProfileHostError(
                        "PROFILE_STOPPED",
                        "This agent is stopped. Start it to use it.",
                    )
                pending = asyncio.create_task(
                    self._attach_runtime(name, lease)
                    if lease
                    else self._start_runtime(name),
                    name=f"profile-{'attach' if lease else 'start'}:{name}",
                )
                self._starting[name] = pending
                break
        try:
            runtime = await pending
        finally:
            if self._starting.get(name) is pending:
                self._starting.pop(name, None)
        if explicit and profile.stopped_by_user:
            # The owner started it again: their stop is over.
            await asyncio.to_thread(set_profile_stopped_by_user, name, False)
        return runtime

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
        raw_capabilities = lease.get("capabilities")
        capabilities = frozenset(
            capability
            for capability in raw_capabilities
            if isinstance(capability, str) and capability
        ) if isinstance(raw_capabilities, list) and len(raw_capabilities) <= 64 else frozenset()
        parent_key = self._parent_authority_key(lease, capabilities)
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
                or (parent_key and not secrets.compare_digest(str(current.get('voiceParentKey') or ''), parent_key))
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
                capabilities=capabilities,
                voice_parent_key=parent_key,
                owned=False,
            )
            runtime.reader_task = asyncio.create_task(
                self._read_runtime(runtime), name=f"profile-reader:{name}"
            )
            self._runtimes[name] = runtime
            await self._emit(name, "connection", {"state": "connected"})
            # Something may already be waiting from before this connection.
            self._schedule_attention_refresh(runtime)
            # This bot is up for its own reasons, which makes it the free
            # moment to settle what groups left behind on it — a membership
            # it lost while stopped, a group deleted the same way, a delete
            # that only got half done. Never a reason to fail the start.
            self._spawn_background(
                self._rooms.retire_orphaned_sessions(name),
                name=f"profile-room-sweep:{name}",
            )
            self._spawn_background(
                self._rooms.retire_orphaned_media(),
                name="profile-room-media-sweep",
            )
            self._spawn_background(
                self._rooms.reclaim_store_space(),
                name="profile-room-space-reclaim",
            )
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
            raw_capabilities = ready.get('capabilities')
            capabilities = frozenset(value for value in raw_capabilities if isinstance(value, str)) if isinstance(raw_capabilities, list) else frozenset()
            parent_key = ''
            if 'voice-owner-hop-v1' in capabilities:
                lease = reconcile_runtime_lease(describe_profile(name).path, profile_name=name)
                if (not lease or lease.get('instanceId') != ready.get('instanceId')
                        or lease.get('pid') != process.pid or lease.get('port') != port
                        or not secrets.compare_digest(str(lease.get('authToken') or ''), token)):
                    raise ProfileHostError('PROFILE_RUNTIME_CHANGED', 'The profile runtime identity changed during startup.')
                parent_key = self._parent_authority_key(lease, capabilities)
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
                voice_parent_key=parent_key,
                capabilities=capabilities,
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
            # Something may already be waiting from before this connection.
            self._schedule_attention_refresh(runtime)
            # This bot is up for its own reasons, which makes it the free
            # moment to settle what groups left behind on it — a membership
            # it lost while stopped, a group deleted the same way, a delete
            # that only got half done. Never a reason to fail the start.
            self._spawn_background(
                self._rooms.retire_orphaned_sessions(name),
                name=f"profile-room-sweep:{name}",
            )
            self._spawn_background(
                self._rooms.retire_orphaned_media(),
                name="profile-room-media-sweep",
            )
            self._spawn_background(
                self._rooms.reclaim_store_space(),
                name="profile-room-space-reclaim",
            )
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

    @staticmethod
    def _parent_authority_key(lease: dict, capabilities: frozenset[str]) -> str:
        from flowly.live_voice.authority import valid_parent_key

        if 'voice-owner-hop-v1' not in capabilities:
            return ''
        key = lease.get('voiceParentKey')
        if not valid_parent_key(key):
            raise ProfileHostError('VOICE_AUTH_UNAVAILABLE', 'The profile runtime cannot preserve voice account identity. Restart it after updating.')
        return key

    async def _rpc(self, runtime: _Runtime, method: str, params: dict[str, Any], timeout: float) -> Any:
        from flowly.live_voice.authority import (
            VoiceAuthorityError,
            current_request_owner,
            sign_profile_hop,
        )

        if runtime.ws.closed:
            raise ProfileHostError("PROFILE_OFFLINE", "The profile runtime is offline.", retryable=True)
        runtime.last_used_at = time.time()
        request_id = secrets.token_urlsafe(18)
        frame = {'type': 'rpc', 'id': request_id, 'method': method, 'params': params}
        owner = current_request_owner()
        if ((method == 'runtime.voice.reserve' or (owner is not None and owner.uid is not None))
                and 'voice-owner-events-v1' not in runtime.capabilities):
            raise ProfileHostError('VOICE_AUTH_UNAVAILABLE', 'Update the profile runtime before opening account-owned voice work.')
        if 'voice-owner-hop-v1' in runtime.capabilities:
            try:
                frame['voiceAuthority'] = sign_profile_hop(runtime.voice_parent_key, runtime.instance_id,
                                                         request_id, method, params, owner)
            except VoiceAuthorityError as exc:
                raise ProfileHostError(exc.code, str(exc)) from None
        elif method == 'runtime.voice.reserve' or (owner is not None and owner.uid is not None):
            raise ProfileHostError('VOICE_AUTH_UNAVAILABLE', 'Update the profile runtime before opening account-owned voice work.')
        future = asyncio.get_running_loop().create_future()
        runtime.pending[request_id] = future
        try:
            await runtime.ws.send_json(frame)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ProfileHostError("PROFILE_RPC_TIMEOUT", "The profile operation timed out.", retryable=True) from exc
        finally:
            runtime.pending.pop(request_id, None)
            runtime.last_used_at = time.time()

    async def _read_runtime(self, runtime: _Runtime) -> None:
        try:
            async for message in runtime.ws:
                if self._runtimes.get(runtime.profile) is not runtime:
                    break
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
                    await self._handle_runtime_event(runtime, frame)
                elif frame.get("type") == "profile_message_request":
                    self._spawn_background(
                        self._handle_broker_request(runtime, frame),
                        name=f"profile-broker:{runtime.profile}",
                    )
                elif frame.get("type") == "shared_service_request":
                    self._spawn_background(
                        self._handle_shared_service_request(runtime, frame),
                        name=f"shared-service:{runtime.profile}",
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

    async def _handle_runtime_event(self, runtime: _Runtime, frame: dict) -> None:
        """Validate the source before it can update waiters, audits or rooms."""
        from flowly.live_voice.authority import HOST_OWNER, VoiceAuthorityError, request_owner_scope
        from flowly.live_voice.event_transport import ProfileEventVerifier
        from flowly.live_voice.events import EventAccess, event_access_scope
        from flowly.profile import describe_profile
        from flowly.session.ownership import SessionAccessError

        try:
            directory = describe_profile(runtime.profile).path / 'sessions'
            if 'voice-owner-events-v1' in runtime.capabilities:
                if runtime.event_authority is None:
                    runtime.event_authority = ProfileEventVerifier(runtime.instance_id, key=runtime.voice_parent_key)
                event = runtime.event_authority.verify(frame, sessions_dir=directory)
                access = event.access
            else:
                # A legacy runtime cannot attest any owned session. Shared
                # sessions continue to work without projecting reader identity.
                data = frame.get('data')
                key = (data.get('sessionKey') or data.get('session_key')) if isinstance(data, dict) else None
                with request_owner_scope(HOST_OWNER):
                    access = EventAccess.capture(key, sessions_dir=directory) if key else EventAccess(owner=HOST_OWNER)
                if any(scope.owner is not None for scope in access.scopes):
                    return
                event = frame
            owner = access.producer()
            if not access.permits(owner):
                return
        except (VoiceAuthorityError, SessionAccessError, OSError, ValueError):
            logger.warning('Profile event authority could not be verified for {}', runtime.profile)
            return
        with request_owner_scope(owner), event_access_scope(access):
            await self._handle_event(runtime, str(event.get('event') or ''), event.get('data'))

    async def handle_primary_frame(self, frame: dict[str, Any]) -> None:
        """Consume a frame from the host gateway's private in-process client."""
        from flowly.live_voice.authority import HOST_OWNER, VoiceAuthorityError, request_owner_scope
        from flowly.live_voice.events import (
            EventAccess,
            ScopedEvent,
            current_event_access,
            event_access_scope,
        )

        if frame.get('type') != 'event':
            return
        access = frame.access if isinstance(frame, ScopedEvent) else current_event_access()
        if access is None:
            data = frame.get('data')
            key = (data.get('sessionKey') or data.get('session_key')) if isinstance(data, dict) else None
            with request_owner_scope(HOST_OWNER):
                access = EventAccess.capture(key)
        try:
            owner = access.producer()
            if not access.permits(owner):
                return
        except VoiceAuthorityError:
            return
        with request_owner_scope(owner), event_access_scope(access):
            await self._handle_primary_frame_event(frame)

    async def _handle_primary_frame_event(self, frame: dict[str, Any]) -> None:
        if frame.get("type") == "event":
            payload = frame.get("data")
            if not isinstance(payload, dict):
                return
            session_key = str(payload.get("sessionKey") or "")
            run_id = str(payload.get("runId") or "")
            if not self._default_event_leases and (
                ("default", session_key) not in self._broker_sessions
                and ("default", session_key) not in self._interactive_task_sessions
                and ("default", run_id) not in self._broker_waiters
                and ("default", run_id) not in self._terminal_events
                and not self._rooms.accepts_event("default", session_key, run_id)
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
        if event == "cron.completed" and profile != "default":
            # Push belongs to the primary host, independently of phone sockets
            # or Desktop windows. Keep network I/O off the runtime reader.
            self._spawn_background(
                self._notify_cron_completion({
                    "name": profile,
                    "jobId": payload.get("jobId"),
                    "runId": payload.get("runId"),
                }),
                name=f"profile-cron-push:{profile}:{run_id}",
            )
        internal_turn = bool(self._broker_sessions.get((profile, session_key)))
        if internal_turn or (profile, session_key) in self._interactive_task_sessions:
            self._capture_task_audit_event(profile, session_key, event, payload)
        if runtime is not None and event == "agent" and run_id:
            runtime.active_runs.add(run_id)
        if event == "chat" and run_id:
            if payload.get("state") in ("final", "aborted", "error"):
                self._cancel_profile_message_requests(profile, session_key, run_id)
                if runtime is not None:
                    runtime.active_runs.discard(run_id)
                key = (profile, run_id)
                waiter = self._broker_waiters.get(key)
                if (waiter is not None or (profile, session_key) in self._broker_sessions
                        or (profile, session_key) in self._interactive_task_sessions):
                    self._terminal_events[key] = payload
                    if len(self._terminal_events) > 256:
                        self._terminal_events.pop(next(iter(self._terminal_events)))
                if waiter is not None and not waiter.done():
                    # Cached and awaited terminals must use the same consumer
                    # validation, regardless of whether the event beat the ACK.
                    waiter.set_result(payload)
            elif runtime is not None:
                runtime.active_runs.add(run_id)
        if runtime is not None:
            runtime.last_used_at = time.time()
            if event in _ATTENTION_EVENTS or (
                event == "chat" and payload.get("state") in _ATTENTION_CHAT_STATES
            ):
                self._schedule_attention_refresh(runtime)
        if await self._rooms.handle_profile_event(profile, event, payload):
            return
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
        """Accumulate sanitized lifecycle metadata for a Board turn."""
        audit = self._task_audit_sessions.get((profile, session_key))
        if audit is None:
            return
        if event == "chat" and payload.get("state") in ("final", "aborted", "error"):
            if audit.get("awaitingGoal"):
                # A turn terminal is not a task terminal while a standing goal
                # may still be evaluating or scheduling its next turn.
                return
            state = _profile_terminal_state(payload)
            if state == "final" and not _profile_reply_text(payload.get("message")):
                state = "error"
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

    async def _notify_cron_completion(self, params: dict[str, Any]) -> None:
        try:
            await self.dispatch("profiles.cron.notify", params)
        except Exception:
            # Notification failures must not break the runtime event reader.
            logger.warning("Profile cron push could not be dispatched for {}", params.get("name"))

    def _schedule_attention_refresh(self, runtime: _Runtime) -> None:
        """Re-read what this bot waits on, soon, if its runtime can say.

        Events only say *that* something may have changed; the runtime's
        ``sessions.attention`` says what is true, so a missed or reordered
        event cannot leave a stale badge behind. Never starts a runtime.
        """
        if _ATTENTION_CAPABILITY not in runtime.capabilities:
            return
        runtime.attention_dirty = True
        if runtime.attention_task is None or runtime.attention_task.done():
            runtime.attention_task = asyncio.create_task(
                self._refresh_attention(runtime),
                name=f"profile-attention:{runtime.profile}",
            )
            self._background_tasks.add(runtime.attention_task)
            runtime.attention_task.add_done_callback(self._background_tasks.discard)

    async def _refresh_attention(self, runtime: _Runtime) -> None:
        while runtime.attention_dirty:
            await asyncio.sleep(_ATTENTION_DEBOUNCE_SECONDS)
            runtime.attention_dirty = False
            if self._runtimes.get(runtime.profile) is not runtime or not self._runtime_is_open(runtime):
                return
            # A status read is not use; it must not keep an idle bot alive.
            last_used_at = runtime.last_used_at
            try:
                result = await self._rpc(runtime, "sessions.attention", {}, _ATTENTION_RPC_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 — a failed read keeps the last answer
                logger.debug("Profile attention unavailable for {}: {}", runtime.profile, exc)
                continue
            finally:
                runtime.last_used_at = last_used_at
            if self._runtimes.get(runtime.profile) is not runtime:
                return
            before = most_urgent(runtime.attention)
            runtime.attention = _clean_attention(result)
            after = most_urgent(runtime.attention)
            if after != before:
                await self._emit(runtime.profile, "needsInput", {"needsInput": after})

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
        key = (source.profile, request_id)
        if key in self._profile_message_requests:
            # The original request will respond on this socket. A repeated
            # frame must not execute the same message a second time.
            return
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        task = asyncio.current_task()
        assert task is not None
        self._profile_message_requests[key] = (
            str(params.get("sourceSessionKey") or ""),
            str(params.get("sourceRunId") or ""),
            task,
        )
        try:
            await self._respond_to_broker_request(source, frame)
        finally:
            self._profile_message_requests.pop(key, None)

    def _cancel_profile_message_requests(
        self, profile: str, session_key: str | None = None, run_id: str | None = None,
    ) -> None:
        for (source, _request_id), (session, run, task) in tuple(self._profile_message_requests.items()):
            if source != profile or (session_key is not None and session != session_key):
                continue
            # Older controllers omit sourceRunId. Never cancel by session
            # alone: a late terminal from the previous turn can arrive after
            # the next turn has already started in that same conversation.
            if run_id is not None and run != run_id:
                continue
            task.cancel()

    async def _respond_to_broker_request(self, source: _Runtime, frame: dict[str, Any]) -> None:
        request_id = str(frame.get("id") or "")
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        try:
            result = await self._broker(source.profile, {**params, "requestId": request_id})
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

    async def _handle_shared_service_request(
        self,
        source: _Runtime,
        frame: dict[str, Any],
    ) -> None:
        """Forward one closed-vocabulary request to the primary runtime."""
        from flowly.shared_service import SharedServiceError, validate_shared_service_request

        request_id = str(frame.get("id") or "")
        params = frame.get("params") if isinstance(frame.get("params"), dict) else {}
        try:
            if str(params.get("sourceProfile") or "").strip() != source.profile:
                raise SharedServiceError(
                    "SHARED_SOURCE_INVALID",
                    "Shared service source failed validation.",
                )
            canonical = validate_shared_service_request(params)
            if self._primary_rpc is None:
                raise SharedServiceError(
                    "SHARED_BROKER_UNAVAILABLE",
                    "The primary shared-service broker is unavailable.",
                )
            async with self._shared_service_locks.setdefault(
                str(canonical["service"]), asyncio.Lock()
            ):
                result = await self._primary_rpc("shared.invoke", canonical, 600)
            response = {
                "type": "shared_service_result",
                "id": request_id,
                "result": result,
            }
        except SharedServiceError as exc:
            response = {
                "type": "shared_service_result",
                "id": request_id,
                "error": {"code": exc.code, "message": exc.message},
            }
        except ProfileHostError as exc:
            response = {
                "type": "shared_service_result",
                "id": request_id,
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception:
            logger.exception("Shared service failed for {}", source.profile)
            response = {
                "type": "shared_service_result",
                "id": request_id,
                "error": {
                    "code": "SHARED_SERVICE_FAILED",
                    "message": "The shared service operation could not be completed.",
                },
            }
        try:
            if not source.ws.closed:
                await source.ws.send_json(response)
        except Exception:
            logger.debug("Shared service result could not return to {}", source.profile)

    async def _broker(self, source_profile: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise ProfileHostError("PROFILE_HOST_UNAVAILABLE", "The agent host is stopping.")
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
            # The caller was asked for an id and may still arrive with the name
            # the user said. Accept it when exactly one bot answers to it —
            # display names are not unique, and two bots called Friday must
            # produce an error rather than a coin flip about which one gets the
            # message.
            resolved = resolve_profile_reference(target)
            if resolved is None:
                raise ProfileHostError("PROFILE_NOT_FOUND", f"Unknown profile '{target}'.")
            target = resolved
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
        target_lock = self._broker_target_locks.setdefault(target, asyncio.Lock())
        if hop > 1 and target_lock.locked():
            # Nested requests must never wait behind a target that may itself
            # be waiting for this source. This also breaks cross-run cycles.
            raise ProfileHostError(
                "PROFILE_COLLABORATION_BUSY",
                f"Profile '{target}' is already handling another agent request.",
                retryable=True,
            )
        async with target_lock:
            if self._closed:
                raise ProfileHostError("PROFILE_HOST_UNAVAILABLE", "The agent host is stopping.")
            return await self._run_broker_turn(
                source_profile=source_profile,
                target=target,
                target_session=target_session,
                message=message,
                correlation_id=correlation_id,
                hop=hop,
                available=available,
                request_id=str(params.get("requestId") or uuid.uuid4()),
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
        request_id: str,
    ) -> dict[str, Any]:
        self._broker_sessions[(target, target_session)] = correlation_id
        # A correlation describes the parent turn, not one message. Distinct
        # calls in that turn must not reuse a child's idempotency/run key.
        run_id = "profile-" + hashlib.sha256(
            f"{source_profile}:{target_session}:{request_id}".encode()
        ).hexdigest()[:32]
        try:
            accepted = await self._target_rpc(target, "chat.send", {
                "sessionKey": target_session,
                "message": message,
                "thinking": False,
                "idempotencyKey": run_id,
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
                    await self._abort_broker_turn(target, run_id)
                    raise ProfileHostError(
                        "PROFILE_RESPONSE_TIMEOUT",
                        f"Profile '{target}' did not respond in time.",
                    ) from exc
                finally:
                    self._broker_waiters.pop(key, None)
                    self._terminal_events.pop(key, None)
            _check_profile_terminal(terminal)
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
        except asyncio.CancelledError:
            # Also use the requested id if cancellation won the race with the
            # ACK. The gateway may already have accepted and started that run.
            await self._abort_broker_turn(target, run_id)
            raise
        finally:
            self._broker_sessions.pop((target, target_session), None)

    async def _abort_broker_turn(self, target: str, run_id: str) -> None:
        try:
            await asyncio.wait_for(
                self._target_rpc(target, "chat.abort", {"runId": run_id}, 30),
                timeout=30,
            )
        except Exception:
            logger.debug("Could not abort cancelled profile collaboration {}", run_id)

    def _check_rpc_profile_identity(self, name: str, expected_bot_id: str) -> None:
        if _public_profile(name).get("botId") != expected_bot_id:
            raise ProfileHostError("PROFILE_IDENTITY_CHANGED", "The selected profile identity changed.")

    async def _target_rpc(
        self,
        target: str,
        method: str,
        params: dict[str, Any],
        timeout: float,
        *,
        expected_bot_id: str | None = None,
    ) -> Any:
        expected = params.get('expectedBotId')
        if expected is not None and ensure_profile_bot_id(target).bot_id != expected:
            raise ProfileHostError('PROFILE_IDENTITY_CHANGED', 'The selected agent has changed.')
        if target == "default":
            if expected_bot_id is not None:
                self._check_rpc_profile_identity(target, expected_bot_id)
            if self._primary_rpc is None:
                raise ProfileHostError(
                    "DEFAULT_PROFILE_UNAVAILABLE",
                    "The default profile is not available to bot collaboration.",
                    retryable=True,
                )
            return await self._primary_rpc(method, params, timeout)
        if method == "chat.abort":
            target_runtime = self._runtimes.get(target)
            if not self._runtime_is_open(target_runtime):
                # Stop is never a reason to start or restart an agent.
                return {"aborted": False}
        else:
            target_runtime = await self._ensure_runtime(target)
        # Runtime startup can yield while a profile is deleted/re-created.
        # Recheck immediately before sending to the captured runtime socket.
        if expected_bot_id is not None:
            self._check_rpc_profile_identity(target, expected_bot_id)
        if expected is not None and ensure_profile_bot_id(target).bot_id != expected:
            raise ProfileHostError('PROFILE_IDENTITY_CHANGED', 'The selected agent has changed.')
        result = await self._rpc(target_runtime, method, params, timeout)
        if method == "chat.send" and isinstance(result, dict):
            run_id = str(result.get("runId") or "")
            if run_id and (target, run_id) not in self._terminal_events:
                target_runtime.active_runs.add(run_id)
        return result

    async def _close_runtime(self, runtime: _Runtime) -> None:
        self._cancel_profile_message_requests(runtime.profile)
        for future in runtime.pending.values():
            if not future.done():
                future.set_exception(ProfileHostError("PROFILE_STOPPED", "The profile runtime was stopped."))
        runtime.pending.clear()
        current = asyncio.current_task()
        if runtime.attention_task and runtime.attention_task is not current:
            runtime.attention_task.cancel()
        runtime.attention = {}
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

    async def _emit_room(self, data: dict[str, Any]) -> None:
        """Publish one host-owned group update to every attached transport.

        A group is reachable over more than one transport at once — a desktop
        on the gateway socket and a phone on the relay are ordinary — so this
        is a fan-out rather than a single sink. One consumer raising must not
        cost the others their update, which is why each is called in its own
        try.
        """
        envelope = {"hostId": self.host_id, **data}
        for callback in list(self._room_event_subscribers.values()):
            try:
                await callback(envelope)
            except Exception:
                logger.exception("Profile room event callback failed")
