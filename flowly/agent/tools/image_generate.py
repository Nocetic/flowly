"""``image_generate`` — text-to-image via the media layer (FAL).

Registered only when ``tools.image_generation`` is enabled and has an API key
(see the agent loop). Produces files under ``<flowly home>/media`` and tells the
agent to attach them with the ``message`` tool — the existing delivery path then
reaches every client, including remote/VPS gateways (via ``/api/media``).
"""

from __future__ import annotations

from typing import Any

from flowly.agent.tools.base import Tool


class ImageGenerateTool(Tool):
    def __init__(self, *, api_key: str, model: str):
        self._api_key = api_key
        self._model = model or "fal-ai/flux/dev"

    @property
    def name(self) -> str:
        return "image_generate"

    @property
    def description(self) -> str:
        return (
            "Generate an image from a text prompt. The image is attached to your "
            "reply automatically — do NOT call the message tool for it. Just call "
            "this, then describe the result. Use a vivid, detailed prompt, and "
            "ALWAYS write the prompt in English (translate the user's request if "
            "they asked in another language) — image models understand English best."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "Detailed description of the image to generate. ALWAYS in "
                        "English — translate if the user wrote in another language; "
                        "many image models don't understand non-English prompts."
                    ),
                },
                "image_size": {
                    "type": "string",
                    "description": (
                        "Aspect/size: square_hd, square, portrait_4_3, portrait_16_9, "
                        "landscape_4_3, landscape_16_9."
                    ),
                    "default": "landscape_4_3",
                },
                "num_images": {
                    "type": "integer",
                    "description": "How many images to generate (1-4).",
                    "default": 1,
                },
                "model": {
                    "type": "string",
                    "description": "Optional FAL model id to override the configured default.",
                },
            },
            "required": ["prompt"],
        }

    async def execute(self, **kwargs: Any) -> str:
        from flowly.media.fal import FalError, generate_image

        prompt = (kwargs.get("prompt") or "").strip()
        if not prompt:
            return "Error: 'prompt' is required."
        model = (kwargs.get("model") or self._model).strip() or self._model
        image_size = kwargs.get("image_size") or "landscape_4_3"
        num_images = kwargs.get("num_images") or 1

        try:
            result = await generate_image(
                api_key=self._api_key,
                model=model,
                prompt=prompt,
                image_size=image_size,
                num_images=num_images,
            )
        except FalError as exc:
            return f"Image generation failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Image generation error: {exc}"

        from flowly.agent.reply_media import media_envelope

        paths = result["paths"]
        noun = "image" if len(paths) == 1 else "images"
        # The loop attaches these to the assistant's reply (one bubble) and shows
        # the model this summary — no separate message-tool send needed.
        return media_envelope(
            paths,
            f"Generated {len(paths)} {noun} with {model}, attached to this reply.",
        )
