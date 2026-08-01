"""``voice_generate`` — speech and music, from whichever voice provider is set up.

The schema names nothing provider-specific. A voice and a model are chosen ONCE
on the connection card, from the user's own account, and the agent does not get
to override them mid-conversation: hearing a different narrator than the one you
picked is a wrong result, not a missing nicety.

The file rides the assistant's own reply, the same way a generated image or clip
does, so every surface that can already play media can play this with nothing
new to learn.
"""

from __future__ import annotations

from typing import Any

from flowly.agent.tools.base import Tool

_MODES = ("speech", "music")


class VoiceGenerateTool(Tool):
    def __init__(self, *, elevenlabs: Any = None):
        # One provider today, held by name rather than as "the provider" so the
        # second one is an added branch instead of a rewrite.
        self._elevenlabs = elevenlabs

    @property
    def name(self) -> str:
        return "voice_generate"

    @property
    def description(self) -> str:
        return (
            "Read text aloud, or compose a piece of music, as an audio file. The "
            "file is attached to your reply automatically — do NOT call the message "
            "tool for it. Just call this, then say what you made. Write speech text "
            "in the SAME language the user is speaking; the configured voice handles "
            "it. Music prompts work best in English."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "For 'speech', the exact words to say — written out, with "
                        "punctuation, in the language they should be spoken. For "
                        "'music', a description of the piece: genre, mood, "
                        "instruments, tempo."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": list(_MODES),
                    "description": (
                        "'speech' reads the prompt aloud in the configured voice. "
                        "'music' composes an instrumental or song from it."
                    ),
                    "default": "speech",
                },
                "length_seconds": {
                    "type": "integer",
                    "description": (
                        "Music only. Roughly how long the piece should be, "
                        "3-600 seconds. Ignored for speech, whose length is "
                        "however long the text takes to say."
                    ),
                },
                "instrumental": {
                    "type": "boolean",
                    "description": "Music only. Ask for no vocals.",
                },
            },
            "required": ["prompt"],
        }

    async def execute(self, **kwargs: Any) -> str:
        from flowly.agent.reply_media import media_envelope
        from flowly.voice.generate import (
            MODE_MUSIC,
            VoiceGenerationError,
            generate_elevenlabs,
        )

        prompt = (kwargs.get("prompt") or "").strip()
        if not prompt:
            return "Error: 'prompt' is required."

        mode = (kwargs.get("mode") or "speech").strip()
        if mode not in _MODES:
            return f"Error: 'mode' must be one of {', '.join(_MODES)}."

        settings = self._elevenlabs
        if settings is None or not settings.configured:
            return (
                "Error: no voice provider is configured. Add your ElevenLabs key "
                "under Connections."
            )

        try:
            result = await generate_elevenlabs(
                settings,
                mode=mode,
                prompt=prompt,
                length_seconds=int(kwargs.get("length_seconds") or 0),
                instrumental=bool(kwargs.get("instrumental")),
            )
        except VoiceGenerationError as exc:
            return f"Voice generation failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Voice generation error: {exc}"

        asset = result.assets[0]
        seconds = asset.duration_ms
        length = f" ({round(seconds / 1000)}s)" if seconds else ""
        noun = "music" if mode == MODE_MUSIC else "speech"
        # The saved path belongs in the summary. Without it the model has no
        # idea the file exists on disk and answers "where is it?" by inventing
        # something.
        return media_envelope(
            [asset.path],
            f"Generated {noun}{length} with {result.provider} ({result.model}), "
            f"attached to this reply.\nSaved to: {asset.path}",
            assets=result.assets,
        )
