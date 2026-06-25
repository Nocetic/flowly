"""Microsoft Teams channel — Faz 1 incoming-webhook outbound only.

A Teams "Incoming Webhook" is a per-channel HTTPS URL the user generates
inside Microsoft Teams (channel → … → Connectors → Incoming Webhook →
Configure → copy URL). POSTing JSON to that URL posts a message into the
channel. Authentication is the URL itself (unguessable secret).

Faz 1 limitations
-----------------
* Outbound only. The bot can post into Teams; Teams users cannot reply
  back to the bot through the webhook. Conversational bidirectional
  delivery (Bot Framework + Graph API) lands in Faz 2; this module
  stays backward-compatible.
* One channel per webhook URL. If the user wants the bot to post to
  multiple Teams channels they create multiple webhooks today; Faz 2
  introduces routing.

Adapted from an upstream Teams webhook writer; the surface is mapped
onto Flowly's BaseChannel contract so the rest of the gateway routes
through it unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.base import BaseChannel
from flowly.config.schema import TeamsConfig


_REQUEST_TIMEOUT_S = 20.0
_RETRY_BACKOFF_S = 2.0


class TeamsChannel(BaseChannel):
    """Outbound-only Teams channel backed by a single Incoming Webhook URL."""

    name = "teams"

    def __init__(self, config: TeamsConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: TeamsConfig = config
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Mark the channel as running.

        No inbound polling — webhook mode is push-only the other way:
        we post into Teams, Teams doesn't push anything back to us.
        The ``start`` method still exists to satisfy ``BaseChannel`` and
        to set up a shared httpx client.
        """
        if not self.config.webhook_url:
            logger.warning("[Teams] webhook_url is empty — channel disabled")
            return
        if not self.config.webhook_url.startswith("https://"):
            logger.warning(
                "[Teams] webhook_url must be https; got prefix '%s'",
                self.config.webhook_url[:8],
            )
            return

        self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S)
        self._running = True
        label = self.config.default_chat_label or "(no label)"
        logger.info(f"[Teams] Channel ready (outbound webhook → {label})")

    async def stop(self) -> None:
        self._running = False
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:
                logger.warning(f"[Teams] http client close failed: {exc}")
            self._client = None

    async def send(self, msg: OutboundMessage) -> None:
        """POST the message body to the Teams Incoming Webhook URL.

        Single retry on transient errors (5xx, network). Failures are
        logged but never raise — message delivery is best-effort so a
        Teams outage doesn't crash the dispatcher and starve other
        channels.
        """
        if not self._running or self._client is None:
            logger.warning("[Teams] send() called before start() / after stop()")
            return

        body = self._build_payload(msg)
        if body is None:
            return

        # Two attempts: original + one retry on transient failure.
        for attempt in range(2):
            try:
                response = await self._client.post(
                    self.config.webhook_url,
                    json=body,
                )
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Teams webhook returned {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                if response.status_code >= 400:
                    logger.error(
                        "[Teams] webhook rejected ({}): {}",
                        response.status_code,
                        response.text[:200],
                    )
                    return
                logger.debug("[Teams] message delivered ({})", response.status_code)
                return
            except httpx.HTTPError as exc:
                if attempt == 0:
                    logger.warning(
                        "[Teams] webhook POST failed (attempt 1/2): {} — retrying",
                        exc,
                    )
                    await asyncio.sleep(_RETRY_BACKOFF_S)
                    continue
                logger.error("[Teams] webhook POST failed permanently: {}", exc)
                return

    def _build_payload(self, msg: OutboundMessage) -> dict[str, Any] | None:
        """Compose the Teams webhook JSON body.

        Faz 1 sends a plain-text payload (Markdown-rendered by Teams).
        Adaptive Card responses are deferred to Faz 2 along with the
        bidirectional Bot Framework wiring — at that point this method
        will inspect ``msg.metadata['teams']['card']`` and emit a card
        envelope when one is present.

        Media URLs are appended as a "Attachments" list at the end of
        the body so the cdnUrl pipeline (image / video / pdf uploads)
        surfaces a clickable link inside Teams.
        """
        text = (msg.content or "").strip()
        lines: list[str] = []
        if text:
            lines.append(text)

        media_urls = self._collect_media(msg)
        if media_urls:
            if lines:
                lines.append("")
            lines.append("**Attachments**")
            for url in media_urls:
                lines.append(f"- {url}")

        if not lines:
            # Nothing to deliver — relay would silently drop this anyway,
            # but a Teams webhook with empty body 400s instead, so guard.
            logger.debug("[Teams] skipping empty outbound message")
            return None

        return {"text": "\n".join(lines)}

    @staticmethod
    def _collect_media(msg: OutboundMessage) -> list[str]:
        """Pull every URL-like reference off the OutboundMessage.

        ``OutboundMessage.media`` is the canonical list; local file
        paths there don't help Teams (the bot's disk is unreachable
        from a Teams renderer), so only HTTP(S) entries make it through.
        """
        urls: list[str] = []
        for entry in msg.media or []:
            if isinstance(entry, str) and entry.startswith(("http://", "https://")):
                urls.append(entry)
        # Teams-specific metadata override (callers can stuff URLs directly).
        teams_meta = (msg.metadata or {}).get("teams", {}) if msg.metadata else {}
        for url in teams_meta.get("attachments") or []:
            if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in urls:
                urls.append(url)
        return urls
