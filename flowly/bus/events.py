"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return f"{self.channel}:{self.chat_id}"


@dataclass
class ConversationInboundMessage(InboundMessage):
    """An inbound message for a conversation whose key is not ``channel:chat_id``.

    A desktop chat is stored under its own conversation key while replies
    travel to a connection id. Work the agent started in that chat (a
    subagent's result or question) must come back to the same conversation —
    its history, its turn lock — not to a session named after the connection.
    """

    conversation_key: str = ""

    @property
    def session_key(self) -> str:  # type: ignore[override]
        return self.conversation_key or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""
    
    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


