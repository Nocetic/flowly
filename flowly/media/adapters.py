"""Turning one canonical request into whatever a given endpoint actually takes.

Every video model wants the same handful of things — a prompt, a length, a
shape, sometimes a starting image — and every one of them spells those
differently. Kling calls the still ``start_image_url``; others call it
``image_url``. Duration is a number here and the string ``"5"`` there, drawn
from an enum of exactly two values.

Writing an adapter per model would mean shipping code for each new endpoint,
which is the same trap the hard-coded model list was. Instead this module reads
the endpoint's own OpenAPI input schema and maps onto it:

  * a canonical field is matched against a list of known aliases;
  * the value is coerced to whatever the schema says that property holds,
    including snapping to an enum;
  * a field the endpoint doesn't have is dropped — EXCEPT when the user asked
    for it explicitly and its absence changes the result. Silently returning a
    silent clip to someone who asked for audio is worse than refusing.

The same schema knowledge answers "can Flowly drive this at all": if a required
input isn't one we know how to fill, the model is unsupported and we say so
rather than sending a payload the provider will reject.
"""

from __future__ import annotations

import re
from typing import Any

# Canonical field -> property names endpoints are known to use, best first.
_ALIASES: dict[str, tuple[str, ...]] = {
    "prompt": ("prompt",),
    "negative_prompt": ("negative_prompt",),
    "input_image": (
        "image_url",
        "start_image_url",
        "first_frame_image",
        "input_image_url",
        "image",
    ),
    "duration_seconds": ("duration", "duration_seconds", "video_length"),
    "aspect_ratio": ("aspect_ratio", "ratio"),
    "resolution": ("resolution", "quality", "size"),
    "generate_audio": ("generate_audio", "enable_audio", "with_audio", "audio"),
    "seed": ("seed",),
}

# What we can supply without asking the user for anything extra. A required
# property outside this set means we cannot drive the endpoint.
_FILLABLE = frozenset(_ALIASES.keys())

# Fields whose absence changes what the user GETS, so dropping them silently is
# not acceptable — the caller is told instead.
_MUST_NOT_DROP = frozenset({"generate_audio", "input_image"})

# Aspect ratio is conditional. For text-to-video it decides the framing and
# nothing else can supply it, so asking for 9:16 and receiving landscape is a
# wrong video. For image-to-video the SOURCE STILL defines the frame — several
# endpoints (every Kling image-to-video, for one) legitimately expose no aspect
# field at all, and refusing them would fail a request the model would have
# answered correctly.
_ASPECT_DECIDES_FRAMING = frozenset({"text-to-video", "text-to-image"})


def _must_not_drop_for(category: str) -> frozenset[str]:
    if category in _ASPECT_DECIDES_FRAMING:
        return _MUST_NOT_DROP | {"aspect_ratio"}
    return _MUST_NOT_DROP


class AdapterError(ValueError):
    """The request cannot be expressed for this endpoint."""


def input_schema_from_openapi(openapi: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull the endpoint's input object schema out of its OpenAPI document.

    fal names it ``<Something>Input`` under ``components.schemas``. Falling back
    to the request body of the first path keeps this working if that naming
    convention ever changes.
    """
    if not isinstance(openapi, dict):
        return None
    schemas = (openapi.get("components") or {}).get("schemas")
    if isinstance(schemas, dict):
        for name, schema in schemas.items():
            if isinstance(name, str) and name.endswith("Input") and isinstance(schema, dict):
                if isinstance(schema.get("properties"), dict):
                    return schema
    for path_item in (openapi.get("paths") or {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            content = ((operation.get("requestBody") or {}).get("content") or {})
            for media_type in content.values():
                schema = (media_type or {}).get("schema")
                if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
                    return schema
    return None


def _properties(schema: dict[str, Any]) -> dict[str, Any]:
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


def _required(schema: dict[str, Any]) -> list[str]:
    required = schema.get("required")
    return [r for r in (required or []) if isinstance(r, str)]


def resolve_property(schema: dict[str, Any], canonical: str) -> str | None:
    """The endpoint's own name for a canonical field, or None if it has none."""
    props = _properties(schema)
    for alias in _ALIASES.get(canonical, ()):  # ordered: best match first
        if alias in props:
            return alias
    return None


def _canonical_for_property(name: str) -> str | None:
    for canonical, aliases in _ALIASES.items():
        if name in aliases:
            return canonical
    return None


def compatibility_report(category: str, schema: dict[str, Any] | None) -> tuple[str, str]:
    """``(verdict, reason)`` for one endpoint.

    ``ready`` means we can fill every required input AND the field that defines
    the category is present (a prompt for text-to-*, a still for image-to-*).
    ``compatible`` means the schema is unknown, so it will be judged when it
    runs. ``unsupported`` means the endpoint needs something Flowly has no way
    to provide — and the reason names it, because "this model can't be used" is
    only useful if it says what the model wanted.
    """
    from flowly.media.catalog import COMPAT_COMPATIBLE, COMPAT_READY, COMPAT_UNSUPPORTED

    if not schema:
        return COMPAT_COMPATIBLE, ""

    unfillable = [
        required
        for required in _required(schema)
        if (_canonical_for_property(required) or "") not in _FILLABLE
    ]
    if unfillable:
        return COMPAT_UNSUPPORTED, "needs " + ", ".join(sorted(unfillable))

    needs_image = category in ("image-to-video", "image-to-image")
    has_prompt = resolve_property(schema, "prompt") is not None
    has_image = resolve_property(schema, "input_image") is not None

    if needs_image and not has_image:
        return COMPAT_UNSUPPORTED, "takes no starting image"
    if not needs_image and not has_prompt:
        return COMPAT_UNSUPPORTED, "takes no text prompt"
    return COMPAT_READY, ""


def verdict_for(category: str, schema: dict[str, Any] | None) -> str:
    """Just the verdict from :func:`compatibility_report`."""
    return compatibility_report(category, schema)[0]


# -- value coercion ----------------------------------------------------------


def _enum_values(prop: dict[str, Any]) -> list[Any]:
    values = prop.get("enum")
    if isinstance(values, list) and values:
        return values
    # fal often expresses an enum through anyOf/oneOf of const values.
    for key in ("anyOf", "oneOf"):
        for branch in prop.get(key) or []:
            if isinstance(branch, dict) and isinstance(branch.get("enum"), list):
                return branch["enum"]
    return []


def _prop_types(prop: dict[str, Any]) -> set[str]:
    raw = prop.get("type")
    types: set[str] = set()
    if isinstance(raw, str):
        types.add(raw)
    elif isinstance(raw, list):
        types.update(t for t in raw if isinstance(t, str))
    for key in ("anyOf", "oneOf"):
        for branch in prop.get(key) or []:
            if isinstance(branch, dict) and isinstance(branch.get("type"), str):
                types.add(branch["type"])
    return types


def _snap_to_enum(value: Any, options: list[Any]) -> Any | None:
    """Nearest allowed option, or None when nothing sensible matches.

    Numeric enums snap to the closest value — asking for 7 seconds from a model
    that offers 5 or 10 should give 5, not an error. Everything else must match
    case-insensitively as a string.
    """
    text = str(value).strip().lower()
    for option in options:
        if str(option).strip().lower() == text:
            return option

    def _as_number(candidate: Any) -> float | None:
        """Leading number in a value, unit suffix and all.

        Enums are written for humans: durations come as ``"6s"``, resolutions
        as ``"720p"``. Parsing those as plain floats fails, which used to drop
        the field entirely — the user asked for 6 seconds and silently got the
        model's default.
        """
        match = re.match(r"\s*(\d+(?:\.\d+)?)", str(candidate))
        if match is None:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    # Ratios are not quantities. "9:16" starts with a 9, and snapping that to
    # the nearest number among ["16:9", "1:1"] would hand back landscape when
    # the user asked for portrait — a silently wrong video, not an error.
    if any(":" in str(v) for v in [value, *options]):
        return None

    wanted = _as_number(value)
    if wanted is None:
        return None
    numeric = [(o, _as_number(o)) for o in options]
    numeric = [(o, n) for o, n in numeric if n is not None]
    if not numeric:
        return None
    return min(numeric, key=lambda pair: abs(pair[1] - wanted))[0]


def coerce_value(value: Any, prop: dict[str, Any]) -> Any | None:
    """Fit ``value`` to one property's schema, or None when it cannot be."""
    if value is None:
        return None
    options = _enum_values(prop)
    if options:
        return _snap_to_enum(value, options)

    types = _prop_types(prop)
    if "boolean" in types:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    if "integer" in types:
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None
    if "number" in types:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if "string" in types:
        return str(value)
    return value


def build_payload(
    schema: dict[str, Any],
    request: dict[str, Any],
    *,
    explicit: frozenset[str] = frozenset(),
    category: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Canonical request → provider payload.

    Returns ``(payload, dropped)``. ``dropped`` names canonical fields the
    endpoint has no home for; the caller decides whether that is acceptable.
    ``explicit`` marks fields the user asked for by name, which turns a
    consequential drop into an error instead of a surprise — and ``category``
    decides which drops are consequential, since a field that determines the
    result for one kind of generation is redundant for another.
    """
    payload: dict[str, Any] = {}
    dropped: list[str] = []

    for canonical, value in request.items():
        if value is None:
            continue
        prop_name = resolve_property(schema, canonical)
        if prop_name is None:
            dropped.append(canonical)
            continue
        coerced = coerce_value(value, _properties(schema).get(prop_name) or {})
        if coerced is None:
            dropped.append(canonical)
            continue
        payload[prop_name] = coerced

    must_not_drop = _must_not_drop_for(category)
    consequential = [f for f in dropped if f in must_not_drop and f in explicit]
    if consequential:
        raise AdapterError(
            "this model does not support: " + ", ".join(sorted(consequential))
        )

    missing = [r for r in _required(schema) if r not in payload]
    if missing:
        raise AdapterError("this model needs values Flowly can't supply: " + ", ".join(missing))

    return payload, dropped


# -- output normalization ----------------------------------------------------

_VIDEO_KEYS = ("video", "videos", "output", "result")
_IMAGE_KEYS = ("images", "image")


def _urls_from(node: Any) -> list[str]:
    if isinstance(node, str) and node.startswith(("http://", "https://")):
        return [node]
    if isinstance(node, dict):
        url = node.get("url")
        return [url] if isinstance(url, str) and url.startswith(("http://", "https://")) else []
    if isinstance(node, list):
        out: list[str] = []
        for item in node:
            out.extend(_urls_from(item))
        return out
    return []


def extract_media_urls(result: Any, *, kind: str) -> list[str]:
    """Pull downloadable URLs out of a provider result.

    Endpoints disagree about the envelope — ``{"video": {"url"}}``,
    ``{"videos": [...]}``, sometimes a bare ``{"url"}`` — so we look through the
    known shapes rather than hard-coding one per model.
    """
    if not isinstance(result, dict):
        return _urls_from(result)
    keys = _VIDEO_KEYS if kind == "video" else _IMAGE_KEYS
    for key in keys:
        if key in result:
            urls = _urls_from(result[key])
            if urls:
                return urls
    return _urls_from(result.get("url"))
