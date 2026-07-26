"""Validation and prompt formatting for Flowly Browser annotations.

Desktop sends the annotated screenshot as a normal image attachment and a
small structured manifest alongside it. This module converts only the
allow-listed manifest fields into model-visible text. It deliberately ignores
unknown keys and never serializes arbitrary DOM attributes or markup.
"""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import urlparse

MAX_BUNDLES = 3
MAX_ANNOTATIONS_PER_BUNDLE = 20
MAX_COMMENT_CHARS = 2_000
MAX_SELECTED_TEXT_CHARS = 4_000
MAX_SELECTOR_CHARS = 1_000


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    filtered = "".join(char for char in value if char in "\n\t" or ord(char) >= 32).strip()
    return filtered[:limit]


def _safe_url(value: Any) -> str:
    url = _clean(value, 2_048)
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _manifest_from_attachment(attachment: Any) -> dict[str, Any] | None:
    if not isinstance(attachment, dict):
        return None
    if attachment.get("kind") != "browser_annotation":
        return None
    manifest = attachment.get("browserAnnotation")
    if manifest is None:
        manifest = attachment.get("browser_annotation")
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        return None
    return manifest


def _format_annotation(raw: Any, fallback_number: int) -> list[str]:
    if not isinstance(raw, dict):
        return []
    target = raw.get("target")
    if not isinstance(target, dict):
        return []

    number_value = raw.get("number")
    number = number_value if isinstance(number_value, int) and number_value > 0 else fallback_number
    sensitive = target.get("sensitive") is True
    role = _clean(target.get("role"), 80)
    tag = _clean(target.get("tag"), 80)
    name = _clean(target.get("accessibleName"), 160)
    selected = "" if sensitive else _clean(target.get("text"), MAX_SELECTED_TEXT_CHARS)
    comment = _clean(raw.get("comment"), MAX_COMMENT_CHARS)
    selector = _clean(target.get("selector"), MAX_SELECTOR_CHARS)

    pieces = []
    if role:
        pieces.append(role)
    if name:
        pieces.append(f'"{name}"')
    if tag:
        pieces.append(f"<{tag}>")
    description = " ".join(pieces) or "page element"

    lines = [f"Annotation {number}: {escape(description)}"]
    if selected:
        lines.append(f"Selected text: {escape(selected)}")
    if comment:
        lines.append(f"Comment: {escape(comment)}")
    if selector:
        lines.append(f"Selector hint: {escape(selector)}")
    if sensitive:
        lines.append("Sensitive field: value omitted")
    return lines


def extract_browser_annotation_context(attachments: Any) -> str:
    """Return a bounded, model-readable annotation block."""

    if not isinstance(attachments, list):
        return ""

    bundles: list[str] = []
    for attachment in attachments:
        manifest = _manifest_from_attachment(attachment)
        if manifest is None:
            continue

        url = _safe_url(manifest.get("url"))
        title = _clean(manifest.get("pageTitle"), 300)
        raw_annotations = manifest.get("annotations")
        if not url or not isinstance(raw_annotations, list):
            continue

        lines = ["<browser_annotations>"]
        if title:
            lines.append(f"Page: {escape(title)}")
        lines.append(f"URL: {escape(url)}")
        formatted_count = 0
        for index, raw in enumerate(raw_annotations[:MAX_ANNOTATIONS_PER_BUNDLE], start=1):
            annotation_lines = _format_annotation(raw, index)
            if not annotation_lines:
                continue
            formatted_count += 1
            lines.extend(annotation_lines)
        if formatted_count == 0:
            continue
        lines.append("</browser_annotations>")
        bundles.append("\n".join(lines))
        if len(bundles) >= MAX_BUNDLES:
            break

    return "\n\n".join(bundles)


def append_browser_annotation_context(message: Any, attachments: Any) -> str:
    text = message if isinstance(message, str) else ""
    context = extract_browser_annotation_context(attachments)
    if not context:
        return text
    return f"{text.rstrip()}\n\n{context}".lstrip()
