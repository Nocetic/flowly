"""Slack channel implementation using Socket Mode."""

import asyncio
import re
import time
from collections import OrderedDict
from typing import Any

from loguru import logger
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.base import BaseChannel
from flowly.channels.slack_format import split_message, to_mrkdwn
from flowly.config.schema import SlackConfig


class SlackChannel(BaseChannel):
    """Slack channel using Socket Mode."""

    name = "slack"

    def __init__(self, config: SlackConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self._web_client: AsyncWebClient | None = None
        self._socket_client: SocketModeClient | None = None
        self._bot_user_id: str | None = None
        # (channel, ts) of already-dispatched events. Slack delivers a mention
        # as BOTH `message` and `app_mention` when both subscriptions exist —
        # whichever copy arrives first wins, the twin is dropped here.
        self._seen_events: OrderedDict[tuple[str, str], None] = OrderedDict()
        # user_id -> display name. Misses are cached too, so a workspace
        # without the users:read scope costs one failed lookup per user,
        # not one per message.
        self._user_names: dict[str, str] = {}
        # channel_id -> (fetched_monotonic, channel_name, member_names). TTL'd
        # so membership changes surface without a call on every reply.
        self._channel_ctx: dict[str, tuple[float, str, list[str]]] = {}

    async def start(self) -> None:
        """Start the Slack Socket Mode client."""
        if not self.config.bot_token or not self.config.app_token:
            logger.error("Slack bot/app token not configured")
            return
        if self.config.mode != "socket":
            logger.error(f"Unsupported Slack mode: {self.config.mode}")
            return

        self._running = True

        self._web_client = AsyncWebClient(token=self.config.bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self._web_client,
        )

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        # Resolve bot user ID for mention handling
        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info(f"Slack bot connected as {self._bot_user_id}")
        except Exception as e:
            logger.warning(f"Slack auth_test failed: {e}")

        logger.info("Starting Slack Socket Mode client...")
        await self._socket_client.connect()

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Slack client."""
        self._running = False
        if self._socket_client:
            try:
                await self._socket_client.close()
            except Exception as e:
                logger.warning(f"Slack socket close failed: {e}")
            self._socket_client = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Slack."""
        if not self._web_client:
            logger.warning("Slack client not running")
            return
        try:
            slack_meta = msg.metadata.get("slack", {}) if msg.metadata else {}
            thread_ts = slack_meta.get("thread_ts")
            channel_type = slack_meta.get("channel_type")
            # Only reply in thread for channel/group messages; DMs don't use threads
            use_thread = thread_ts and channel_type != "im"

            # Convert model markdown to Slack mrkdwn (else **bold**/[x](url)/#
            # render literally), then split anything over Slack's length cap so
            # long replies post as sequential messages instead of erroring.
            mrkdwn = to_mrkdwn(msg.content or "")
            for chunk in split_message(mrkdwn) or [""]:
                await self._web_client.chat_postMessage(
                    channel=msg.chat_id,
                    text=chunk,
                    thread_ts=thread_ts if use_thread else None,
                )
        except Exception as e:
            logger.error(f"Error sending Slack message: {e}")

    async def _on_socket_request(
        self,
        client: SocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """Handle incoming Socket Mode requests."""
        if req.type != "events_api":
            return

        # Acknowledge right away
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        payload = req.payload or {}
        event = payload.get("event") or {}
        event_type = event.get("type")

        # Handle app mentions or plain messages
        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")

        # Ignore bot/system messages (any subtype = not a normal user message)
        if event.get("subtype"):
            return
        if self._bot_user_id and sender_id == self._bot_user_id:
            return

        text = event.get("text") or ""
        logger.debug(
            "Slack event: type={} subtype={} user={} channel={} channel_type={} text={}",
            event_type,
            event.get("subtype"),
            sender_id,
            chat_id,
            event.get("channel_type"),
            text[:80],
        )
        if not sender_id or not chat_id:
            return

        # A mention arrives as both `message` and `app_mention` ONLY when the
        # Slack app subscribed to both events. Dedupe by (channel, ts) instead
        # of dropping the `message` copy outright: for an app without the
        # `app_mention` subscription that copy is the only delivery, and
        # dropping it made the bot ignore every @-mention.
        ts = str(event.get("ts") or "")
        if ts and (str(chat_id), ts) in self._seen_events:
            return

        # `app_mention` events carry no channel_type; they can only fire in
        # channels/groups, never in DMs.
        channel_type = event.get("channel_type") or (
            "channel" if event_type == "app_mention" else ""
        )

        if not self._is_allowed(sender_id, chat_id, channel_type):
            return

        is_group = channel_type != "im"
        should_respond = not is_group or self._should_respond_in_channel(
            event_type, text, chat_id
        )
        # When we won't reply, still passively record the message so the next
        # mention has the channel's context. Scoped to "mention" policy: open
        # answers everything (nothing to observe) and allowlist deliberately
        # ignores channels it isn't in, so neither should buffer.
        observe = (
            is_group
            and not should_respond
            and self.config.group_policy == "mention"
            and getattr(self.config, "group_context", "listen") == "listen"
        )
        if not should_respond and not observe:
            return

        if ts:
            self._seen_events[(str(chat_id), ts)] = None
            while len(self._seen_events) > 500:
                self._seen_events.popitem(last=False)

        text = self._strip_bot_mention(text)
        text = await self._humanize_refs(text)
        sender_name = await self._resolve_user_name(sender_id)
        thread_ts = event.get("thread_ts") or event.get("ts")
        slack_meta: dict[str, Any] = {
            "event": event,
            "thread_ts": thread_ts,
            "channel_type": channel_type,
            "sender_name": sender_name,
        }

        # Observed (not answered): forward bare text tagged group_observe so the
        # loop files it into the channel's context buffer without an LLM turn,
        # a reaction, or a reply.
        if observe:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=text,
                metadata={"slack": slack_meta, "group_observe": True},
            )
            return

        # Answering: label the speaker and attach channel/member awareness so
        # the agent knows the room; raw content gives it no speaker at all.
        content = f"[{sender_name}]: {text}" if (is_group and sender_name) else text
        if is_group:
            ch_name, members = await self._resolve_channel_context(chat_id)
            slack_meta["channel_name"] = ch_name
            slack_meta["members"] = members

        # Add :eyes: reaction to the triggering message (best-effort)
        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name="eyes",
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            logger.debug(f"Slack reactions_add failed: {e}")

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            metadata={"slack": slack_meta},
        )

    async def _resolve_user_name(self, user_id: str) -> str:
        """Human display name for a Slack user id ('' when unresolvable)."""
        if not user_id:
            return ""
        cached = self._user_names.get(user_id)
        if cached is not None:
            return cached
        name = ""
        try:
            if self._web_client:
                info = await self._web_client.users_info(user=user_id)
                user = info.get("user") or {}
                profile = user.get("profile") or {}
                name = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or user.get("real_name")
                    or user.get("name")
                    or ""
                )
        except Exception as e:
            # Typically a missing users:read scope — degrade to raw ids.
            logger.debug(f"Slack users_info failed for {user_id}: {e}")
        if len(self._user_names) > 1000:
            self._user_names.clear()
        self._user_names[user_id] = name
        return name

    async def _humanize_refs(self, text: str) -> str:
        """Rewrite raw Slack refs (`<@U…>`, `<#C…|name>`) as readable names."""
        if not text:
            return text

        for user_id in set(re.findall(r"<@([A-Z0-9]+)>", text)):
            name = await self._resolve_user_name(user_id)
            if name:
                text = text.replace(f"<@{user_id}>", f"@{name}")
        return re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)

    async def _resolve_channel_context(self, chat_id: str) -> tuple[str, list[str]]:
        """(#channel-name, [member names]) for ``chat_id``, TTL-cached.

        Degrades to ('', []) when the channels:read / groups:read scopes are
        missing. Member resolution is capped so joining a huge channel doesn't
        fan out into hundreds of users_info calls on the first reply.
        """
        now = time.monotonic()
        cached = self._channel_ctx.get(chat_id)
        if cached and now - cached[0] < 600:
            return cached[1], cached[2]

        name = ""
        members: list[str] = []
        try:
            if self._web_client:
                info = await self._web_client.conversations_info(channel=chat_id)
                name = ((info.get("channel") or {}).get("name")) or ""
                mem = await self._web_client.conversations_members(
                    channel=chat_id, limit=30
                )
                for uid in (mem.get("members") or [])[:20]:
                    nm = await self._resolve_user_name(uid)
                    if nm:
                        members.append(nm)
        except Exception as e:
            logger.debug(f"Slack channel context failed for {chat_id}: {e}")

        self._channel_ctx[chat_id] = (now, name, members)
        return name, members

    def _is_allowed(self, sender_id: str, chat_id: str, channel_type: str) -> bool:
        if channel_type == "im":
            if not self.config.dm.enabled:
                return False
            if self.config.dm.policy == "allowlist":
                return sender_id in self.config.dm.allow_from
            return True

        # Group / channel messages
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return True

    def _should_respond_in_channel(self, event_type: str, text: str, chat_id: str) -> bool:
        if self.config.group_policy == "open":
            return True
        if self.config.group_policy == "mention":
            if event_type == "app_mention":
                return True
            return self._bot_user_id is not None and f"<@{self._bot_user_id}>" in text
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return False

    def _strip_bot_mention(self, text: str) -> str:
        if not text or not self._bot_user_id:
            return text
        return re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()
