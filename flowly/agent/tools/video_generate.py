"""``video_generate`` — text-to-video and image-to-video through the media layer.

The tool's schema is deliberately provider-neutral: prompt, mode, duration,
aspect ratio, resolution. None of fal's field names appear here, because the
model the user picks decides those and the adapter translates. That is the
difference from ``image_generate``, whose ``image_size`` vocabulary predates
this layer and is kept as-is so existing calls don't break.

Generation runs to completion inside the turn. A clip takes minutes, which is
long for a tool call — but it means the video rides the assistant's own reply
through the delivery path every surface already has, instead of needing a way
to post into a conversation after the turn has ended.
"""

from __future__ import annotations

from typing import Any

from flowly.agent.tools.base import Tool

# What the user can ask for, in plain units. The adapter snaps these onto
# whatever the chosen model actually accepts.
_MODES = ("text_to_video", "image_to_video")
_CATEGORY_FOR_MODE = {
    "text_to_video": "text-to-video",
    "image_to_video": "image-to-video",
}


class VideoGenerateTool(Tool):
    def __init__(
        self,
        *,
        api_key: str,
        text_to_video_model: str = "",
        image_to_video_model: str = "",
        catalog: Any = None,
        timeout_seconds: float | None = None,
    ):
        self._api_key = api_key
        self._defaults = {
            "text_to_video": text_to_video_model,
            "image_to_video": image_to_video_model,
        }
        self._catalog = catalog
        self._timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "video_generate"

    @property
    def description(self) -> str:
        return (
            "Generate a short video from a text prompt, or animate an image you "
            "already have. The clip is attached to your reply automatically — do "
            "NOT call the message tool for it. Just call this, then describe the "
            "result. Generation takes a few minutes; call it once and wait. "
            "ALWAYS write the prompt in English (translate the user's request if "
            "they asked in another language) — video models understand English best."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "What should happen in the clip — subject, action, camera "
                        "movement, lighting, mood. ALWAYS in English."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": list(_MODES),
                    "description": (
                        "'text_to_video' generates from the prompt alone. "
                        "'image_to_video' animates 'input_image'."
                    ),
                    "default": "text_to_video",
                },
                "input_image": {
                    "type": "string",
                    "description": (
                        "Public https URL of the starting frame. Required for "
                        "image_to_video."
                    ),
                },
                "duration_seconds": {
                    "type": "integer",
                    "description": (
                        "Roughly how long the clip should be. Snapped to the "
                        "nearest length the chosen model supports."
                    ),
                    "default": 5,
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "e.g. '16:9', '9:16', '1:1'. Use 9:16 for phone-first video.",
                },
                "resolution": {
                    "type": "string",
                    "description": "e.g. '720p' or '1080p', when the model offers a choice.",
                },
                "generate_audio": {
                    "type": "boolean",
                    "description": (
                        "Ask for a soundtrack. Only some models can; the call "
                        "fails rather than silently returning a mute clip."
                    ),
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "What to keep out of the clip.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional endpoint id to override the configured default.",
                },
            },
            "required": ["prompt"],
        }

    def _default_model(self, mode: str) -> str:
        return (self._defaults.get(mode) or "").strip()

    async def execute(self, **kwargs: Any) -> str:
        from flowly.agent.reply_media import media_envelope
        from flowly.media.catalog import ModelCatalog
        from flowly.media.generate import GenerationError, generate_video, resolve_model

        prompt = (kwargs.get("prompt") or "").strip()
        if not prompt:
            return "Error: 'prompt' is required."

        mode = (kwargs.get("mode") or "text_to_video").strip()
        if mode not in _MODES:
            return f"Error: 'mode' must be one of {', '.join(_MODES)}."

        input_image = (kwargs.get("input_image") or "").strip()
        if mode == "image_to_video" and not input_image:
            return "Error: 'input_image' is required for image_to_video."
        if input_image and not input_image.startswith(("http://", "https://")):
            return "Error: 'input_image' must be a public https URL."

        endpoint_id = (kwargs.get("model") or "").strip() or self._default_model(mode)
        if not endpoint_id:
            return (
                f"Error: no {mode.replace('_', '-')} model is configured. "
                "Set one in media generation settings, or pass 'model'."
            )

        catalog = self._catalog or ModelCatalog(api_key=self._api_key)
        request: dict[str, Any] = {
            "prompt": prompt,
            "duration_seconds": kwargs.get("duration_seconds") or 5,
        }
        # Only fields the caller actually set count as explicit — that is what
        # turns "this model has no audio switch" from a silent drop into a
        # refusal, without doing the same for defaults nobody asked for.
        explicit: set[str] = set()
        for key in ("aspect_ratio", "resolution", "negative_prompt"):
            value = kwargs.get(key)
            if value:
                request[key] = value
                explicit.add(key)
        if kwargs.get("generate_audio") is not None:
            request["generate_audio"] = bool(kwargs["generate_audio"])
            explicit.add("generate_audio")
        if input_image:
            request["input_image"] = input_image
            explicit.add("input_image")
        if kwargs.get("duration_seconds"):
            explicit.add("duration_seconds")

        try:
            model = await resolve_model(
                catalog, endpoint_id, category=_CATEGORY_FOR_MODE[mode]
            )
            result = await generate_video(
                api_key=self._api_key,
                model=model,
                request=request,
                explicit=frozenset(explicit),
                **({"timeout_seconds": self._timeout_seconds} if self._timeout_seconds else {}),
            )
        except GenerationError as exc:
            return f"Video generation failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Video generation error: {exc}"

        assets = result.assets
        noun = "video" if len(assets) == 1 else "videos"
        seconds = next((a.duration_ms for a in assets if a.duration_ms), None)
        length = f" ({round(seconds / 1000)}s)" if seconds else ""
        return media_envelope(
            [a.path for a in assets],
            f"Generated {len(assets)} {noun}{length} with {result.model}, attached to this reply.",
            assets=assets,
        )
