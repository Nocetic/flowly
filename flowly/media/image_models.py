"""Curated catalog of FAL text-to-image models.

FAL has no clean "list all image models" API (unlike models.dev for LLMs), so we
maintain our own short list of popular, known-good models. The user picks the
active one in setup; the agent may override per call. ``edit_endpoint`` is the
image-to-image route used by a future ``image_edit`` tool (empty = none yet).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImageModel:
    id: str
    label: str
    description: str = ""
    edit_endpoint: str = ""


# Our own selection — by capability, not copied from any other catalog.
FAL_IMAGE_MODELS: list[ImageModel] = [
    ImageModel("fal-ai/flux/schnell", "FLUX.1 [schnell]", "fastest & cheapest"),
    ImageModel(
        "fal-ai/flux/dev", "FLUX.1 [dev]", "balanced quality/speed",
        edit_endpoint="fal-ai/flux/dev/image-to-image",
    ),
    ImageModel("fal-ai/flux-pro/v1.1", "FLUX1.1 [pro]", "highest quality"),
    ImageModel("fal-ai/stable-diffusion-v35-large", "Stable Diffusion 3.5 Large", "open, versatile"),
    ImageModel("fal-ai/recraft-v3", "Recraft V3", "strong typography & logos"),
    ImageModel("fal-ai/ideogram/v2", "Ideogram v2", "best in-image text"),
]

DEFAULT_IMAGE_MODEL = "fal-ai/flux/dev"


def get_image_model(model_id: str) -> ImageModel | None:
    return next((m for m in FAL_IMAGE_MODELS if m.id == model_id), None)


def model_choices() -> list[tuple[str, str]]:
    """(id, "Label · description") pairs for a SELECT field / picker."""
    return [
        (m.id, f"{m.label}  ·  {m.description}" if m.description else m.label)
        for m in FAL_IMAGE_MODELS
    ]
