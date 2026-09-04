"""Loss-aware rendering of MCP content into Flowly tool-result envelopes.

Flowly's core tool interface returns text, while MCP can return text, images,
audio, linked resources, embedded resources, annotations, and metadata.  The
helpers here retain that structure as JSON and replace large base64 payloads
with bounded local-file references suitable for downstream delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flowly.mcp.media_cache import (
    DEFAULT_MAX_BINARY_BYTES,
    cache_base64_payload,
    cache_media_block,
)


def mcp_attr(obj: Any, snake_name: str, wire_name: str, default: Any = None) -> Any:
    """Read an SDK field across snake_case Python and wire-name models."""
    if hasattr(obj, snake_name):
        return getattr(obj, snake_name)
    return getattr(obj, wire_name, default)


def mcp_wire_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): mcp_wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [mcp_wire_value(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True, exclude_none=True, mode="json")
    return str(value)


def _optional_protocol_fields(block: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    annotations = getattr(block, "annotations", None)
    if annotations is not None:
        out["annotations"] = mcp_wire_value(annotations)
    meta = mcp_attr(block, "meta", "_meta")
    if meta is not None:
        out["_meta"] = mcp_wire_value(meta)
    return out


@dataclass(frozen=True, slots=True)
class RenderedMCPContent:
    """Text projection plus structured descriptors for MCP content blocks."""

    text: str
    blocks: tuple[dict[str, Any], ...]
    rich: bool


def _render_resource_contents(
    resource: Any,
    *,
    max_binary_bytes: int,
) -> tuple[str, dict[str, Any]]:
    uri = str(getattr(resource, "uri", "") or "")
    mime_type = mcp_attr(resource, "mime_type", "mimeType")
    descriptor: dict[str, Any] = {"uri": uri}
    if mime_type:
        descriptor["mimeType"] = str(mime_type)
    meta = mcp_attr(resource, "meta", "_meta")
    if meta is not None:
        descriptor["_meta"] = mcp_wire_value(meta)

    text = getattr(resource, "text", None)
    if text is not None:
        descriptor["text"] = str(text)
        label = f"Resource {uri}" if uri else "Embedded resource"
        return f"{label}:\n{text}", descriptor

    blob = getattr(resource, "blob", None)
    if blob is None:
        descriptor["unavailable"] = "resource content was empty"
        return "", descriptor

    cached = cache_base64_payload(
        blob,
        mime_type or "application/octet-stream",
        max_bytes=max_binary_bytes,
    )
    if cached is None:
        descriptor["unavailable"] = "binary payload was invalid or exceeded the size limit"
        return "", descriptor
    descriptor.update({
        "path": str(cached.path),
        "size": cached.size,
    })
    if cached.mime_type.startswith(("image/", "audio/")):
        descriptor["media"] = cached.media_tag
        return cached.media_tag, descriptor
    label = f"Resource {uri}" if uri else "Embedded resource"
    return f"{label} cached at {cached.path}", descriptor


def render_content_blocks(
    blocks: list[Any] | tuple[Any, ...],
    *,
    max_binary_bytes: int = DEFAULT_MAX_BINARY_BYTES,
) -> RenderedMCPContent:
    """Render every standard MCP content type without embedding raw base64."""
    text_parts: list[str] = []
    descriptors: list[dict[str, Any]] = []
    rich = False

    for block in blocks:
        block_type = str(getattr(block, "type", "") or "")
        optional = _optional_protocol_fields(block)

        if block_type == "text" or hasattr(block, "text"):
            text = str(getattr(block, "text", "") or "")
            text_parts.append(text)
            descriptors.append({"type": "text", "text": text, **optional})
            rich = rich or bool(optional)
            continue

        if block_type in {"image", "audio"} or hasattr(block, "data"):
            rich = True
            mime_type = mcp_attr(block, "mime_type", "mimeType")
            descriptor = {
                "type": block_type or "binary",
                **({"mimeType": str(mime_type)} if mime_type else {}),
                **optional,
            }
            cached = cache_media_block(block, max_bytes=max_binary_bytes)
            if cached is None:
                descriptor["unavailable"] = (
                    "media payload was invalid, unsupported, or exceeded the size limit"
                )
            else:
                descriptor.update({
                    "media": cached.media_tag,
                    "path": str(cached.path),
                    "size": cached.size,
                })
                text_parts.append(cached.media_tag)
            descriptors.append(descriptor)
            continue

        if block_type == "resource_link" or (
            hasattr(block, "uri") and hasattr(block, "name")
        ):
            rich = True
            wire = mcp_wire_value(block)
            descriptor = dict(wire) if isinstance(wire, dict) else {"value": wire}
            descriptor.setdefault("type", "resource_link")
            descriptors.append(descriptor)
            uri = descriptor.get("uri")
            name = descriptor.get("title") or descriptor.get("name") or "Resource"
            if uri:
                text_parts.append(f"{name}: {uri}")
            continue

        if block_type == "resource" or hasattr(block, "resource"):
            rich = True
            projection, resource = _render_resource_contents(
                getattr(block, "resource", None),
                max_binary_bytes=max_binary_bytes,
            )
            descriptors.append({
                "type": "resource",
                "resource": resource,
                **optional,
            })
            if projection:
                text_parts.append(projection)
            continue

        # Preserve unknown future block types, but never pass through an
        # unbounded opaque binary field into the model context.
        rich = True
        wire = mcp_wire_value(block)
        descriptor = dict(wire) if isinstance(wire, dict) else {"value": wire}
        for binary_key in ("data", "blob"):
            if binary_key in descriptor:
                descriptor.pop(binary_key, None)
                descriptor["unavailable"] = "unknown binary content type was not inlined"
        descriptor.setdefault("type", block_type or "unknown")
        descriptors.append(descriptor)

    return RenderedMCPContent(
        text="\n".join(part for part in text_parts if part),
        blocks=tuple(descriptors),
        rich=rich,
    )


def render_resource_contents(
    contents: list[Any] | tuple[Any, ...],
    *,
    max_binary_bytes: int = DEFAULT_MAX_BINARY_BYTES,
) -> RenderedMCPContent:
    """Render the contents array returned by ``resources/read``."""
    projections: list[str] = []
    descriptors: list[dict[str, Any]] = []
    for resource in contents:
        projection, descriptor = _render_resource_contents(
            resource,
            max_binary_bytes=max_binary_bytes,
        )
        descriptors.append(descriptor)
        if projection:
            projections.append(projection)
    return RenderedMCPContent(
        text="\n".join(projections),
        blocks=tuple(descriptors),
        rich=bool(descriptors),
    )
