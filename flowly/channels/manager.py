"""Channel manager for coordinating chat channels."""

import asyncio
from typing import Any

from loguru import logger

from flowly.bus.events import OutboundMessage
from flowly.bus.queue import MessageBus
from flowly.channels.base import BaseChannel
from flowly.config.schema import Config


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.
    
    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """
    
    def __init__(self, config: Config, bus: MessageBus):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        
        self._init_channels()
    
    def _init_channels(self) -> None:
        """Initialize channels based on config."""
        
        # Telegram channel
        if self.config.channels.telegram.enabled:
            try:
                from flowly.channels.telegram import TelegramChannel
                self.channels["telegram"] = TelegramChannel(
                    self.config.channels.telegram,
                    self.bus,
                    groq_api_key=self.config.providers.groq.api_key,
                )
                logger.info("Telegram channel enabled")
            except ImportError as e:
                logger.warning(f"Telegram channel not available: {e}")
        
        # WhatsApp channel
        if self.config.channels.whatsapp.enabled:
            try:
                from flowly.channels.whatsapp import WhatsAppChannel
                self.channels["whatsapp"] = WhatsAppChannel(
                    self.config.channels.whatsapp, self.bus
                )
                logger.info("WhatsApp channel enabled")
            except ImportError as e:
                logger.warning(f"WhatsApp channel not available: {e}")

        # iMessage channel (macOS only — reads the local Messages DB)
        if self.config.channels.imessage.enabled:
            import sys
            if sys.platform != "darwin":
                logger.warning("iMessage channel requires macOS — skipping")
            else:
                try:
                    from flowly.channels.imessage import IMessageChannel
                    self.channels["imessage"] = IMessageChannel(
                        self.config.channels.imessage,
                        self.bus,
                        groq_api_key=self.config.providers.groq.api_key,
                    )
                    logger.info("iMessage channel enabled")
                except ImportError as e:
                    logger.warning(f"iMessage channel not available: {e}")

        # Discord channel
        if self.config.channels.discord.enabled:
            try:
                from flowly.channels.discord import DiscordChannel
                self.channels["discord"] = DiscordChannel(
                    self.config.channels.discord, self.bus
                )
                logger.info("Discord channel enabled")
            except ImportError as e:
                logger.warning(f"Discord channel not available: {e}")

        # Slack channel
        if self.config.channels.slack.enabled:
            try:
                from flowly.channels.slack import SlackChannel
                self.channels["slack"] = SlackChannel(
                    self.config.channels.slack, self.bus
                )
                logger.info("Slack channel enabled")
            except ImportError as e:
                logger.warning(f"Slack channel not available: {e}")

        # Web channel (relay mode — no SSH)
        if self.config.channels.web.enabled:
            try:
                from flowly.channels.web import WebChannel
                self.channels["web"] = WebChannel(
                    self.config.channels.web, self.bus
                )
                logger.info("Web channel enabled (relay mode)")
            except ImportError as e:
                logger.warning(f"Web channel not available: {e}")

        # Microsoft Teams channel — Faz 1 incoming-webhook outbound.
        # Inbound (bidirectional Bot Framework) lands in Faz 2.
        if self.config.channels.teams.enabled:
            try:
                from flowly.channels.teams import TeamsChannel
                self.channels["teams"] = TeamsChannel(
                    self.config.channels.teams, self.bus
                )
                logger.info("Teams channel enabled (webhook mode)")
            except ImportError as e:
                logger.warning(f"Teams channel not available: {e}")



    async def start_all(self) -> None:
        """Start WhatsApp channel and the outbound dispatcher."""
        if not self.channels:
            logger.warning("No channels enabled")
            return
        
        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())
        
        # Start WhatsApp channel
        tasks = []
        for name, channel in self.channels.items():
            logger.info(f"Starting {name} channel...")
            tasks.append(asyncio.create_task(channel.start()))
        
        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")
        
        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        
        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info(f"Stopped {name} channel")
            except Exception as e:
                logger.error(f"Error stopping {name}: {e}")
    
    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")
        # Channel names that intentionally have no adapter — their
        # responses are delivered via the gateway's WS final event
        # instead. Listed here so the dispatcher can silently skip them
        # instead of logging a "Unknown channel" warning per tool call.
        _EXPECTED_NO_ADAPTER = {"cli", "tui", "desktop"}
        
        while True:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_outbound(),
                    timeout=1.0
                )
                
                channel = self.channels.get(msg.channel)
                if channel:
                    try:
                        await channel.send(msg)
                    except Exception as e:
                        logger.error(f"Error sending to {msg.channel}: {e}")
                elif msg.channel in _EXPECTED_NO_ADAPTER:
                    # CLI / TUI / direct WS responses are delivered via
                    # the gateway's final chat event, not a channel
                    # adapter. The agent loop still publishes to the bus
                    # so listeners (memory, audit) can observe — but the
                    # adapter-lookup miss is expected, not an error.
                    pass
                else:
                    logger.warning(f"Unknown channel: {msg.channel}")
                    
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
    
    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def set_compact_callback(self, callback: callable) -> None:
        """Set the compaction callback for all channels that support it."""
        for channel in self.channels.values():
            if hasattr(channel, "set_compact_callback"):
                channel.set_compact_callback(callback)

    def set_abort_callback(self, callback: callable) -> None:
        """Set the abort callback for all channels that support it.

        Channels with a Stop affordance (web/desktop, iOS) invoke
        this with the ``run_id`` of the turn to interrupt. The
        gateway wires it to ``agent.mark_aborted`` so the streaming
        loop can break cooperatively while preserving any partial
        text. Channels without a Stop affordance (telegram, slack,
        discord) simply don't expose the entry point.
        """
        for channel in self.channels.values():
            if hasattr(channel, "set_abort_callback"):
                channel.set_abort_callback(callback)
    
    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }
    
    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
