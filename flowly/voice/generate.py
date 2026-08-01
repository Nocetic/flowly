"""Turning a provider's audio bytes into something a chat can play.

The provider modules return bytes and nothing else. Everything after that —
where the file lands, what is measured about it, how the folder stays inside
its budget — is the same for every provider and lives here, so adding the
second one is a module and a card rather than a second copy of this.

Deliberately parallel to :mod:`flowly.media.generate`, which does the same job
for images and video. It is not shared with it because the two differ where it
matters: media downloads a URL the provider hosts, and this receives the bytes
directly from the call that made them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from flowly.media.assets import MediaAsset

__all__ = [
    "MODE_MUSIC",
    "MODE_SPEECH",
    "VoiceGenerationError",
    "VoiceResult",
    "generate_elevenlabs",
    "save_audio",
]

MODE_SPEECH = "speech"
MODE_MUSIC = "music"

#: Extension per output format. The provider names formats as
#: ``codec_rate_bitrate``, so the codec is the leading segment.
_EXTENSION_FOR_CODEC = {
    "mp3": ".mp3",
    "opus": ".opus",
    "pcm": ".wav",
    "ulaw": ".wav",
    "alaw": ".wav",
}


class VoiceGenerationError(Exception):
    """Generation failed, with a message meant for the user."""


@dataclass(frozen=True, slots=True)
class VoiceResult:
    assets: list[MediaAsset]
    provider: str
    model: str


def _extension_for(output_format: str) -> str:
    codec = (output_format or "").split("_", 1)[0].strip().lower()
    return _EXTENSION_FOR_CODEC.get(codec, ".mp3")


def save_audio(data: bytes, *, output_format: str, stem: str = "") -> Path:
    """Write generated audio into the media directory and return its path.

    Written under a temporary name and renamed into place, so a crash halfway
    through never leaves a half-file that looks like a finished recording.
    """
    from flowly.media.generate import media_dir

    if not data:
        raise VoiceGenerationError("the provider returned an empty file.")

    directory = media_dir()
    name = stem or f"voice_{uuid.uuid4().hex[:12]}"
    tmp = directory / f".{name}.part"
    try:
        tmp.write_bytes(data)
        final = directory / f"{name}{_extension_for(output_format)}"
        tmp.replace(final)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise VoiceGenerationError(f"could not save the audio: {exc}") from exc
    return final


async def _finish(path: Path, *, provider: str, model: str) -> VoiceResult:
    """Describe the file, then reclaim space around it."""
    from flowly.media.assets import describe
    from flowly.media.generate import prune_after_generation

    asset = describe(path, provider=provider, model=model)
    # Protected, so a piece longer than its own budget fails loudly at the
    # quota instead of vanishing the moment it lands.
    await prune_after_generation([str(path)])
    return VoiceResult(assets=[asset], provider=provider, model=model)


async def generate_elevenlabs(
    settings,
    *,
    mode: str,
    prompt: str,
    voice_id: str = "",
    length_seconds: int = 0,
    instrumental: bool = False,
) -> VoiceResult:
    """Speak or compose with ElevenLabs, and land the result on disk.

    ``settings`` is a :class:`flowly.voice.settings.ElevenLabsSettings`. The
    readiness checks live on it rather than here, because "is this provider
    usable" is a question about configuration, not about this call.
    """
    from flowly.voice.providers import elevenlabs

    try:
        if mode == MODE_MUSIC:
            if not settings.music_ready:
                raise VoiceGenerationError(
                    "no music model is chosen on the ElevenLabs connection."
                )
            model = settings.music_model_id
            data = await elevenlabs.compose_music(
                settings.api_key,
                prompt=prompt,
                length_ms=int(length_seconds) * 1000 if length_seconds else 0,
                model_id=model,
                instrumental=instrumental,
            )
        else:
            if not settings.speech_ready:
                raise VoiceGenerationError(
                    "the ElevenLabs connection needs both a voice and a speech model."
                )
            model = settings.model_id
            data = await elevenlabs.synthesize_speech(
                settings.api_key,
                text=prompt,
                voice_id=voice_id or settings.voice_id,
                model_id=model,
            )
    except elevenlabs.ElevenLabsError as exc:
        raise VoiceGenerationError(str(exc)) from exc

    path = save_audio(
        data,
        output_format=elevenlabs.DEFAULT_OUTPUT_FORMAT,
        stem=f"{'music' if mode == MODE_MUSIC else 'speech'}_{uuid.uuid4().hex[:10]}",
    )
    return await _finish(path, provider="elevenlabs", model=model)
