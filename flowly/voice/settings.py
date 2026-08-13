"""Which voice provider is configured, and with what.

One resolver per provider, each answering the same two questions: can it speak,
and can it compose. The agent's tool asks nothing else, which is what lets a
second provider be added without the tool learning its name.

The ElevenLabs key first shipped inside the Twilio voice card, where it read as
a telephony detail — you could only reach it by configuring phone calls you
might not want. It has its own connection now, but the old location keeps
working: an upgrade that silently forgets a credential is worse than a slightly
untidy config file. The merge happens at READ time rather than by rewriting the
file, so a rollback to an older build still finds what it wrote, and nobody's
config changes under them while they are looking at it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ElevenLabsSettings",
    "resolve_elevenlabs",
    "resolve_elevenlabs_key",
]


@dataclass(frozen=True, slots=True)
class ElevenLabsSettings:
    """Everything needed to call ElevenLabs, from wherever it was configured."""

    enabled: bool
    api_key: str
    voice_id: str
    model_id: str
    music_model_id: str
    #: True when the key came from the old voice card rather than the
    #: standalone connection. Callers can nudge the user to move it without
    #: breaking anything if they don't.
    from_legacy_voice_config: bool = False

    @property
    def configured(self) -> bool:
        """A key is present. Not the same as being able to generate."""
        return bool(self.api_key)

    @property
    def speech_ready(self) -> bool:
        """Enabled, keyed, and pointed at both a voice and a model.

        All four, because each missing piece fails differently and none can be
        guessed on the user's behalf: a voice is a personal choice, and a model
        is a price.
        """
        return bool(self.enabled and self.api_key and self.voice_id and self.model_id)

    @property
    def music_ready(self) -> bool:
        return bool(self.enabled and self.api_key and self.music_model_id)


def resolve_elevenlabs(integrations: Any) -> ElevenLabsSettings:
    """Merge the standalone connection over the legacy voice-card fields."""
    standalone = getattr(integrations, "elevenlabs", None)
    voice = getattr(integrations, "voice", None)

    new_key = str(getattr(standalone, "api_key", "") or "")
    legacy_key = str(getattr(voice, "elevenlabs_api_key", "") or "")

    # The voice card also carries a chosen TTS voice, and it means the same
    # thing here — so somebody who picked one for calls does not pick it twice.
    new_voice = str(getattr(standalone, "voice_id", "") or "")
    legacy_voice = str(getattr(voice, "tts_voice", "") or "")

    return ElevenLabsSettings(
        # A key inherited from the voice card was never switched on for
        # generation, so inheriting it must not switch generation on either.
        enabled=bool(getattr(standalone, "enabled", False)),
        api_key=new_key or legacy_key,
        voice_id=new_voice or legacy_voice,
        model_id=str(getattr(standalone, "model_id", "") or ""),
        music_model_id=str(getattr(standalone, "music_model_id", "") or ""),
        from_legacy_voice_config=not new_key and bool(legacy_key),
    )


def resolve_elevenlabs_key(integrations: Any) -> str:
    """Just the key — for callers that only need to know whether they can call."""
    return resolve_elevenlabs(integrations).api_key
