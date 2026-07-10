"""Photo capture → a `vision` action turns an image into a new list item.

A `photo` component carries a ``{op:"vision", prompt, into}`` action. When the
user snaps/picks a photo, the client downscales it and calls ``flowlets.capture``;
this module stores the photo (if the target list keeps one), runs a single
isolated model turn over the image, coerces the reply into the list's item schema
(the schema *is* the output contract, exactly like a data source), writes any
``image``-typed field with the stored attachment id, and appends the row.

The photo never enters flowlet state or a broadcast — only its attachment id
does; the bytes live in the store's attachment dir and are served on demand.
"""

from __future__ import annotations

import os
import tempfile
from datetime import tzinfo
from pathlib import Path
from typing import Any, Awaitable, Callable

from flowly.flowlets import catalog
from flowly.flowlets.queries import resolve_values
from flowly.flowlets.sources import _coerce_field, _extract_json
from flowly.flowlets.store import FlowletStore
from flowly.flowlets.store import now_ms as _now_ms

# (flowlet, prompt, image_path) → the model's reply text (JSON expected).
# The image travels as a LOCAL FILE PATH — the agent's media pipeline consumes
# file paths / http URLs (exactly like chat attachments, which are saved to
# ``~/.flowly/media`` first); a data URI would be dropped.
VisionRunner = Callable[[dict, str, str], Awaitable[str | None]]


class FlowletCaptureError(Exception):
    """A photo capture couldn't be applied (bad component, model failure, …)."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _build_vision_prompt(user_prompt: str, fields: dict) -> str:
    non_image = {f: t for f, t in fields.items() if t != "image"}
    cols = ", ".join(f"{f}: {t}" for f, t in non_image.items())
    return (
        "[Flowlet photo capture]\n"
        f"{user_prompt}\n\n"
        "Look at the attached photo and return ONLY a JSON object with these fields: "
        "{" + cols + "}. "
        'string → text, number → a number, bool → true/false, date → "YYYY-MM-DD". '
        "No prose, no markdown fences — just the JSON object. If a field can't be "
        "determined, omit it."
    )


async def apply_capture(
    store: FlowletStore,
    flowlet: dict,
    component: dict,
    image_bytes: bytes,
    *,
    runner: VisionRunner | None,
    tz: tzinfo | None = None,
) -> dict:
    """Store the photo, interpret it, append the item; return fresh values."""
    if runner is None:
        raise FlowletCaptureError("UNAVAILABLE", "photo capture can't run right now")
    flowlet_id = flowlet["id"]
    defn = flowlet["definition"]
    action = component.get("action") or {}
    if action.get("op") != "vision":
        raise FlowletCaptureError("INVALID", "this component has no photo/vision action")

    into = action.get("into")
    spec = (defn.get("state") or {}).get(into) or {}
    if spec.get("type") != "list":
        raise FlowletCaptureError("INVALID", f"vision `into` '{into}' is not a list")
    fields = spec.get("item") or {}
    keeps_photo = any(t == "image" for t in fields.values())

    # Store the photo only if the list has somewhere to hold it — otherwise this
    # is an analyze-only capture and keeping the file would orphan it.
    att_id = store.put_attachment(flowlet_id, image_bytes) if keeps_photo else None
    # The model turn needs the image as a local FILE PATH (the media pipeline's
    # contract). Reuse the stored attachment's file; analyze-only captures get a
    # temp file that is removed after the turn.
    image_path = store.attachment_path(flowlet_id, att_id) if att_id else None
    tmp: Path | None = None
    if image_path is None:
        fd, name = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)
        tmp = Path(name)
        image_path = tmp
    try:
        prompt = _build_vision_prompt(action.get("prompt", ""), fields)
        reply = await runner(flowlet, prompt, str(image_path))
        parsed = _extract_json(reply or "")
    except Exception as exc:  # noqa: BLE001 — model/parse failure: don't keep the orphan
        if att_id:
            store.delete_attachment(flowlet_id, att_id)
        raise FlowletCaptureError("UNAVAILABLE", f"couldn't read the photo ({exc})")
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)

    # Shape the reply into the item schema (lenient — bad fields drop).
    item: dict[str, Any] = {}
    if isinstance(parsed, dict):
        for f, ftype in fields.items():
            if ftype == "image" or f not in parsed:
                continue
            cv = _coerce_field(ftype, parsed[f])
            if cv is not None:
                item[f] = cv
    if att_id:
        for f, ftype in fields.items():
            if ftype == "image":
                item[f] = att_id
    item["id"] = f"itm_{os.urandom(4).hex()}"

    items = list(store.get_state(flowlet_id).get(into) or [])
    limit = int(spec.get("max") or catalog.MAX_LIST_ITEMS)
    if len(items) >= limit:
        if att_id:
            store.delete_attachment(flowlet_id, att_id)
        raise FlowletCaptureError("INVALID", f"'{into}' is full ({limit} items)")
    items.append(item)
    store.set_state(flowlet_id, into, items)

    return resolve_values(
        defn, store.get_state(flowlet_id), store.get_events(flowlet_id), _now_ms(), tz
    )
