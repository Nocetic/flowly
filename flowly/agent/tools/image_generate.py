"""``image_generate`` — text-to-image and image edits via the media layer (FAL).

Registered only when ``tools.image_generation`` is enabled and has an API key
(see the agent loop). Produces files under ``<flowly home>/media`` and tells the
agent to attach them with the ``message`` tool — the existing delivery path then
reaches every client, including remote/VPS gateways (via ``/api/media``).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from flowly.agent.tools.base import Tool

#: The most source images one edit takes (FAL's gpt-image edit takes 16).
MAX_SOURCE_IMAGES = 8
#: A source sent as is up to this size; a bigger one is sent downscaled.
_INLINE_SOURCE_BYTES = 4 * 1024 * 1024


def edit_endpoint(model: str, edit_model: str = "") -> str | None:
    """The FAL endpoint that edits images for this setup: the chosen
    image-to-image model, else the edit endpoint of the OpenAI image model
    in use; None when neither is known."""
    if edit_model:
        return edit_model
    if model.startswith("openai/gpt-image") and not model.endswith("/edit"):
        return f"{model}/edit"
    return None


def source_reference(ref: str) -> str:
    """A source image as FAL takes it: an https URL as is, a local image as a data URI.

    Raises ValueError with a sentence the agent can act on.
    """
    ref = ref.strip()
    if ref.startswith("https://") or ref.startswith("data:image/"):
        return ref
    path = Path(ref).expanduser()
    if not path.is_file():
        raise ValueError(f"source image not found: {ref}")
    mime = mimetypes.guess_type(str(path))[0] or ""
    if not mime.startswith("image/"):
        raise ValueError(f"not an image: {ref}")
    if path.stat().st_size > _INLINE_SOURCE_BYTES:
        from flowly.channels.web import _compress_image_for_transport

        compressed = _compress_image_for_transport(
            path, max_dimension=2048, target_bytes=_INLINE_SOURCE_BYTES, initial_quality=90
        )
        if compressed is None:
            raise ValueError(f"source image too large to send: {ref}")
        data, mime = compressed
    else:
        data = path.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class ImageGenerateTool(Tool):
    def __init__(self, *, api_key: str, model: str, edit_model: str = ""):
        self._api_key = api_key
        self._model = model or "fal-ai/flux/dev"
        self._edit_model = edit_model

    @property
    def name(self) -> str:
        return "image_generate"

    @property
    def description(self) -> str:
        return (
            "Generate an image from a text prompt, or edit images: pass them as "
            "source_images (the path or URL an attached image's note gives, or "
            "a file on this machine) and describe the change in the prompt. The "
            "image is attached to your reply automatically — do NOT call the "
            "message tool for it. Just call this, then describe the result. Use "
            "a vivid, detailed prompt, and ALWAYS write the prompt in English "
            "(translate the user's request if they asked in another language) — "
            "image models understand English best."
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
                "source_images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_SOURCE_IMAGES,
                    "description": (
                        "Images to edit or work from: local paths or https URLs. "
                        "With these the edit model keeps what the prompt does not change."
                    ),
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
        num_images = kwargs.get("num_images") or 1
        sources = [s for s in (kwargs.get("source_images") or []) if isinstance(s, str) and s.strip()]
        image_urls: list[str] | None = None
        if sources:
            if len(sources) > MAX_SOURCE_IMAGES:
                return f"Error: at most {MAX_SOURCE_IMAGES} source images."
            edit = edit_endpoint(model, "" if kwargs.get("model") else self._edit_model)
            if edit is None:
                return (f"Error: {model} cannot edit images. Pass an edit model id "
                        "(e.g. openai/gpt-image-2/edit) as model, or ask the owner to "
                        "choose an image-to-image model in Media settings.")
            try:
                image_urls = [source_reference(s) for s in sources]
            except ValueError as exc:
                return f"Error: {exc}"
            except OSError as exc:
                return f"Error: a source image could not be read ({exc})."
            model = edit
            # An edit keeps its source's shape unless asked otherwise.
            image_size = kwargs.get("image_size") or ""
        else:
            image_size = kwargs.get("image_size") or "landscape_4_3"

        try:
            result = await generate_image(
                api_key=self._api_key,
                model=model,
                prompt=prompt,
                image_size=image_size,
                num_images=num_images,
                image_urls=image_urls,
            )
        except FalError as exc:
            return f"Image generation failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Image generation error: {exc}"

        from flowly.agent.reply_media import media_envelope
        from flowly.media.assets import describe

        paths = result["paths"]
        # Descriptors, so this reply carries the same provenance video and voice
        # already do. Nothing in the chat bubble needs them — an image tells a
        # client everything by being openable — but the media library does: a
        # generated picture with no provider, model or prompt is indistinguishable
        # from a photo somebody sent, and unsearchable besides.
        # No ``source_url``: ``generate_image`` records every candidate URL but
        # only appends a path for the ones that downloaded, so the two lists can
        # fall out of step and a wrong attribution is worse than none.
        assets = [
            describe(path, provider="fal", model=model, prompt=prompt)
            for path in paths
        ]
        noun = "image" if len(paths) == 1 else "images"
        # The loop attaches these to the assistant's reply (one bubble) and shows
        # the model this summary — no separate message-tool send needed.
        # Same reason as video_generate: the model must know where the file
        # landed, or it will make something up when asked.
        where = "\n".join(f"Saved to: {p}" for p in paths)
        return media_envelope(
            paths,
            f"Generated {len(paths)} {noun} with {model}, attached to this reply.\n{where}",
            assets=assets,
        )
