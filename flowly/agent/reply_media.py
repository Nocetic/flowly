"""Reply-media envelope — attach tool-produced files to the agent's OWN reply.

A media-producing tool (``image_generate``, ``screenshot``, …) returns
:func:`media_envelope` as its result. The agent loop parses it with
:func:`extract_reply_media`, collects the paths onto the turn's outgoing
``OutboundMessage.media`` (delivered to every surface — web/iOS via ``/api/media``,
messaging channels via native upload), and shows the model the human ``summary``
instead of the raw JSON.

This is how mainstream assistants surface generated media: it rides the assistant
turn (one bubble), and the model never orchestrates a separate send. The
``message`` tool stays for its real job — proactive / cross-channel sends.

One module owns the contract so the producer (tools) and consumer (loop) can't
drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

# Envelope key. Present in the JSON => the loop should peel media off and attach
# it to the reply. Chosen to be unlikely in any normal tool result.
_KEY = "_reply_media"

# Optional companion key: per-file descriptors (duration, dimensions, poster)
# that a path alone cannot express. Images never needed it — a client can open
# the file and see everything. Video does: a player must know the duration and
# aspect before it fetches a byte. Old envelopes omit the key and old readers
# ignore it, so both shapes coexist without a version negotiation.
_ASSETS_KEY = "_reply_media_assets"


def media_envelope(paths: list[str], summary: str, assets: list | None = None) -> str:
    """Build a tool result asking the loop to attach ``paths`` to the reply.

    ``summary`` is the human, model-facing text shown in place of the envelope.
    ``assets`` are optional :class:`~flowly.media.assets.MediaAsset` records for
    those paths. The path list stays authoritative for delivery, so a tool with
    no metadata to add keeps calling this with two arguments.
    """
    payload: dict = {_KEY: [str(p) for p in paths], "summary": summary}
    if assets:
        payload[_ASSETS_KEY] = [a.to_dict() for a in assets]
    return json.dumps(payload)


def extract_reply_media(raw_result: str) -> tuple[list[str], str | None]:
    """Parse a tool result → ``(existing_file_paths, human_summary)``.

    Returns ``([], None)`` when ``raw_result`` is not a reply-media envelope, so
    the loop can skip non-media tools cheaply (a substring guard avoids parsing
    every tool result as JSON). Only paths that exist on disk are returned — a
    stale or fabricated path is dropped rather than handed to a channel.
    """
    if not isinstance(raw_result, str) or _KEY not in raw_result:
        return [], None
    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return [], None
    if not isinstance(parsed, dict):
        return [], None
    media = parsed.get(_KEY)
    if not isinstance(media, list) or not media:
        return [], None
    paths = [p for p in media if isinstance(p, str) and p and Path(p).is_file()]
    summary = parsed.get("summary")
    return paths, (summary if isinstance(summary, str) else None)


def extract_reply_media_assets(raw_result: str) -> list:
    """Parse the optional asset descriptors out of a reply-media envelope.

    Kept separate from :func:`extract_reply_media` so the loop's existing
    two-value contract is untouched: callers that only need paths don't have to
    learn about assets, and an envelope without them is not an error.

    Only descriptors whose file still exists are returned — the same rule the
    path list follows, so the two can never disagree about what is deliverable.
    """
    if not isinstance(raw_result, str) or _ASSETS_KEY not in raw_result:
        return []
    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []

    from flowly.media.assets import assets_from_meta

    return [a for a in assets_from_meta(parsed.get(_ASSETS_KEY)) if Path(a.path).is_file()]
