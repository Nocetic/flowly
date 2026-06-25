"""Chat channels module with plugin architecture."""

from flowly.channels.base import BaseChannel
from flowly.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
