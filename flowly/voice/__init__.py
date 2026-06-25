"""Integrated voice system for Flowly.

This module provides voice call handling integrated directly into the agent,
allowing full tool access during voice conversations.
"""

from .types import CallState, CallStatus, VoiceCall, STTResult
from .call_manager import CallManager
from .webhook import create_voice_app, TwilioClient
from .plugin import VoicePlugin
from .stt import STTProvider, create_stt_provider
from .tts import TTSProvider, create_tts_provider

__all__ = [
    # Types
    "CallState",
    "CallStatus",
    "VoiceCall",
    "STTResult",
    # Core
    "CallManager",
    "VoicePlugin",
    # Providers
    "STTProvider",
    "TTSProvider",
    "create_stt_provider",
    "create_tts_provider",
    # Webhook
    "create_voice_app",
    "TwilioClient",
]
