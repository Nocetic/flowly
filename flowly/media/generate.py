"""Generating media: model → payload → queue → local file → asset.

This is the one place that knows the whole sequence, so the tools stay thin and
the pieces (catalog, adapter, queue, probe) stay independent.

Downloading the provider's output is the security-sensitive step. The URL comes
back from a remote service, and fetching it happens on the user's own machine
with their network — so it is treated as untrusted input: HTTPS only, redirects
off, a hard size ceiling enforced while streaming rather than after, and the
declared content type checked against what we asked for. A file is only moved
into the media directory once it has passed all of that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from flowly.media.adapters import AdapterError, build_payload, extract_media_urls
from flowly.media.assets import MediaAsset, describe
from flowly.media.catalog import COMPAT_UNSUPPORTED, MediaModel, ModelCatalog

# Generous, because a 1080p clip is genuinely large — but finite, so a runaway
# or hostile response cannot fill the disk.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, connect=15.0)
_UA = "flowly/media-download"

# How long a single generation may take before we stop waiting and cancel.
DEFAULT_VIDEO_TIMEOUT_SECONDS = 15 * 60


class GenerationError(RuntimeError):
    """Generation could not be completed. The message is user-facing."""


@dataclass(frozen=True, slots=True)
class GenerationResult:
    assets: list[MediaAsset]
    model: str
    request_id: str


def media_dir() -> Path:
    from flowly.profile import get_flowly_home

    directory = get_flowly_home() / "media"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _extension_for(mime: str, fallback: str) -> str:
    known = {
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    return known.get(mime.split(";")[0].strip().lower(), fallback)


async def download_output(url: str, *, kind: str, dest_dir: Path | None = None) -> Path:
    """Stream a provider output URL to a file in the media directory.

    Raises :class:`GenerationError` with a user-facing message on anything
    suspicious. The file is written under a temporary name and only renamed
    into place once complete, so a partial download is never mistaken for a
    finished clip.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        raise GenerationError("the model returned a result that could not be downloaded safely.")

    directory = dest_dir or media_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{'vid' if kind == 'video' else 'img'}-{uuid.uuid4().hex[:12]}"
    tmp = directory / f".{stem}.part"

    expected_prefix = "video/" if kind == "video" else "image/"
    written = 0
    try:
        async with httpx.AsyncClient(
            timeout=_DOWNLOAD_TIMEOUT,
            # A redirect would let the provider's answer point somewhere we
            # never validated; make it declare its final URL up front.
            follow_redirects=False,
        ) as client:
            async with client.stream("GET", url, headers={"User-Agent": _UA}) as response:
                if response.status_code >= 400:
                    raise GenerationError(
                        f"the generated file could not be downloaded (HTTP {response.status_code})."
                    )
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type and not content_type.startswith(expected_prefix):
                    raise GenerationError(
                        f"the model returned {content_type.split(';')[0]} where {kind} was expected."
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                    raise GenerationError("the generated file is too large to store.")

                extension = _extension_for(content_type, ".mp4" if kind == "video" else ".png")
                with tmp.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        # Enforced DURING the stream: a missing or lying
                        # content-length must not become an unbounded write.
                        if written > MAX_DOWNLOAD_BYTES:
                            raise GenerationError("the generated file is too large to store.")
                        handle.write(chunk)
    except GenerationError:
        tmp.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise GenerationError(f"the generated file could not be downloaded: {exc}") from exc

    if written == 0:
        tmp.unlink(missing_ok=True)
        raise GenerationError("the model returned an empty file.")

    final = directory / f"{stem}{extension}"
    tmp.replace(final)
    return final


def _attach_poster(video_path: Path) -> str | None:
    """Best-effort poster next to the clip. None when ffmpeg isn't available."""
    from flowly.media.probe import extract_poster

    poster = video_path.with_suffix(".jpg")
    if extract_poster(video_path, poster):
        return str(poster)
    return None


async def resolve_model(
    catalog: ModelCatalog, endpoint_id: str, *, category: str
) -> MediaModel:
    """Fetch a model with its schema and refuse it if Flowly can't drive it."""
    from flowly.media.fal_catalog import CatalogError, CatalogRateLimitedError

    try:
        model = await catalog.get(endpoint_id, with_schema=True)
    except CatalogRateLimitedError as exc:
        raise GenerationError(f"{exc} Try again in a moment.") from exc
    except CatalogError as exc:
        raise GenerationError(f"couldn't reach the model catalog: {exc}") from exc
    if model is None:
        raise GenerationError(
            f"'{endpoint_id}' is not in the model catalog. Pick a different model."
        )
    if model.compatibility == COMPAT_UNSUPPORTED:
        raise GenerationError(
            f"'{endpoint_id}' needs inputs Flowly can't provide. Pick a different model."
        )
    if model.category and model.category != category:
        raise GenerationError(
            f"'{endpoint_id}' is a {model.category} model, not {category}."
        )
    return model


async def generate_video(
    *,
    api_key: str,
    model: MediaModel,
    request: dict[str, Any],
    explicit: frozenset[str] = frozenset(),
    timeout_seconds: float = DEFAULT_VIDEO_TIMEOUT_SECONDS,
    on_state: Any = None,
    should_cancel: Any = None,
) -> GenerationResult:
    """Run one video generation end to end and return local assets."""
    from flowly.media.fal_queue import (
        FalQueueCancelledError,
        FalQueueError,
        submit,
        wait_for_result,
    )

    if not (api_key or "").strip():
        raise GenerationError("the fal API key is missing — add it in setup.")
    if model.input_schema is None:
        # The model exists — resolve_model already confirmed that — so this is
        # us failing to read its interface, not the user picking a bad id.
        raise GenerationError(
            f"couldn't read what inputs '{model.endpoint_id}' takes, so it can't be "
            "driven safely. Try again, or pick a different model."
        )

    try:
        payload, dropped = build_payload(model.input_schema, request, explicit=explicit)
    except AdapterError as exc:
        raise GenerationError(str(exc)) from exc
    if dropped:
        logger.info(
            "[media] {} ignores: {}", model.endpoint_id, ", ".join(sorted(dropped))
        )

    try:
        job = await submit(api_key=api_key, endpoint_id=model.endpoint_id, payload=payload)
        result = await wait_for_result(
            job,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            on_state=on_state,
            should_cancel=should_cancel,
        )
    except FalQueueCancelledError as exc:
        raise GenerationError(str(exc)) from exc
    except FalQueueError as exc:
        raise GenerationError(str(exc)) from exc

    urls = extract_media_urls(result, kind="video")
    if not urls:
        raise GenerationError("the model finished but returned no video.")

    assets: list[MediaAsset] = []
    for url in urls:
        path = await download_output(url, kind="video")
        poster = _attach_poster(path)
        assets.append(
            describe(
                path,
                provider="fal",
                model=model.endpoint_id,
                request_id=job.request_id,
                source_url=url,
                poster_path=poster,
            )
        )

    if not assets:
        raise GenerationError("the generated video could not be saved.")
    return GenerationResult(assets=assets, model=model.endpoint_id, request_id=job.request_id)
