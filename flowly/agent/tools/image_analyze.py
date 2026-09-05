"""Standalone image questions through the user's configured live provider."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from flowly.agent.tools.base import Tool

_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


class ImageAnalyzeTool(Tool):
    name = "image_analyze"
    description = (
        "Analyze an image or answer a question about it using the configured model. "
        "Supply a public image URL, base64 image data URL, or an image file in the "
        "Flowly workspace/media directory. Maximum local image size: 8 MiB."
    )

    def __init__(
        self, *, provider_getter: Callable[[], Any], model_getter: Callable[[], str],
        workspace: Path,
    ):
        self._provider_getter = provider_getter
        self._model_getter = model_getter
        self._workspace = Path(workspace).resolve()

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "minLength": 1, "description": "Image URL, data URL or local path"},
                "question": {"type": "string", "minLength": 1, "maxLength": 32000},
            },
            "required": ["image_url", "question"], "additionalProperties": False,
        }

    @property
    def output_schema(self) -> dict:
        return {
            "type": "object", "properties": {"analysis": {"type": "string"}},
            "required": ["analysis"], "additionalProperties": False,
        }

    def _source(self, source: str) -> str:
        from PIL import Image

        from flowly.agent.tools.video_analyze import _check_url_safe
        from flowly.profile import get_flowly_home

        parsed = urlsplit(source)
        if parsed.scheme in {"http", "https"}:
            if parsed.username or parsed.password or _check_url_safe(source):
                raise ValueError("Image URL must be a public HTTP(S) address without user credentials")
            return source
        if source.startswith("data:"):
            if len(source) > (_MAX_IMAGE_BYTES * 4 // 3) + 256:
                raise ValueError("Image exceeds the size limit")
            header, encoded = source.split(",", 1)
            if header not in {f"data:{mime};base64" for mime in _MIMES.values()}:
                raise ValueError("Unsupported image data URL")
            raw = base64.b64decode(encoded, validate=True)
        else:
            from flowly.agent.media_files import read_media_file

            path = Path(source.removeprefix("file://"))
            roots = (self._workspace, (get_flowly_home() / "media").resolve())
            raw = read_media_file(path, roots, _MAX_IMAGE_BYTES)
        if len(raw) > _MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the size limit")
        try:
            with Image.open(io.BytesIO(raw)) as opened:
                mime = _MIMES.get(opened.format)
                if not mime or opened.width * opened.height > 25_000_000:
                    raise ValueError("Unsupported image format or dimensions")
                opened.verify()
        except Image.DecompressionBombError:
            raise ValueError("Image dimensions exceed the size limit") from None
        return f"data:{mime};base64," + base64.b64encode(raw).decode()

    async def execute(self, image_url: str, question: str) -> str:
        if not isinstance(image_url, str) or not isinstance(question, str) or not question.strip() or len(question) > 32000:
            return "Error: image_url and question are required"
        try:
            source = await asyncio.to_thread(self._source, image_url)
        except (ValueError, OSError):
            return "Error: Image must be a supported public URL or an allowed image file/data URL within the size limit"
        model = self._model_getter()
        if not model:
            return "Error: No image-analysis model is configured"
        try:
            response = await self._provider_getter().chat(
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": source}},
                ]}], tools=None, model=model, max_tokens=4000, temperature=0.1,
            )
            if not response.content:
                return "Error: The configured model returned no image analysis"
            return json.dumps({"analysis": response.content}, ensure_ascii=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            return "Error: Image analysis failed; check the configured provider and model's image support"
