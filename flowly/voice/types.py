"""Voice system types and data structures."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import asyncio
import time


class CallStatus(str, Enum):
    """Call lifecycle states."""
    INITIATED = "initiated"      # Call started, not yet connected
    RINGING = "ringing"          # Phone is ringing
    ANSWERED = "answered"        # Call was answered
    ACTIVE = "active"            # Call is active, media streaming
    SPEAKING = "speaking"        # Agent is speaking (TTS playing)
    LISTENING = "listening"      # Agent is listening (STT active)
    PROCESSING = "processing"    # Processing user speech
    COMPLETED = "completed"      # Call ended normally
    FAILED = "failed"            # Call failed
    BUSY = "busy"                # Line was busy
    NO_ANSWER = "no_answer"      # No answer


@dataclass
class CallState:
    """Complete state for an active call."""
    call_sid: str
    from_number: str
    to_number: str
    status: CallStatus = CallStatus.INITIATED
    stream_sid: str | None = None

    # Audio state
    is_speaking: bool = False
    is_listening: bool = True
    pending_audio: list[bytes] = field(default_factory=list)

    # TTS queue for serialized playback (bounded to prevent memory exhaustion)
    tts_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=50))
    tts_playing: bool = False

    # Speech detection
    speech_buffer: list[bytes] = field(default_factory=list)
    silence_start: float | None = None
    last_speech_time: float = field(default_factory=time.time)
    # Temporary guard window to avoid immediate re-trigger after playback
    suppress_until: float = 0.0
    # Prevent overlapping transcription turns within a single active call.
    turn_lock: bool = False

    # Session linking
    telegram_chat_id: str | None = None
    session_key: str | None = None

    # Pending greeting to speak when call is answered
    pending_greeting: str | None = None

    # Metadata
    started_at: float = field(default_factory=time.time)
    answered_at: float | None = None
    ended_at: float | None = None
    # Lightweight dedupe state to avoid repeated turns/speech.
    last_user_text: str = ""
    last_user_at: float = 0.0
    duplicate_user_drops: int = 0
    first_user_text_at: float | None = None
    last_spoken_text: str = ""
    last_spoken_at: float = 0.0
    duplicate_tts_drops: int = 0
    duplicate_tts_streak: int = 0

    def __post_init__(self):
        # Ensure tts_queue is always a bounded asyncio.Queue
        if not isinstance(self.tts_queue, asyncio.Queue):
            self.tts_queue = asyncio.Queue(maxsize=50)


@dataclass
class VoiceCall:
    """Voice call representation for external use."""
    call_sid: str
    from_number: str
    to_number: str
    status: str
    duration_seconds: float = 0.0

    @classmethod
    def from_state(cls, state: CallState) -> "VoiceCall":
        """Create from internal CallState."""
        ended = state.ended_at or time.time()
        started = state.answered_at or state.started_at
        return cls(
            call_sid=state.call_sid,
            from_number=state.from_number,
            to_number=state.to_number,
            status=state.status.value,
            duration_seconds=ended - started,
        )


@dataclass
class STTResult:
    """Speech-to-text result."""
    text: str
    confidence: float = 1.0
    is_final: bool = True
    language: str | None = None


@dataclass
class MediaStreamMessage:
    """Twilio Media Stream WebSocket message."""
    event: str
    stream_sid: str | None = None
    sequence_number: int | None = None
    media: dict[str, Any] | None = None
    start: dict[str, Any] | None = None
    stop: dict[str, Any] | None = None
    mark: dict[str, Any] | None = None
