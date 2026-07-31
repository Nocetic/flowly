"""Resolving media-generation settings, old shape or new.

``tools.imageGeneration`` shipped before video existed. Rather than migrate the
file on disk — which would rewrite a config the user may be editing, and would
break a rollback to an older build — the two shapes are merged at read time.
The new block wins field by field; anything it leaves empty falls back to the
old one, so an upgrade keeps the existing key and the existing image model with
no action from the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Only used when neither config block names one. Image generation has always
# worked without the user choosing a model, and that should stay true.
DEFAULT_IMAGE_MODEL = "fal-ai/flux/dev"


@dataclass(frozen=True, slots=True)
class MediaSettings:
    """What the tools need, after both config shapes have been considered."""

    enabled: bool
    provider: str
    api_key: str
    text_to_image: str
    image_to_image: str
    text_to_video: str
    image_to_video: str
    video_timeout_seconds: int

    @property
    def image_ready(self) -> bool:
        return bool(self.enabled and self.api_key)

    @property
    def video_ready(self) -> bool:
        """Video needs a configured model too — there is no safe default.

        Picking one for the user would mean spending their money on a model
        they never chose, at a price they never saw.
        """
        return bool(self.enabled and self.api_key and (self.text_to_video or self.image_to_video))


def resolve_media_settings(tools_config: Any) -> MediaSettings:
    """Merge ``tools.media_generation`` over legacy ``tools.image_generation``."""
    media = getattr(tools_config, "media_generation", None)
    legacy = getattr(tools_config, "image_generation", None)
    defaults = getattr(media, "defaults", None)

    def _media(name: str, fallback: Any = "") -> Any:
        value = getattr(media, name, None) if media is not None else None
        return value if value not in (None, "") else fallback

    def _default(name: str, fallback: str = "") -> str:
        value = getattr(defaults, name, "") if defaults is not None else ""
        return value or fallback

    legacy_enabled = bool(getattr(legacy, "enabled", False))
    legacy_key = str(getattr(legacy, "api_key", "") or "")
    legacy_model = str(getattr(legacy, "model", "") or "")

    return MediaSettings(
        # Either block turning it on is enough: an upgrade must not silently
        # disable the image generation someone already had working.
        enabled=bool(getattr(media, "enabled", False)) or legacy_enabled,
        provider=str(_media("provider", getattr(legacy, "provider", "fal") or "fal")),
        api_key=str(_media("api_key", legacy_key)),
        # Explicit new choice, else the model the older config already had,
        # else a known-good image model so image generation works out of the box.
        text_to_image=_default("text_to_image", legacy_model or DEFAULT_IMAGE_MODEL),
        image_to_image=_default("image_to_image"),
        text_to_video=_default("text_to_video"),
        image_to_video=_default("image_to_video"),
        video_timeout_seconds=int(_media("video_timeout_seconds", 900) or 900),
    )
