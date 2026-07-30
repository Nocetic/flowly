"""Buzz channel backed by the Buzz CLI and an authenticated Nostr subscription."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from filelock import FileLock, Timeout
from loguru import logger

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels._buzz_nostr import (
    build_auth_event,
    hex_to_npub,
    npub_to_hex,
)
from flowly.channels.base import BaseChannel
from flowly.config.schema import BuzzConfig
from flowly.profile import default_home, get_flowly_home

_MESSAGE_KIND = 9
_MEMBERSHIP_KIND = 44100
_MEMBERSHIP_SUBSCRIPTION = "flowly-buzz-membership"
_FETCH_LIMIT = 50
_SEEN_LIMIT = 500
_DM_DISCOVERY_SWEEPS = 5
_COMMAND_TIMEOUT = 30.0
_AUTH_TIMEOUT = 20.0
_MAX_WS_MESSAGE_BYTES = 2_000_000
_MIN_POLL_INTERVAL = 1.0
_DEFAULT_CREDENTIALS_DIR = Path("~/.config/buzz").expanduser()


@dataclass(frozen=True)
class CommandResult:
    """Result of one non-shell Buzz CLI invocation."""

    returncode: int
    stdout: str
    stderr: str


@dataclass
class ConversationState:
    """Inbound watermark and bounded de-duplication state for one conversation."""

    kind: Literal["group", "dm"]
    last_timestamp: int = 0
    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_pubkey(value: str) -> str | None:
    candidate = str(value or "").strip().lower()
    if not candidate:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", candidate):
        return candidate
    if candidate.startswith("npub1"):
        try:
            return npub_to_hex(candidate)
        except ValueError:
            return None
    return None


def _resolve_buzz_binary(configured: str) -> str:
    """Resolve an explicit path/command, PATH entry, or ``~/bin/buzz``."""
    candidate = str(configured or "").strip()
    if candidate:
        expanded = Path(candidate).expanduser()
        if expanded.is_file():
            return str(expanded)
        found = shutil.which(candidate)
        return found or ""
    found = shutil.which("buzz")
    if found:
        return found
    fallback = Path.home() / "bin" / "buzz"
    return str(fallback) if fallback.is_file() else ""


def _private_key_from_sources(config: BuzzConfig) -> str:
    """Read the Nostr private key without logging it."""
    from flowly.mcp.env_loader import load_flowly_dotenv

    load_flowly_dotenv()
    from_environment = os.getenv("BUZZ_PRIVATE_KEY", "").strip()
    if from_environment:
        return from_environment

    configured_file = (
        os.getenv("BUZZ_CREDENTIALS_FILE", "").strip() or str(config.credentials_file or "").strip()
    )
    if configured_file:
        candidates = [Path(configured_file).expanduser()]
    else:
        profile_candidate = get_flowly_home() / "credentials" / "buzz.json"
        candidates = [profile_candidate]
        try:
            candidates.extend(sorted(_DEFAULT_CREDENTIALS_DIR.glob("*credentials*.json")))
        except OSError:
            pass

    for path in candidates:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        for key in ("nsec", "private_key_hex", "private_key"):
            value = document.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


async def _execute_buzz(
    binary: str,
    args: list[str],
    *,
    relay_url: str,
    private_key: str,
    input_text: str | None = None,
    timeout: float = _COMMAND_TIMEOUT,
) -> CommandResult:
    """Run Buzz without a shell; credentials are carried only in the environment."""
    child_env = os.environ.copy()
    child_env["BUZZ_RELAY_URL"] = relay_url
    child_env["BUZZ_PRIVATE_KEY"] = private_key
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
    except OSError as exc:
        return CommandResult(
            127,
            "",
            json.dumps(
                {
                    "error": "launch_failed",
                    "message": f"Buzz command could not start: {exc}",
                }
            ),
        )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input_text.encode("utf-8") if input_text is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return CommandResult(
            124,
            "",
            json.dumps(
                {
                    "error": "timeout",
                    "message": f"Buzz command timed out after {timeout:g} seconds",
                }
            ),
        )
    return CommandResult(
        process.returncode if process.returncode is not None else 1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _json_objects(raw: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _command_error(result: CommandResult) -> str:
    raw = (result.stderr or "").strip()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("message"):
        category = str(payload.get("error") or "error")
        return f"{category}: {payload['message']} (exit {result.returncode})"
    return raw or f"Buzz command failed with exit {result.returncode}"


def _redact(value: str, *secrets: str) -> str:
    """Keep credentials out of user-facing discovery/probe errors."""
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "••••••••")
    return redacted


def _connection_inputs(values: dict[str, Any]) -> tuple[str, str, str]:
    """Resolve unsaved/saved form values into relay, private key, and CLI path."""
    from flowly.mcp.env_loader import load_flowly_dotenv

    load_flowly_dotenv()
    config = BuzzConfig(
        **{
            key: value
            for key, value in values.items()
            if key in BuzzConfig.model_fields
        }
    )
    relay_url = (
        str(values.get("relay_url") or "").strip()
        or os.getenv("BUZZ_RELAY_URL", "").strip()
        or str(config.relay_url or "").strip()
    )
    private_key = str(values.get("private_key") or "").strip()
    if private_key == "••••••••":
        private_key = ""
    private_key = private_key or _private_key_from_sources(config)
    binary = _resolve_buzz_binary(
        str(values.get("cli_path") or "").strip()
        or os.getenv("BUZZ_CLI_PATH", "").strip()
        or str(config.cli_path or "").strip()
    )
    return relay_url, private_key, binary


async def discover_buzz_channels(values: dict[str, Any]) -> dict[str, Any]:
    """Validate an identity and list joined channels without persisting values."""
    relay_url, private_key, binary = _connection_inputs(values)
    if not relay_url:
        return {
            "ok": False,
            "status": "not_configured",
            "detail": "Community relay URL is required.",
            "identity": None,
            "channels": [],
        }
    if not private_key:
        return {
            "ok": False,
            "status": "not_configured",
            "detail": "Nostr private key is required.",
            "identity": None,
            "channels": [],
        }
    if not binary:
        return {
            "ok": False,
            "status": "not_configured",
            "detail": "Buzz CLI was not found on this machine.",
            "identity": None,
            "channels": [],
        }

    identity_result = await _execute_buzz(
        binary,
        ["users", "get"],
        relay_url=relay_url,
        private_key=private_key,
        timeout=6.0,
    )
    if identity_result.returncode != 0:
        detail = _redact(_command_error(identity_result), private_key)
        lowered = detail.lower()
        status = (
            "auth_failed"
            if any(word in lowered for word in ("auth", "key", "member", "forbidden"))
            else "down"
        )
        return {
            "ok": False,
            "status": status,
            "detail": detail,
            "identity": None,
            "channels": [],
        }

    profiles = _json_objects(identity_result.stdout)
    if not profiles or not profiles[0].get("pubkey"):
        return {
            "ok": False,
            "status": "auth_failed",
            "detail": "This identity is not a member of the community.",
            "identity": None,
            "channels": [],
        }
    profile = profiles[0]
    pubkey = str(profile["pubkey"]).lower()
    try:
        npub = hex_to_npub(pubkey)
    except ValueError:
        return {
            "ok": False,
            "status": "auth_failed",
            "detail": "Buzz returned an invalid identity public key.",
            "identity": None,
            "channels": [],
        }

    channel_result = await _execute_buzz(
        binary,
        ["channels", "list"],
        relay_url=relay_url,
        private_key=private_key,
        timeout=8.0,
    )
    if channel_result.returncode != 0:
        return {
            "ok": False,
            "status": "down",
            "detail": _redact(_command_error(channel_result), private_key),
            "identity": None,
            "channels": [],
        }

    channels_by_id: dict[str, dict[str, str]] = {}
    for item in _json_objects(channel_result.stdout):
        channel_id = str(item.get("channel_id") or "").strip()
        if not channel_id:
            continue
        channels_by_id[channel_id] = {
            "id": channel_id,
            "name": str(item.get("name") or channel_id).strip() or channel_id,
            "description": str(item.get("description") or "").strip(),
        }
    channels = sorted(
        channels_by_id.values(),
        key=lambda channel: (channel["name"].casefold(), channel["id"]),
    )
    display_name = str(profile.get("display_name") or "").strip()
    detail = (
        f"Connected as {display_name}. Found {len(channels)} joined channel"
        f"{'' if len(channels) == 1 else 's'}."
    )
    return {
        "ok": True,
        "status": "ok",
        "detail": detail,
        "identity": {
            "pubkey": pubkey,
            "npub": npub,
            "displayName": display_name,
        },
        "channels": channels,
    }


class BuzzChannel(BaseChannel):
    """Bidirectional Buzz community channel."""

    name = "buzz"

    def __init__(self, config: BuzzConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: BuzzConfig = config
        self._binary = ""
        self._private_key = ""
        self._self_pubkey = ""
        self._self_npub = ""
        self._display_name = ""
        self._conversations: dict[str, ConversationState] = {}
        self._channel_names: dict[str, str] = {}
        self._channel_metadata: dict[str, dict[str, Any]] = {}
        self._user_names: dict[str, str] = {}
        self._poll_task: asyncio.Task | None = None
        self._ws_task: asyncio.Task | None = None
        self._ws_ready: asyncio.Event | None = None
        self._ws_first_attempt: asyncio.Event | None = None
        self._ws_active = False
        self._membership_since = 0
        self._poll_sweeps = 0
        self._stop_event: asyncio.Event | None = None
        self._identity_lock: FileLock | None = None

    def _relay_url(self) -> str:
        return (os.getenv("BUZZ_RELAY_URL") or self.config.relay_url or "").strip()

    def _watch_channels(self) -> list[str]:
        raw: Any = os.getenv("BUZZ_CHANNELS") or self.config.channels
        if isinstance(raw, str):
            raw = raw.split(",")
        return [item.strip() for item in raw or [] if isinstance(item, str) and item.strip()]

    def _home_channel(self) -> str:
        configured = (os.getenv("BUZZ_HOME_CHANNEL") or self.config.home_channel or "").strip()
        if configured:
            return configured
        watched = self._watch_channels()
        if watched:
            return watched[0]
        # Joined channels are populated during _prepare(). This makes scheduled
        # and proactive sends work without asking a user to copy a UUID.
        return next(iter(self._channel_names), "")

    def _transport(self) -> Literal["auto", "websocket", "poll"]:
        raw = (os.getenv("BUZZ_TRANSPORT") or self.config.transport or "auto").strip().lower()
        if raw in {"auto", "websocket", "poll"}:
            return raw  # type: ignore[return-value]
        return "auto"

    def _poll_interval(self) -> float:
        raw: Any = os.getenv("BUZZ_POLL_INTERVAL") or self.config.poll_interval_seconds
        try:
            return max(float(raw), _MIN_POLL_INTERVAL)
        except (TypeError, ValueError):
            return 4.0

    def _group_policy(self) -> Literal["mention", "open"]:
        mention_override = os.getenv("BUZZ_REQUIRE_MENTION")
        if mention_override is not None:
            return "mention" if _truthy(mention_override) else "open"
        return self.config.group_policy

    def _allowed_pubkeys(self) -> set[str]:
        raw: Any = os.getenv("BUZZ_ALLOWED_USERS") or self.config.allow_from
        if isinstance(raw, str):
            raw = raw.split(",")
        return {
            normalized
            for item in raw or []
            if isinstance(item, str) and (normalized := _normalize_pubkey(item)) is not None
        }

    def _allow_all(self) -> bool:
        override = os.getenv("BUZZ_ALLOW_ALL_USERS")
        return _truthy(override) if override is not None else self.config.allow_all_users

    def is_allowed(self, sender_id: str) -> bool:
        """Buzz is default-deny; an empty allow-list does not mean public access."""
        if self._allow_all():
            return True
        normalized = _normalize_pubkey(sender_id)
        return normalized is not None and normalized in self._allowed_pubkeys()

    async def start(self) -> None:
        """Connect, seed watermarks, and keep the selected inbound transports alive."""
        if self._running:
            return
        self._running = True
        self._stop_event = asyncio.Event()
        try:
            if not await self._prepare():
                return
            transport = self._transport()
            if transport == "poll":
                self._ensure_polling()
            else:
                websocket_ready = await self._start_websocket_transport()
                if not websocket_ready and transport == "websocket":
                    logger.error("Buzz WebSocket authentication failed")
                    return
                if not websocket_ready:
                    self._ensure_polling()

            logger.info(
                "Buzz connected to {} as {} via {}",
                self._relay_url(),
                self._display_name or self._self_npub[:16],
                "websocket" if self._ws_active else "polling",
            )
            await self._stop_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Buzz channel stopped after an unexpected error: {}", exc)
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Stop polling/WebSocket activity and release the shared identity lock."""
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        await self._shutdown()

    async def _shutdown(self) -> None:
        self._running = False
        await self._cancel_task("_ws_task")
        await self._cancel_task("_poll_task")
        self._ws_active = False
        if self._identity_lock is not None:
            try:
                self._identity_lock.release()
            except Exception:
                logger.debug("Buzz identity lock release failed")
            self._identity_lock = None

    async def _cancel_task(self, attribute: str) -> None:
        task = getattr(self, attribute)
        if task is None:
            return
        setattr(self, attribute, None)
        if task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _prepare(self) -> bool:
        configured_binary = os.getenv("BUZZ_CLI_PATH") or self.config.cli_path
        self._binary = _resolve_buzz_binary(configured_binary)
        self._private_key = _private_key_from_sources(self.config)
        if not self._relay_url():
            logger.error("Buzz relay URL is not configured")
            return False
        if not self._binary:
            logger.error("Buzz CLI not found; configure cliPath or install buzz on PATH")
            return False
        if not self._private_key:
            logger.error(
                "Buzz private key is not configured in BUZZ_PRIVATE_KEY or a credentials file"
            )
            return False

        identity_result = await self._call(["users", "get"])
        profiles = _json_objects(identity_result.stdout) if identity_result.returncode == 0 else []
        if not profiles or not profiles[0].get("pubkey"):
            logger.error("Buzz identity lookup failed: {}", _command_error(identity_result))
            return False
        self._self_pubkey = str(profiles[0]["pubkey"]).lower()
        self._display_name = str(profiles[0].get("display_name") or "").strip()
        try:
            self._self_npub = hex_to_npub(self._self_pubkey)
        except ValueError:
            logger.error("Buzz returned an invalid public key for the current identity")
            return False
        if not self._acquire_identity_lock():
            return False

        channel_result = await self._call(["channels", "list"])
        if channel_result.returncode != 0:
            logger.error("Buzz channel discovery failed: {}", _command_error(channel_result))
            return False
        listed = _json_objects(channel_result.stdout)
        for item in listed:
            channel_id = str(item.get("channel_id") or "")
            if not channel_id:
                continue
            self._channel_names[channel_id] = str(item.get("name") or channel_id)
            self._channel_metadata[channel_id] = item

        watched = self._watch_channels() or list(self._channel_names)
        if not watched:
            logger.error("Buzz has no joined or configured channels to watch")
            return False
        for channel_id in watched:
            self._channel_names.setdefault(channel_id, channel_id)
            await self._seed_conversation(channel_id, kind="group")
        await self._discover_direct_messages(seed=True)
        return True

    def _acquire_identity_lock(self) -> bool:
        identity = f"{self._relay_url()}:{self._self_pubkey}".encode()
        digest = hashlib.sha256(identity).hexdigest()
        lock_path = default_home() / "locks" / "buzz" / f"{digest}.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(lock_path))
            lock.acquire(timeout=0)
        except Timeout:
            logger.error("Buzz identity is already active in another Flowly profile")
            return False
        except OSError as exc:
            logger.error("Buzz identity lock could not be created: {}", exc)
            return False
        self._identity_lock = lock
        return True

    async def _call(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        timeout: float = _COMMAND_TIMEOUT,
    ) -> CommandResult:
        if not self._private_key:
            self._private_key = _private_key_from_sources(self.config)
        return await _execute_buzz(
            self._binary,
            args,
            relay_url=self._relay_url(),
            private_key=self._private_key,
            input_text=input_text,
            timeout=timeout,
        )

    async def send(self, msg: OutboundMessage) -> None:
        """Send text, replies, and local attachments through the Buzz CLI."""
        if not self._binary:
            self._binary = _resolve_buzz_binary(os.getenv("BUZZ_CLI_PATH") or self.config.cli_path)
        if not self._binary:
            logger.error("Cannot send to Buzz because the Buzz CLI is unavailable")
            return

        target = str(msg.chat_id or "").strip() or self._home_channel()
        if not target:
            logger.error("Cannot send to Buzz because no target or home channel is configured")
            return

        content = msg.content or ""
        args = ["messages", "send", "--channel", target, "--content", "-"]
        if msg.reply_to:
            args.extend(["--reply-to", str(msg.reply_to)])
        links: list[str] = []
        for raw_path in msg.media:
            if raw_path.startswith(("http://", "https://")):
                links.append(raw_path)
                continue
            path = Path(raw_path).expanduser()
            if path.is_file():
                args.extend(["--file", str(path)])
            else:
                logger.warning("Buzz attachment does not exist: {}", raw_path)
        if links:
            content = "\n".join([content, *links]).strip()
        if not content and "--file" not in args:
            logger.warning("Ignoring empty Buzz message")
            return

        result = await self._call(args, input_text=content)
        if result.returncode != 0:
            logger.error("Buzz message delivery failed: {}", _command_error(result))
            return
        response = _json_objects(result.stdout)
        if response and response[0].get("accepted") is False:
            logger.error("Buzz relay rejected the outbound message")
            return
        if response and response[0].get("event_id"):
            self._mark_seen(target, str(response[0]["event_id"]))

    async def _send_reaction(self, chat_id: str, event_id: str, emoji: str) -> bool:
        if not self._binary or not event_id or not emoji:
            return False
        result = await self._call(["reactions", "add", "--event", event_id, "--emoji", emoji])
        if result.returncode != 0:
            logger.debug(
                "Buzz acknowledgement reaction failed for {} in {}",
                event_id[:12],
                chat_id,
            )
            return False
        return True

    async def _seed_conversation(
        self,
        channel_id: str,
        *,
        kind: Literal["group", "dm"],
    ) -> None:
        state = ConversationState(kind=kind)
        self._conversations[channel_id] = state
        result = await self._call(
            [
                "messages",
                "get",
                "--channel",
                channel_id,
                "--limit",
                str(_FETCH_LIMIT),
            ]
        )
        if result.returncode != 0:
            state.last_timestamp = int(time.time())
            logger.warning("Buzz could not seed {}: {}", channel_id, _command_error(result))
            return
        for event in _json_objects(result.stdout):
            event_id = str(event.get("id") or "")
            if event_id:
                state.seen[event_id] = None
            state.last_timestamp = max(
                state.last_timestamp,
                _integer(event.get("created_at")),
            )
            self._maybe_latch_direct_message(channel_id, state, event)
        self._trim_seen(state)

    async def _discover_direct_messages(self, *, seed: bool) -> None:
        result = await self._call(["dms", "list"])
        if result.returncode == 0:
            for item in _json_objects(result.stdout):
                direct_id = str(item.get("dm_id") or item.get("channel_id") or "")
                if not direct_id or direct_id in self._conversations:
                    continue
                if seed:
                    await self._seed_conversation(direct_id, kind="dm")
                else:
                    self._conversations[direct_id] = ConversationState(
                        kind="dm",
                        last_timestamp=int(time.time()),
                    )
                self._channel_names.setdefault(direct_id, "DM")

        fallback = await self._call(["channels", "list"])
        if fallback.returncode != 0:
            return
        for item in _json_objects(fallback.stdout):
            channel_id = str(item.get("channel_id") or "")
            if not channel_id:
                continue
            self._channel_metadata[channel_id] = item
            self._channel_names.setdefault(channel_id, str(item.get("name") or channel_id))
            if channel_id in self._conversations or not self._may_be_direct_message(channel_id):
                continue
            if seed:
                await self._seed_conversation(channel_id, kind="group")
            else:
                self._conversations[channel_id] = ConversationState(
                    kind="group",
                    last_timestamp=int(time.time()),
                )

    def _ensure_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _stop_polling(self) -> None:
        await self._cancel_task("_poll_task")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval())
                self._poll_sweeps += 1
                if self._poll_sweeps % _DM_DISCOVERY_SWEEPS == 0:
                    await self._discover_direct_messages(seed=False)
                for channel_id in list(self._conversations):
                    await self._poll_conversation(channel_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Buzz polling sweep failed; continuing: {}", exc)

    async def _poll_conversation(self, channel_id: str) -> None:
        state = self._conversations.get(channel_id)
        if state is None:
            return
        args = [
            "messages",
            "get",
            "--channel",
            channel_id,
            "--limit",
            str(_FETCH_LIMIT),
        ]
        if state.last_timestamp:
            args.extend(["--since", str(max(state.last_timestamp - 1, 0))])
        result = await self._call(args)
        if result.returncode != 0:
            logger.debug("Buzz poll failed for {}: {}", channel_id, _command_error(result))
            return
        for event in _json_objects(result.stdout):
            await self._handle_event(channel_id, event)
        self._trim_seen(state)

    async def _handle_event(self, channel_id: str, event: dict[str, Any]) -> None:
        state = self._conversations.get(channel_id)
        if state is None:
            return
        event_id = str(event.get("id") or "")
        timestamp = _integer(event.get("created_at"))
        if not event_id or event_id in state.seen:
            return
        state.seen[event_id] = None
        state.last_timestamp = max(state.last_timestamp, timestamp)
        self._trim_seen(state)

        if _integer(event.get("kind")) != _MESSAGE_KIND:
            return
        sender = str(event.get("pubkey") or "").lower()
        content = event.get("content")
        if not sender or not isinstance(content, str) or not content.strip():
            return
        if sender == self._self_pubkey:
            return

        self._maybe_latch_direct_message(channel_id, state, event)
        is_direct = state.kind == "dm"
        if not is_direct and self._group_policy() == "mention" and not self._is_mentioned(content):
            return
        if not self.is_allowed(sender):
            logger.debug("Buzz ignored an unauthorized sender {}", sender[:8])
            return

        clean_content = self._strip_leading_mention(content)
        sender_name = await self._resolve_user_name(sender)
        if not is_direct and sender_name and not clean_content.lstrip().startswith("/"):
            clean_content = f"[{sender_name}]: {clean_content}"
        await self._handle_message(
            sender_id=sender,
            chat_id=channel_id,
            content=clean_content,
            metadata={
                "message_id": event_id,
                "created_at": timestamp,
                "chat_type": state.kind,
                "chat_name": self._channel_names.get(channel_id, channel_id),
                "sender_name": sender_name,
            },
        )
        if self._running and self._binary:
            try:
                await self._send_reaction(channel_id, event_id, "👀")
            except Exception:
                logger.debug("Buzz acknowledgement reaction raised unexpectedly")

    def _may_be_direct_message(self, channel_id: str) -> bool:
        metadata = self._channel_metadata.get(channel_id)
        if metadata is None:
            return channel_id not in self._watch_channels()
        name = str(metadata.get("name") or "").strip()
        description = str(metadata.get("description") or "").strip()
        return name == "DM" and not description

    def _looks_like_direct_message(
        self,
        channel_id: str,
        event: dict[str, Any],
    ) -> bool:
        if not self._self_pubkey or not self._may_be_direct_message(channel_id):
            return False
        if _integer(event.get("kind")) != _MESSAGE_KIND:
            return False
        sender = str(event.get("pubkey") or "").lower()
        if not sender or sender == self._self_pubkey:
            return False
        tags = event.get("tags")
        if not isinstance(tags, list):
            return False
        addressed_to_self = any(
            isinstance(tag, (list, tuple))
            and len(tag) > 1
            and tag[0] == "p"
            and str(tag[1]).lower() == self._self_pubkey
            for tag in tags
        )
        content = event.get("content")
        return addressed_to_self and isinstance(content, str) and not self._is_mentioned(content)

    def _maybe_latch_direct_message(
        self,
        channel_id: str,
        state: ConversationState,
        event: dict[str, Any],
    ) -> None:
        if state.kind == "group" and self._looks_like_direct_message(channel_id, event):
            state.kind = "dm"
            self._channel_names.setdefault(channel_id, "DM")
            logger.info("Buzz conversation {} classified as a direct message", channel_id)

    def _is_mentioned(self, content: str) -> bool:
        lowered = content.lower()
        if self._self_pubkey and self._self_pubkey in lowered:
            return True
        if self._self_npub and self._self_npub.lower() in lowered:
            return True
        if self._display_name:
            pattern = rf"(?<!\w)@?{re.escape(self._display_name.lower())}(?!\w)"
            return re.search(pattern, lowered) is not None
        return False

    def _strip_leading_mention(self, content: str) -> str:
        candidates = [
            candidate
            for candidate in (self._display_name, self._self_npub, self._self_pubkey)
            if candidate
        ]
        text = content.strip()
        if not candidates:
            return text
        alternatives = "|".join(re.escape(item) for item in candidates)
        return re.sub(
            rf"^@?(?:{alternatives})[\s:,]*",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        ).strip()

    async def _resolve_user_name(self, pubkey: str) -> str:
        if pubkey in self._user_names:
            return self._user_names[pubkey]
        name = ""
        if self._binary:
            result = await self._call(["users", "get", "--pubkey", pubkey])
            if result.returncode == 0:
                profiles = _json_objects(result.stdout)
                if profiles:
                    name = str(profiles[0].get("display_name") or "").strip()
        if not name:
            try:
                name = hex_to_npub(pubkey)[:16]
            except ValueError:
                name = pubkey[:16]
        self._user_names[pubkey] = name
        return name

    @staticmethod
    def _trim_seen(state: ConversationState) -> None:
        while len(state.seen) > _SEEN_LIMIT:
            state.seen.popitem(last=False)

    def _mark_seen(self, channel_id: str, event_id: str) -> None:
        state = self._conversations.get(channel_id)
        if state is None:
            return
        state.seen[event_id] = None
        self._trim_seen(state)

    def _websocket_url(self) -> str:
        parsed = urlsplit(self._relay_url())
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
        if scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ValueError("Buzz relay URL must use HTTP(S) or WS(S)")
        return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))

    async def _start_websocket_transport(self) -> bool:
        try:
            import websockets  # noqa: F401

            self._websocket_url()
        except Exception as exc:
            logger.warning("Buzz WebSocket is unavailable: {}", exc)
            return False

        self._ws_ready = asyncio.Event()
        self._ws_first_attempt = asyncio.Event()
        self._membership_since = int(time.time())
        self._ws_task = asyncio.create_task(self._websocket_loop())
        try:
            await asyncio.wait_for(
                self._ws_first_attempt.wait(),
                timeout=_AUTH_TIMEOUT + 5,
            )
        except asyncio.TimeoutError:
            logger.warning("Buzz WebSocket did not finish its first authentication attempt")
        ready = self._ws_ready.is_set()
        if not ready and self._transport() == "websocket":
            await self._cancel_task("_ws_task")
        return ready

    async def _authenticate(self, websocket) -> None:
        raw = await asyncio.wait_for(websocket.recv(), timeout=_AUTH_TIMEOUT)
        challenge_frame = json.loads(raw)
        if (
            not isinstance(challenge_frame, list)
            or len(challenge_frame) < 2
            or challenge_frame[0] != "AUTH"
        ):
            raise ConnectionError("Buzz relay did not issue a NIP-42 challenge")
        auth_tag = os.getenv("BUZZ_AUTH_TAG") or self.config.auth_tag
        event = build_auth_event(
            private_key=self._private_key,
            challenge=str(challenge_frame[1]),
            relay_url=self._websocket_url(),
            auth_tag_json=auth_tag,
        )
        await websocket.send(json.dumps(["AUTH", event], separators=(",", ":")))

        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=_AUTH_TIMEOUT)
            response = json.loads(raw)
            if not isinstance(response, list) or not response:
                continue
            if response[0] == "OK" and len(response) >= 4 and response[1] == event["id"]:
                if response[2] is True:
                    return
                raise ConnectionError(f"Buzz authentication rejected: {response[3]}")
            if response[0] in {"NOTICE", "CLOSED"}:
                detail = response[-1] if len(response) > 1 else "authentication failed"
                raise ConnectionError(f"Buzz authentication failed: {detail}")

    async def _send_conversation_subscription(
        self,
        websocket,
        subscription_id: str,
        channel_id: str,
    ) -> None:
        state = self._conversations.get(channel_id) or ConversationState(kind="group")
        since = max(int(state.last_timestamp or time.time()) - 1, 0)
        request = [
            "REQ",
            subscription_id,
            {"kinds": [_MESSAGE_KIND], "#h": [channel_id], "since": since},
        ]
        await websocket.send(json.dumps(request, separators=(",", ":")))

    async def _subscribe(self, websocket) -> dict[str, str | None]:
        subscriptions: dict[str, str | None] = {}
        for index, channel_id in enumerate(list(self._conversations)):
            subscription_id = f"flowly-buzz-{index}"
            subscriptions[subscription_id] = channel_id
            await self._send_conversation_subscription(
                websocket,
                subscription_id,
                channel_id,
            )
        if self._self_pubkey:
            request = [
                "REQ",
                _MEMBERSHIP_SUBSCRIPTION,
                {
                    "kinds": [_MEMBERSHIP_KIND],
                    "#p": [self._self_pubkey],
                    "since": max(self._membership_since - 1, 0),
                },
            ]
            await websocket.send(json.dumps(request, separators=(",", ":")))
            subscriptions[_MEMBERSHIP_SUBSCRIPTION] = None
        return subscriptions

    async def _handle_membership_event(
        self,
        websocket,
        subscriptions: dict[str, str | None],
        event: dict[str, Any],
    ) -> None:
        self._membership_since = max(
            self._membership_since,
            _integer(event.get("created_at")),
        )
        before = set(self._conversations)
        await self._discover_direct_messages(seed=False)
        for channel_id in self._conversations:
            if channel_id in before:
                continue
            subscription_id = f"flowly-buzz-dm-{len(subscriptions)}"
            subscriptions[subscription_id] = channel_id
            await self._send_conversation_subscription(
                websocket,
                subscription_id,
                channel_id,
            )

    async def _websocket_loop(self) -> None:
        import websockets

        backoff = 1.0
        attempted = False
        try:
            while self._running:
                try:
                    async with websockets.connect(
                        self._websocket_url(),
                        open_timeout=_AUTH_TIMEOUT,
                        close_timeout=5,
                        ping_interval=20,
                        ping_timeout=20,
                        max_size=_MAX_WS_MESSAGE_BYTES,
                    ) as websocket:
                        await self._authenticate(websocket)
                        subscriptions = await self._subscribe(websocket)
                        self._ws_active = True
                        attempted = True
                        if self._ws_ready is not None:
                            self._ws_ready.set()
                        if self._ws_first_attempt is not None:
                            self._ws_first_attempt.set()
                        await self._stop_polling()
                        backoff = 1.0

                        async for raw in websocket:
                            try:
                                frame = json.loads(raw)
                            except (TypeError, ValueError):
                                logger.warning("Buzz ignored a malformed WebSocket frame")
                                continue
                            if not isinstance(frame, list) or not frame:
                                continue
                            if frame[0] == "EVENT" and len(frame) >= 3:
                                subscription_id = str(frame[1])
                                event = frame[2]
                                if not isinstance(event, dict):
                                    continue
                                if subscription_id == _MEMBERSHIP_SUBSCRIPTION:
                                    await self._handle_membership_event(
                                        websocket,
                                        subscriptions,
                                        event,
                                    )
                                    continue
                                channel_id = subscriptions.get(subscription_id)
                                if channel_id:
                                    await self._handle_event(channel_id, event)
                            elif frame[0] == "CLOSED":
                                raise ConnectionError(str(frame[-1]))
                            elif frame[0] == "NOTICE":
                                logger.warning("Buzz relay notice: {}", frame[-1])
                        raise ConnectionError("Buzz WebSocket closed")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._ws_active = False
                    if not attempted:
                        attempted = True
                        if self._ws_first_attempt is not None:
                            self._ws_first_attempt.set()
                    if self._transport() == "auto" and self._running:
                        self._ensure_polling()
                    if self._running:
                        logger.warning(
                            "Buzz WebSocket disconnected; retrying in {:.0f}s: {}",
                            backoff,
                            exc,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
        finally:
            self._ws_active = False
            if not attempted and self._ws_first_attempt is not None:
                self._ws_first_attempt.set()
