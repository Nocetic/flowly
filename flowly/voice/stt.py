"""Speech-to-Text providers for voice calls."""

import asyncio
import base64
import httpx
from abc import ABC, abstractmethod
from typing import Protocol, Callable, Awaitable

from loguru import logger

from .types import STTResult

# Retry configuration for transient HTTP errors
_MAX_RETRIES = 2
_RETRY_BASE_DELAY_S = 0.5
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


class STTProvider(ABC):
    """Abstract base class for STT providers."""

    @abstractmethod
    async def transcribe(self, audio_data: bytes) -> STTResult | None:
        """Transcribe audio to text.

        Args:
            audio_data: PCM 16-bit 16kHz mono audio

        Returns:
            Transcription result or None if no speech detected
        """
        pass


class GroqWhisperSTT(STTProvider):
    """Groq Whisper STT provider using batch API.

    Uses Groq's hosted Whisper model for fast transcription.
    """

    def __init__(
        self,
        api_key: str,
        language: str = "tr",
        model: str = "whisper-large-v3-turbo",
    ):
        self.api_key = api_key
        self.language = language
        self.model = model
        self.base_url = "https://api.groq.com/openai/v1"

    async def transcribe(self, audio_data: bytes) -> STTResult | None:
        """Transcribe audio using Groq Whisper API."""
        if len(audio_data) < 1600:  # Less than 0.1s of audio
            return None

        wav_data = self._create_wav(audio_data)

        last_status = 0
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self.base_url}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        files={"file": ("audio.wav", wav_data, "audio/wav")},
                        data={
                            "model": self.model,
                            "language": self.language,
                            "response_format": "json",
                        },
                        timeout=30.0,
                    )
                    last_status = response.status_code

                    if response.status_code == 200:
                        result = response.json()
                        text = result.get("text", "").strip()
                        if not text:
                            return None
                        return STTResult(
                            text=text,
                            confidence=1.0,
                            is_final=True,
                            language=self.language,
                        )

                    if response.status_code not in _RETRYABLE_STATUS_CODES:
                        return None

            except (httpx.ConnectError, httpx.ReadTimeout):
                pass

            if attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY_S * (2 ** attempt)
                logger.warning("Groq STT attempt %d failed (HTTP %s), retrying in %.1fs", attempt + 1, last_status, delay)
                await asyncio.sleep(delay)

        logger.error("Groq STT failed after %d attempts (last HTTP %s)", _MAX_RETRIES + 1, last_status)
        return None

    def _create_wav(self, pcm_data: bytes) -> bytes:
        """Create a WAV file from PCM data.

        Args:
            pcm_data: 16-bit PCM 16kHz mono audio

        Returns:
            Complete WAV file bytes
        """
        import struct

        sample_rate = 16000
        channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * channels * bits_per_sample // 8
        block_align = channels * bits_per_sample // 8
        data_size = len(pcm_data)
        file_size = 36 + data_size

        # WAV header
        header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF',
            file_size,
            b'WAVE',
            b'fmt ',
            16,  # fmt chunk size
            1,   # audio format (PCM)
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b'data',
            data_size,
        )

        return header + pcm_data


class ElevenLabsSTT(STTProvider):
    """ElevenLabs STT provider using batch API.

    Uses ElevenLabs' Scribe model for transcription.
    """

    def __init__(
        self,
        api_key: str,
        language: str = "tr",
        model: str = "scribe_v1",
    ):
        self.api_key = api_key
        self.language = language.split("-")[0]  # ElevenLabs uses ISO 639-1
        self.model = model
        self.base_url = "https://api.elevenlabs.io/v1"

    async def transcribe(self, audio_data: bytes) -> STTResult | None:
        """Transcribe audio using ElevenLabs API."""
        if len(audio_data) < 1600:
            return None

        wav_data = self._create_wav(audio_data)

        last_status = 0
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self.base_url}/speech-to-text",
                        headers={"xi-api-key": self.api_key},
                        files={"audio": ("audio.wav", wav_data, "audio/wav")},
                        data={
                            "model_id": self.model,
                            "language_code": self.language,
                        },
                        timeout=30.0,
                    )
                    last_status = response.status_code

                    if response.status_code == 200:
                        result = response.json()
                        text = result.get("text", "").strip()
                        if not text:
                            return None
                        return STTResult(
                            text=text,
                            confidence=1.0,
                            is_final=True,
                            language=self.language,
                        )

                    if response.status_code not in _RETRYABLE_STATUS_CODES:
                        return None

            except (httpx.ConnectError, httpx.ReadTimeout):
                pass

            if attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY_S * (2 ** attempt)
                logger.warning("ElevenLabs STT attempt %d failed (HTTP %s), retrying in %.1fs", attempt + 1, last_status, delay)
                await asyncio.sleep(delay)

        logger.error("ElevenLabs STT failed after %d attempts (last HTTP %s)", _MAX_RETRIES + 1, last_status)
        return None

    def _create_wav(self, pcm_data: bytes) -> bytes:
        """Create WAV from PCM (same as GroqWhisperSTT)."""
        import struct

        sample_rate = 16000
        channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * channels * bits_per_sample // 8
        block_align = channels * bits_per_sample // 8
        data_size = len(pcm_data)
        file_size = 36 + data_size

        header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF',
            file_size,
            b'WAVE',
            b'fmt ',
            16,
            1,
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b'data',
            data_size,
        )

        return header + pcm_data


def create_stt_provider(
    provider: str,
    api_key: str,
    language: str = "tr",
) -> STTProvider:
    """Factory function to create STT provider.

    Args:
        provider: Provider name (groq, elevenlabs)
        api_key: API key for the provider
        language: Language code

    Returns:
        STT provider instance
    """
    providers = {
        "groq": lambda: GroqWhisperSTT(api_key, language),
        "elevenlabs": lambda: ElevenLabsSTT(api_key, language),
    }

    if provider not in providers:
        raise ValueError(f"Unknown STT provider: {provider}. Choose from: {list(providers.keys())}")

    return providers[provider]()
