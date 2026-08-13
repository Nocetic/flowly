"""ElevenLabs, spoken to directly.

Everything offered here belongs to the key the user typed on the ElevenLabs
connection: their voices, the models their plan can run, their quota. Nothing
is routed through anybody else's index, which is why a voice cloned this
morning is in the picker this afternoon.

The module knows nothing about how the result is delivered. It returns bytes;
saving, probing, retention and playback are the media layer's job and are
identical whichever provider produced the sound.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://api.elevenlabs.io"

#: Same ceiling as any other generated file. Speech is small; a ten-minute
#: composition is not, and a provider answering with something enormous should
#: be refused before it reaches the disk rather than after.
MAX_RESPONSE_BYTES = 512 * 1024 * 1024

#: mp3 at 44.1kHz/128kbps — what every browser, phone and chat client plays
#: without being asked twice.
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"

#: Music runs long and speech does not, so they get very different budgets.
_SPEECH_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
_MUSIC_TIMEOUT = httpx.Timeout(600.0, connect=15.0)
_LIST_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

#: The provider's own duration limits, in milliseconds.
MIN_MUSIC_MS = 3_000
MAX_MUSIC_MS = 600_000

__all__ = [
    "BASE_URL",
    "DEFAULT_OUTPUT_FORMAT",
    "ElevenLabsAuthError",
    "ElevenLabsError",
    "Option",
    "compose_music",
    "list_music_models",
    "list_speech_models",
    "list_voices",
    "synthesize_speech",
]


class ElevenLabsError(Exception):
    """A call to ElevenLabs failed, with a message meant for the user."""


class ElevenLabsAuthError(ElevenLabsError):
    """The key was rejected.

    Separate from the rest because the fix is different: no amount of retrying
    helps, and the user has to be told to replace it.
    """


@dataclass(frozen=True, slots=True)
class Option:
    """One pickable thing — a voice, or a model."""

    value: str
    label: str
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value, "label": self.label, "description": self.description}


def _headers(api_key: str) -> dict[str, str]:
    return {"xi-api-key": api_key, "User-Agent": "flowly/voice"}


def _raise_for_status(response: httpx.Response, what: str) -> None:
    if response.status_code in (401, 403):
        raise ElevenLabsAuthError(
            "ElevenLabs rejected the API key. Replace it on the ElevenLabs connection."
        )
    if response.status_code == 422:
        raise ElevenLabsError(f"ElevenLabs refused the {what} request as invalid.")
    if response.status_code == 429:
        raise ElevenLabsError("ElevenLabs is rate-limiting this account; try again shortly.")
    if response.status_code >= 400:
        raise ElevenLabsError(f"ElevenLabs returned HTTP {response.status_code} for {what}.")


async def _get_json(path: str, api_key: str, params: dict[str, Any] | None = None) -> Any:
    if not api_key:
        raise ElevenLabsAuthError("no ElevenLabs API key is configured.")
    try:
        async with httpx.AsyncClient(timeout=_LIST_TIMEOUT) as client:
            response = await client.get(
                f"{BASE_URL}{path}", headers=_headers(api_key), params=params or {}
            )
    except httpx.HTTPError as exc:
        raise ElevenLabsError(f"could not reach ElevenLabs: {exc}") from exc
    _raise_for_status(response, path.strip("/"))
    try:
        return response.json()
    except ValueError as exc:
        raise ElevenLabsError("ElevenLabs returned something that was not JSON.") from exc


async def _post_audio(
    path: str,
    api_key: str,
    body: dict[str, Any],
    *,
    timeout: httpx.Timeout,
    what: str,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> bytes:
    """POST a generation request and return the audio bytes.

    Streamed and counted rather than read whole: the size cap has to hold even
    when the provider declares no length, which it does not for audio it is
    generating as it sends.
    """
    if not api_key:
        raise ElevenLabsAuthError("no ElevenLabs API key is configured.")

    chunks: list[bytes] = []
    written = 0
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{BASE_URL}{path}",
                headers={**_headers(api_key), "Content-Type": "application/json"},
                params={"output_format": output_format},
                json=body,
            ) as response:
                if response.status_code >= 400:
                    # The body carries the reason, and it has to be read before
                    # the stream is abandoned or the message is lost.
                    await response.aread()
                    _raise_for_status(response, what)
                async for chunk in response.aiter_bytes():
                    written += len(chunk)
                    if written > MAX_RESPONSE_BYTES:
                        raise ElevenLabsError("the generated audio is too large to store.")
                    chunks.append(chunk)
    except ElevenLabsError:
        raise
    except httpx.HTTPError as exc:
        raise ElevenLabsError(f"could not reach ElevenLabs: {exc}") from exc

    if not written:
        raise ElevenLabsError("ElevenLabs returned an empty file.")
    return b"".join(chunks)


# ── the account's own library, as the picker sees it ────────────────────────


async def list_voices(api_key: str, query: str = "", limit: int = 100) -> list[Option]:
    """The account's own voices.

    ``/v2/voices`` rather than the v1 list because it is the one that paginates
    and searches; a library of two hundred cloned voices is not unusual, and
    the older endpoint made the picker useless at that size.
    """
    params: dict[str, Any] = {"page_size": max(1, min(limit, 100))}
    if query.strip():
        params["search"] = query.strip()
    payload = await _get_json("/v2/voices", api_key, params)

    rows = payload.get("voices") if isinstance(payload, dict) else None
    out: list[Option] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        voice_id = str(row.get("voice_id") or "")
        if not voice_id:
            continue
        # What somebody needs in order to tell two voices apart: what kind it
        # is, and whatever they labelled it with when they made it.
        bits = [str(row.get("category") or "").replace("_", " ").strip()]
        labels = row.get("labels")
        if isinstance(labels, dict):
            bits += [str(v) for v in labels.values() if isinstance(v, str) and v]
        out.append(
            Option(
                voice_id,
                str(row.get("name") or voice_id),
                " · ".join(b for b in bits if b),
            )
        )
    return out


async def _models(api_key: str) -> list[dict[str, Any]]:
    payload = await _get_json("/v1/models", api_key)
    if not isinstance(payload, list):
        raise ElevenLabsError("ElevenLabs returned an unexpected model list.")
    return [row for row in payload if isinstance(row, dict)]


async def list_speech_models(api_key: str, query: str = "") -> list[Option]:
    """Models that can actually speak.

    The account's model list also holds voice-conversion and other things that
    cannot read text. Offering those would produce a failure at generation time
    for a choice that looked perfectly valid when it was made.
    """
    needle = query.strip().lower()
    out: list[Option] = []
    for row in await _models(api_key):
        if not row.get("can_do_text_to_speech"):
            continue
        model_id = str(row.get("model_id") or "")
        if not model_id:
            continue
        name = str(row.get("name") or model_id)
        description = str(row.get("description") or "")
        if needle and needle not in f"{model_id} {name} {description}".lower():
            continue
        languages = row.get("languages")
        if not description and isinstance(languages, list) and languages:
            description = f"{len(languages)} languages"
        out.append(Option(model_id, name, description))
    return out


#: The music endpoint takes a model rather than exposing one per sound, and the
#: two it accepts are stated in the API contract rather than listed by any
#: endpoint. A hardcoded list is normally how a picker goes stale; this is a
#: closed set the provider publishes, and an id it does not know is refused by
#: them rather than silently mis-generated.
_MUSIC_MODELS = (
    Option("music_v2", "Music v2", "Latest composition model"),
    Option("music_v1", "Music v1", "Previous composition model"),
)


async def list_music_models(api_key: str, query: str = "") -> list[Option]:
    # Same signature as the others so the picker can treat every source alike.
    # The key is still required: an unusable key should fail here exactly as it
    # does everywhere else, rather than showing a list that cannot be used.
    if not api_key:
        raise ElevenLabsAuthError("no ElevenLabs API key is configured.")
    needle = query.strip().lower()
    return [o for o in _MUSIC_MODELS if not needle or needle in o.label.lower()]


# ── generation ──────────────────────────────────────────────────────────────


async def synthesize_speech(
    api_key: str,
    *,
    text: str,
    voice_id: str,
    model_id: str,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> bytes:
    if not text.strip():
        raise ElevenLabsError("there is no text to read aloud.")
    if not voice_id:
        raise ElevenLabsError("no voice is chosen on the ElevenLabs connection.")
    body: dict[str, Any] = {"text": text}
    if model_id:
        body["model_id"] = model_id
    return await _post_audio(
        f"/v1/text-to-speech/{voice_id}",
        api_key,
        body,
        timeout=_SPEECH_TIMEOUT,
        what="speech",
        output_format=output_format,
    )


async def compose_music(
    api_key: str,
    *,
    prompt: str,
    length_ms: int = 0,
    model_id: str = "",
    instrumental: bool = False,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> bytes:
    if not prompt.strip():
        raise ElevenLabsError("there is nothing to compose from.")

    body: dict[str, Any] = {"prompt": prompt}
    if model_id:
        body["model_id"] = model_id
    if instrumental:
        body["force_instrumental"] = True
    if length_ms:
        # Clamped rather than refused: somebody asking for twelve minutes wants
        # the longest piece available, not an error about a limit they had no
        # way of knowing.
        body["music_length_ms"] = max(MIN_MUSIC_MS, min(int(length_ms), MAX_MUSIC_MS))

    return await _post_audio(
        "/v1/music",
        api_key,
        body,
        timeout=_MUSIC_TIMEOUT,
        what="music",
        output_format=output_format,
    )
