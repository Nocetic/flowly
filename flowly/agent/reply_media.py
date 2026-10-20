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
import mimetypes
import os
import re
import tempfile
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


def extract_reply_media(
    raw_result: str, *, require_existing: bool = True, strict: bool = False,
) -> tuple[list[str], str | None]:
    """Parse a tool result → ``(existing_file_paths, human_summary)``.

    Returns ``([], None)`` when ``raw_result`` is not a reply-media envelope, so
    the loop can skip non-media tools cheaply (a substring guard avoids parsing
    every tool result as JSON). Only paths that exist on disk are returned — a
    stale or fabricated path is dropped rather than handed to a channel.

    Trust-boundary consumers can request ``require_existing=False, strict=True``
    to inspect every declared path with their own secure file reader, rejecting
    malformed envelopes and missing files instead of silently losing artifacts.
    """
    if not isinstance(raw_result, str) or _KEY not in raw_result:
        return [], None
    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError, ValueError):
        if strict:
            raise ValueError("Invalid reply-media envelope") from None
        return [], None
    if not isinstance(parsed, dict):
        if strict:
            raise ValueError("Invalid reply-media envelope")
        return [], None
    if _KEY not in parsed:
        return [], None
    media = parsed.get(_KEY)
    if not isinstance(media, list) or not media:
        if strict:
            raise ValueError("Reply-media envelope requires a non-empty path list")
        return [], None
    if strict and any(not isinstance(path, str) or not path for path in media):
        raise ValueError("Invalid reply-media path")
    paths = [p for p in media if isinstance(p, str) and p and (not require_existing or Path(p).is_file())]
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


# ``MEDIA:/absolute/path`` — the token the channel hints tell the agent to put
# in its reply to attach a file it made (flowly/agent/prompt_blocks.py). The
# loop takes the tokens out of the final reply and attaches the files the same
# way a media tool's envelope does, so every surface gets a real attachment
# instead of the literal token. Optional backticks around the token go with it.
_MEDIA_TOKEN = re.compile(r"`?MEDIA:(/[^\s`\"'<>]+)`?")
# Punctuation a sentence puts after a path, never part of the file name we
# were pointed at (checked only when the path as written does not exist).
_TRAILING = ".,;:!?)]}"

# What a token may attach, and how large: what the message tool sends, plus
# the audio and video the clients play inline. Anything else (HTML, archives
# of other kinds, files with no type) is refused.
_DOCUMENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp",
                   "application/pdf", "text/plain", "application/zip"}
_PLAYABLE_TYPES = {"audio/mpeg", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mp4",
                   "video/mp4", "video/quicktime", "video/webm"}
_DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
_PLAYABLE_MAX_BYTES = 50 * 1024 * 1024


def media_token_roots(workspace: str | Path | None = None) -> list[Path]:
    """Where an agent's own files live: its home, its workspace, temp dirs.

    A token outside them is refused, so a reply (or a page that talked the
    agent into one) cannot attach the owner's other files to a chat.
    """
    from flowly.profile import get_flowly_home

    roots = [get_flowly_home(), Path(tempfile.gettempdir()), Path("/tmp")]
    if workspace:
        roots.append(Path(workspace).expanduser())
    resolved: list[Path] = []
    for root in roots:
        try:
            path = root.resolve()
        except OSError:
            continue
        if path not in resolved:
            resolved.append(path)
    return resolved


def deliverable_media_path(raw: str, roots: list[Path]) -> tuple[Path | None, str]:
    """``(path, "")`` when ``raw`` names a file a reply may attach, else ``(None, reason)``."""
    if not raw.startswith("/"):
        return None, "not absolute"
    try:
        path = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "not found"
    if not any(path.is_relative_to(root) for root in roots):
        return None, "outside the agent's files"
    if not path.is_file():
        return None, "not a file"
    mime, _ = mimetypes.guess_type(path.name)
    if mime in _DOCUMENT_TYPES:
        limit = _DOCUMENT_MAX_BYTES
    elif mime in _PLAYABLE_TYPES:
        limit = _PLAYABLE_MAX_BYTES
    else:
        return None, f"unsupported type {mime or 'unknown'}"
    size = path.stat().st_size
    if not 0 < size <= limit:
        return None, "empty" if size == 0 else "too large"
    return path, ""


def take_media_tokens(text: str, roots: list[Path]) -> tuple[str, list[str], list[tuple[str, str]]]:
    """Take every ``MEDIA:`` token out of ``text``.

    Returns the text without them, the files to attach (resolved, in order, no
    repeats) and the refused tokens with why. Refused tokens leave the text
    too: a raw server path is noise to the reader, and a channel adapter must
    never get a second chance to send it.
    """
    if not isinstance(text, str) or "MEDIA:" not in text:
        return text, [], []
    accepted: list[str] = []
    refused: list[tuple[str, str]] = []

    def take(match: re.Match) -> str:
        raw, tail = match.group(1), ""
        while raw and raw[-1] in _TRAILING and not os.path.exists(raw):
            raw, tail = raw[:-1], raw[-1] + tail
        path, reason = deliverable_media_path(raw, roots)
        if path is None:
            refused.append((raw, reason))
        elif str(path) not in accepted:
            accepted.append(str(path))
        return tail

    cleaned = _MEDIA_TOKEN.sub(take, text)
    # A token on its own line leaves the line empty: close the gap it leaves.
    lines = [line.rstrip() for line in cleaned.splitlines()]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return cleaned, accepted, refused


def publish_reply_file(path: str, media_dir: Path | None = None) -> str:
    """The copy of an attached file the clients can open at full size.

    Clients show a reply's attachment from a small inline preview and fetch
    the original by its name from the media directory (``/api/media``, and the
    relay's media window). A file attached from anywhere else (a screenshot in
    ``screenshots/``, a file in the workspace) was never there, so the client
    could only blow its preview up. It is published there: hard-linked when it
    can be, copied otherwise, under a name that does not overwrite another
    file. Returns the published path, or ``path`` when it is already there, is
    a URL, or cannot be published (the preview still shows then).
    """
    if not isinstance(path, str) or not path or path.startswith(("http://", "https://")):
        return path
    import shutil
    from hashlib import sha256

    from loguru import logger

    if media_dir is None:
        from flowly.profile import get_flowly_home

        media_dir = get_flowly_home() / "media"
    try:
        source = Path(path).resolve(strict=True)
        media_dir.mkdir(parents=True, exist_ok=True)
        media_dir = media_dir.resolve()
        if source.parent == media_dir:
            # Already servable; the path stays as the producing tool wrote it,
            # which is the key its asset descriptor is filed under.
            return path
        name = source.name
        if name.startswith("."):
            name = "attachment" + source.suffix
        target = media_dir / name
        if target.exists() and not target.samefile(source):
            stat = source.stat()
            digest = sha256(f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:8]
            target = media_dir / f"{source.stem}-{digest}{source.suffix}"
        if target.exists():
            if target.samefile(source):
                return str(target)
            raise FileExistsError(target)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        return str(target)
    except (OSError, ValueError) as exc:
        logger.warning(f"[media] could not publish {path} for full-size viewing: {exc}")
        return path
