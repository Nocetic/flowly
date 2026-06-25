"""Text-to-Speech providers for voice calls."""

import httpx
from abc import ABC, abstractmethod


class TTSProvider(ABC):
    """Abstract base class for TTS providers."""

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Output sample rate in Hz."""
        pass

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """Convert text to speech.

        Args:
            text: Text to synthesize

        Returns:
            PCM 16-bit audio bytes
        """
        pass


class ElevenLabsTTS(TTSProvider):
    """ElevenLabs TTS provider.

    Uses ElevenLabs' text-to-speech API with streaming support.
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str = "EJGs6dWlD5VrB3llhBqB",  # Turkish voice
        model_id: str = "eleven_multilingual_v2",
    ):
        self.api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self.base_url = "https://api.elevenlabs.io"
        self._sample_rate = 24000  # ElevenLabs PCM output

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to speech using ElevenLabs API."""
        url = f"{self.base_url}/v1/text-to-speech/{self.voice_id}"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "xi-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                params={"output_format": "pcm_24000"},
                json={
                    "text": text,
                    "model_id": self.model_id,
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                        "style": 0.0,
                        "use_speaker_boost": True,
                    },
                },
                timeout=30.0,
            )

            if response.status_code != 200:
                raise Exception(f"ElevenLabs TTS error: {response.status_code} - {response.text}")

            return response.content


class OpenAITTS(TTSProvider):
    """OpenAI TTS provider."""

    def __init__(
        self,
        api_key: str,
        voice: str = "nova",
        model: str = "tts-1",
    ):
        self.api_key = api_key
        self.voice = voice
        self.model = model
        self.base_url = "https://api.openai.com/v1"
        self._sample_rate = 24000

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text using OpenAI TTS API."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/audio/speech",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": text,
                    "voice": self.voice,
                    "response_format": "pcm",
                },
                timeout=30.0,
            )

            if response.status_code != 200:
                raise Exception(f"OpenAI TTS error: {response.status_code} - {response.text}")

            return response.content


class DeepgramTTS(TTSProvider):
    """Deepgram TTS provider."""

    def __init__(
        self,
        api_key: str,
        voice: str = "aura-asteria-en",
    ):
        self.api_key = api_key
        self.voice = voice
        self.base_url = "https://api.deepgram.com/v1"
        self._sample_rate = 24000

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text using Deepgram TTS API."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/speak",
                headers={
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": "application/json",
                },
                params={
                    "model": self.voice,
                    "encoding": "linear16",
                    "sample_rate": self._sample_rate,
                },
                json={"text": text},
                timeout=30.0,
            )

            if response.status_code != 200:
                raise Exception(f"Deepgram TTS error: {response.status_code} - {response.text}")

            return response.content


def create_tts_provider(
    provider: str,
    api_key: str,
    voice: str | None = None,
    model: str | None = None,
) -> TTSProvider:
    """Factory function to create TTS provider.

    Args:
        provider: Provider name (elevenlabs, openai, deepgram)
        api_key: API key for the provider
        voice: Voice ID or name (provider-specific)
        model: Model ID (provider-specific)

    Returns:
        TTS provider instance
    """
    providers = {
        "elevenlabs": lambda: ElevenLabsTTS(
            api_key,
            voice_id=voice or "EJGs6dWlD5VrB3llhBqB",
            model_id=model or "eleven_multilingual_v2",
        ),
        "openai": lambda: OpenAITTS(
            api_key,
            voice=voice or "nova",
            model=model or "tts-1",
        ),
        "deepgram": lambda: DeepgramTTS(
            api_key,
            voice=voice or "aura-asteria-en",
        ),
    }

    if provider not in providers:
        raise ValueError(f"Unknown TTS provider: {provider}. Choose from: {list(providers.keys())}")

    return providers[provider]()
