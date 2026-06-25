"""Audio conversion utilities for voice calls.

Handles conversion between:
- Twilio format: mu-law 8kHz mono
- STT format: PCM 16-bit 16kHz mono
- TTS output: PCM 16-bit 24kHz mono (from ElevenLabs)
"""

import audioop
import struct
from typing import Literal

# Twilio uses mu-law 8kHz
TWILIO_SAMPLE_RATE = 8000
# Most STT providers expect 16kHz
STT_SAMPLE_RATE = 16000
# ElevenLabs outputs 24kHz
TTS_SAMPLE_RATE = 24000


def mulaw_to_pcm16(mulaw_data: bytes) -> bytes:
    """Convert mu-law audio to 16-bit PCM.

    Args:
        mulaw_data: mu-law encoded audio bytes

    Returns:
        16-bit PCM audio bytes
    """
    return audioop.ulaw2lin(mulaw_data, 2)


def pcm16_to_mulaw(pcm_data: bytes) -> bytes:
    """Convert 16-bit PCM to mu-law.

    Args:
        pcm_data: 16-bit PCM audio bytes

    Returns:
        mu-law encoded audio bytes
    """
    return audioop.lin2ulaw(pcm_data, 2)


def resample(
    audio_data: bytes,
    from_rate: int,
    to_rate: int,
    sample_width: int = 2
) -> bytes:
    """Resample audio to a different sample rate.

    Args:
        audio_data: Input audio bytes
        from_rate: Source sample rate (Hz)
        to_rate: Target sample rate (Hz)
        sample_width: Bytes per sample (2 for 16-bit)

    Returns:
        Resampled audio bytes
    """
    if from_rate == to_rate:
        return audio_data

    # Use ratecv for resampling
    converted, _ = audioop.ratecv(
        audio_data,
        sample_width,
        1,  # mono
        from_rate,
        to_rate,
        None
    )
    return converted


def twilio_to_stt(mulaw_data: bytes) -> bytes:
    """Convert Twilio mu-law 8kHz to PCM 16kHz for STT.

    Args:
        mulaw_data: mu-law 8kHz audio from Twilio

    Returns:
        PCM 16-bit 16kHz audio for STT
    """
    # Convert mu-law to PCM
    pcm_8k = mulaw_to_pcm16(mulaw_data)
    # Upsample to 16kHz
    pcm_16k = resample(pcm_8k, TWILIO_SAMPLE_RATE, STT_SAMPLE_RATE)
    return pcm_16k


def tts_to_twilio(pcm_data: bytes, tts_sample_rate: int = TTS_SAMPLE_RATE) -> bytes:
    """Convert TTS output PCM to Twilio mu-law 8kHz.

    Args:
        pcm_data: PCM 16-bit audio from TTS provider
        tts_sample_rate: Sample rate of TTS output (default 24kHz for ElevenLabs)

    Returns:
        mu-law 8kHz audio for Twilio
    """
    # Downsample to 8kHz
    pcm_8k = resample(pcm_data, tts_sample_rate, TWILIO_SAMPLE_RATE)
    # Convert to mu-law
    mulaw = pcm16_to_mulaw(pcm_8k)
    return mulaw


def calculate_audio_duration_ms(audio_bytes: bytes, sample_rate: int, sample_width: int = 2) -> int:
    """Calculate audio duration in milliseconds.

    Args:
        audio_bytes: Audio data
        sample_rate: Sample rate in Hz
        sample_width: Bytes per sample

    Returns:
        Duration in milliseconds
    """
    num_samples = len(audio_bytes) // sample_width
    duration_seconds = num_samples / sample_rate
    return int(duration_seconds * 1000)


def detect_speech_energy(pcm_data: bytes, threshold: int = 500) -> bool:
    """Detect if audio contains speech based on RMS energy.

    Args:
        pcm_data: 16-bit PCM audio
        threshold: RMS threshold for speech detection

    Returns:
        True if speech detected
    """
    if len(pcm_data) < 2:
        return False

    try:
        rms = audioop.rms(pcm_data, 2)
        return rms > threshold
    except audioop.error:
        return False


def create_silence(duration_ms: int, sample_rate: int = TWILIO_SAMPLE_RATE) -> bytes:
    """Create silent mu-law audio.

    Args:
        duration_ms: Duration in milliseconds
        sample_rate: Sample rate

    Returns:
        Silent mu-law audio bytes
    """
    num_samples = int(sample_rate * duration_ms / 1000)
    # mu-law silence is 0xFF (127 in linear)
    return bytes([0xFF] * num_samples)
