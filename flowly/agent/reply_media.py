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


def media_envelope(paths: list[str], summary: str) -> str:
    """Build a tool result asking the loop to attach ``paths`` to the reply.

    ``summary`` is the human, model-facing text shown in place of the envelope.
    """
    return json.dumps({_KEY: [str(p) for p in paths], "summary": summary})


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
