"""Video understanding tool — sends videos to Gemini via the
OpenRouter ``video_url`` content block.

Behaviour:
- Accepts an HTTP/HTTPS URL OR a local file path. Local files are
  base64-encoded inline; URLs are forwarded as-is and downloaded by
  the model provider.
- Hard cap 50 MB / warn at 20 MB on the encoded payload (matches
  the renderer/native-picker limit on the desktop composer).
- Default model ``google/gemini-flash-latest`` — the only OpenRouter
  route with native video understanding currently in the plan
  allowlist (``flowly-app/lib/plans/allowlist.ts``).
- Errors are categorised into actionable hints (insufficient credits,
  model rejected video, payload too large, generic).

The agent reaches this tool when a user attaches a video. The
context layer (``flowly/agent/context.py``) tags the file path in
the user message so the LLM knows to call ``video_analyze`` instead
of trying to feed the bytes to the main model — Claude/GPT/etc.
on the proxy allowlist do not natively understand video.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from flowly.agent.tools.base import Tool

_VIDEO_MIME_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}

_MAX_VIDEO_BYTES = 50 * 1024 * 1024
_MAX_LOCAL_VIDEO_DATA_URL_BYTES = 20 * 1024 * 1024
_WARN_VIDEO_BYTES = 20 * 1024 * 1024
_DEFAULT_MODEL = "google/gemini-3.1-flash-lite"


def _is_private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def _check_url_safe(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception as e:
        return f"Invalid URL: {e}"
    if parsed.scheme not in ("http", "https"):
        return f"Unsupported URL scheme '{parsed.scheme}' (only http/https)"
    if not parsed.hostname:
        return "URL has no hostname"
    if _is_private_host(parsed.hostname):
        return f"Blocked: '{parsed.hostname}' resolves to a private/internal address"
    return None


def _detect_mime(path: Path) -> str | None:
    return _VIDEO_MIME_TYPES.get(path.suffix.lower())


def _to_data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _estimated_data_url_bytes(raw_bytes: int, mime: str) -> int:
    # Base64 expands by 4/3, rounded to complete 4-byte groups.
    return len(f"data:{mime};base64,") + ((raw_bytes + 2) // 3) * 4


def _categorize_error(err: Exception) -> str:
    s = str(err).lower()
    if any(h in s for h in ("402", "insufficient", "payment required", "credits", "billing")):
        return (
            "Insufficient credits or payment required. Top up the account "
            f"and try again. Error: {err}"
        )
    if any(
        h in s
        for h in (
            "does not support",
            "not support video",
            "content_policy",
            "multimodal",
            "video input",
            "video_url",
            "model_not_allowed",
        )
    ):
        return (
            "The model does not support video analysis or rejected the request. "
            f"Use a video-capable model (e.g. {_DEFAULT_MODEL}). Error: {err}"
        )
    if any(
        h in s
        for h in (
            "too large",
            "payload",
            "413",
            "content_too_large",
            "request_too_large",
            "size limit",
            "context_too_large",
        )
    ):
        return (
            "Video too large for the API. Compress or trim it (max ~50 MB). "
            f"Error: {err}"
        )
    return f"Could not analyze video. Error: {err}"


class VideoAnalyzeTool(Tool):
    """Analyze a video via a multimodal LLM (Gemini)."""

    def __init__(self, provider: Any, default_model: str | None = None):
        self.provider = provider
        self.default_model = (
            default_model
            or os.getenv("AUXILIARY_VIDEO_MODEL", "").strip()
            or _DEFAULT_MODEL
        )

    @property
    def name(self) -> str:
        return "video_analyze"

    @property
    def description(self) -> str:
        return (
            "Analyze a video using a multimodal AI model (Gemini). Pass either "
            "a public HTTP/HTTPS URL or a local file path. When the user "
            "attaches a video, the conversation will surface a path under "
            "``~/.flowly/media/`` — pass that exact path as ``video_url``. "
            "Supports mp4, webm, mov, avi, mkv, mpeg formats. Max ~50 MB. "
            "Returns a JSON string with ``success`` and ``analysis`` fields."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "video_url": {
                    "type": "string",
                    "description": (
                        "Video source: a public HTTP(S) URL, or a local file "
                        "path (e.g. the attachment path printed in the user "
                        "message)."
                    ),
                },
                "question": {
                    "type": "string",
                    "description": (
                        "Specific question about the video. The model will "
                        "describe the video and answer the question."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        f"Optional model override. Defaults to {_DEFAULT_MODEL}."
                    ),
                },
            },
            "required": ["video_url", "question"],
        }

    async def execute(
        self,
        video_url: str,
        question: str,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not isinstance(video_url, str) or not video_url:
            return _fail("video_url is required")
        if not isinstance(question, str):
            question = str(question) if question is not None else ""

        chosen_model = model or self.default_model

        try:
            resolved = (
                video_url[len("file://"):]
                if video_url.startswith("file://")
                else video_url
            )
            local = Path(os.path.expanduser(resolved))

            if local.is_file():
                # Local path — base64-encode inline. The proxy's input-token
                # estimator already skips non-text blocks (see
                # flowly-app/.../completions/route.ts), so the cap won't
                # falsely trip on the data URL.
                mime = _detect_mime(local)
                if not mime:
                    return _fail(
                        f"Unsupported video format: '{local.suffix}'. "
                        f"Supported: {', '.join(sorted(_VIDEO_MIME_TYPES))}"
                    )
                size = local.stat().st_size
                if size > _MAX_VIDEO_BYTES:
                    return _fail(
                        f"Video too large: {size / (1024 * 1024):.1f} MB "
                        f"(max {_MAX_VIDEO_BYTES // (1024 * 1024)} MB). "
                        "Compress or trim the video and retry."
                    )
                estimated_payload = _estimated_data_url_bytes(size, mime)
                if estimated_payload > _MAX_LOCAL_VIDEO_DATA_URL_BYTES:
                    return _fail(
                        "Local video is too large to inline safely: "
                        f"{estimated_payload / (1024 * 1024):.1f} MB after base64 "
                        f"(max {_MAX_LOCAL_VIDEO_DATA_URL_BYTES // (1024 * 1024)} MB). "
                        "Sign in and attach the video from the TUI so Flowly can "
                        "upload it securely, or pass a public HTTP(S) video URL."
                    )
                if size > _WARN_VIDEO_BYTES:
                    logger.warning(
                        "[video_analyze] %.1f MB — may be slow or rejected",
                        size / (1024 * 1024),
                    )
                payload_url = _to_data_url(local, mime)
            else:
                # Remote URL — forward as-is, model provider downloads it.
                err = _check_url_safe(video_url)
                if err:
                    return _fail(err)
                payload_url = video_url

            wrapped_prompt = (
                "Fully describe and explain everything happening in this video, "
                "including visual content, motion, audio cues, text overlays, "
                "and scene transitions. Then answer the following question:\n\n"
                + question
            )

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": wrapped_prompt},
                        {"type": "video_url", "video_url": {"url": payload_url}},
                    ],
                }
            ]

            chat_kwargs: dict[str, Any] = {
                "messages": messages,
                "tools": None,
                "max_tokens": 4000,
                "temperature": 0.1,
                "model": chosen_model,
            }

            response = await self.provider.chat(**chat_kwargs)
            analysis = (response.content or "").strip()
            if not analysis:
                # Empty response → one retry before giving up.
                response = await self.provider.chat(**chat_kwargs)
                analysis = (response.content or "").strip()

            return json.dumps(
                {
                    "success": True,
                    "analysis": analysis
                    or "There was a problem with the request and the video could not be analyzed.",
                },
                ensure_ascii=False,
            )

        except Exception as e:  # noqa: BLE001
            logger.opt(exception=True).error("[video_analyze] {}", e)
            return json.dumps(
                {
                    "success": False,
                    "error": f"Error analyzing video: {e}",
                    "analysis": _categorize_error(e),
                },
                ensure_ascii=False,
            )


def _fail(message: str) -> str:
    return json.dumps(
        {"success": False, "error": message, "analysis": message},
        ensure_ascii=False,
    )
