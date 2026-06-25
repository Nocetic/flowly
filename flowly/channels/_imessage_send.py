"""Outbound transport for the iMessage channel.

Three delivery paths, tried in order:

1. **Electron bridge** — the desktop app holds the Automation (Apple
   Events) permission and exposes ``POST /imessage/send`` on its
   localhost API (``~/.flowly/electron-api.json`` → ``{port, token}``).
2. **Signed helper** — ``flowly-imessage-helper``, a standalone
   code-signed Mach-O that makes ITSELF the TCC responsible process
   (responsibility-disclaim re-exec). It therefore owns a stable
   Automation identity ("Flowly") independent of the launching terminal,
   whose TCC identity is unreliable (bare ``osascript`` from iTerm gets
   ``-10004`` with no fixable Automation entry). This is the path that
   works from tmux / launchd / any terminal once granted once.
3. **osascript** — bare AppleScript fallback for when the helper hasn't
   been built (e.g. source checkout without a compile step).

Text and file paths are passed to the helper via ``argv`` and to bare
AppleScript via ``argv`` too — never interpolated into script source —
so message content cannot inject script code.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from loguru import logger

from flowly.profile import get_flowly_home

OSASCRIPT_TIMEOUT = 10  # a stuck consent prompt must not wedge the dispatcher
HELPER_TIMEOUT = 15  # helper re-execs itself + may surface a consent prompt
MAX_BUBBLE_CHARS = 4000  # iMessage practical per-bubble cap

# The signed send helper is a registered .app bundle next to this module
# (built by ``helpers/build.sh``; shipped pre-built + Developer-ID-signed
# in the desktop bundle). We exec its inner Mach-O directly — the binary
# re-execs itself with responsibility disclaim so it becomes its own TCC
# responsible process, and being a registered LaunchServices app lets
# macOS name it ("Flowly iMessage Helper") and surface the Automation
# consent prompt.
_HELPER_APP = (
    Path(__file__).parent / "helpers" / "Flowly iMessage Helper.app"
)
_HELPER_PATH = _HELPER_APP / "Contents" / "MacOS" / "flowly-imessage-helper"

# Send a 1:1 message by recipient handle (phone/email) through the
# iMessage account. Verified on macOS 27: this "buddy of account" form
# DELIVERS, whereas `send ... to chat id "any;-;<handle>"` against an
# existing chat is refused with -10004. So for DMs this is the primary
# form and chat-id is the fallback. Only reaches existing recipients —
# new-conversation initiation is a documented v1 non-goal.
_SEND_TO_BUDDY = """\
on run argv
    set theText to item 1 of argv
    set theHandle to item 2 of argv
    tell application "Messages"
        set theAccount to 1st account whose service type = iMessage
        send theText to buddy theHandle of theAccount
    end tell
end run
"""

# Sent to a conversation by its chat id (groups, or DM fallback).
_SEND_TO_CHAT = """\
on run argv
    set theText to item 1 of argv
    set theChatId to item 2 of argv
    tell application "Messages"
        send theText to chat id theChatId
    end tell
end run
"""

_SEND_FILE_TO_BUDDY = """\
on run argv
    set thePath to item 1 of argv
    set theHandle to item 2 of argv
    set theFile to POSIX file thePath
    tell application "Messages"
        set theAccount to 1st account whose service type = iMessage
        send theFile to buddy theHandle of theAccount
    end tell
end run
"""

_SEND_FILE_TO_CHAT = """\
on run argv
    set thePath to item 1 of argv
    set theChatId to item 2 of argv
    set theFile to POSIX file thePath
    tell application "Messages"
        send theFile to chat id theChatId
    end tell
end run
"""


class IMessageSendError(Exception):
    """Raised when neither delivery path could send the message."""


def strip_markdown(text: str) -> str:
    """Flatten markdown to plain text — iMessage renders none of it."""
    if not text:
        return ""
    # Fenced code blocks: keep the code, drop the fences.
    text = re.sub(r"```[\w]*\n?([\s\S]*?)```", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Headers / blockquotes → bare line.
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # Links: keep "label (url)".
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    # Emphasis markers.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<![a-zA-Z0-9])__(.+?)__(?![a-zA-Z0-9])", r"\1", text)
    text = re.sub(r"(?<![a-zA-Z0-9*])\*([^*\n]+)\*(?![a-zA-Z0-9*])", r"\1", text)
    text = re.sub(r"(?<![a-zA-Z0-9_])_([^_\n]+)_(?![a-zA-Z0-9_])", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # Bullets → middle dot (matches other plain-text channels' look).
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)
    return text.strip()


def split_bubbles(text: str, limit: int = MAX_BUBBLE_CHARS) -> list[str]:
    """Split a long reply into iMessage-sized bubbles on paragraph breaks."""
    if not text:
        return []
    paragraphs = text.split("\n\n")
    bubbles: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            bubbles.append(current)
        # A single paragraph larger than the limit gets hard-wrapped.
        while len(para) > limit:
            bubbles.append(para[:limit])
            para = para[limit:]
        current = para
    if current:
        bubbles.append(current)
    return bubbles


# ── Electron bridge path ───────────────────────────────────────────────


def _electron_api_file() -> Path:
    return get_flowly_home() / "electron-api.json"


def _send_via_electron_sync(payload: dict) -> str | None:
    """POST to the desktop app's bridge.

    Returns ``None`` on success, an error string on a definitive
    failure, or ``"UNAVAILABLE"`` when the bridge is absent/unreachable
    (caller falls through to osascript).
    """
    api_file = _electron_api_file()
    if not api_file.exists():
        return "UNAVAILABLE"
    try:
        api_data = json.loads(api_file.read_text())
        port = int(api_data["port"])
        token = str(api_data["token"])
    except (ValueError, KeyError, json.JSONDecodeError, OSError):
        return "UNAVAILABLE"

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/imessage/send",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            if data.get("success"):
                return None
            return f"Electron iMessage send failed: {data.get('error', 'unknown')}"
    except urllib.error.HTTPError as e:
        # 404 = older desktop build without the endpoint; 401 = stale token.
        if e.code in (401, 404):
            return "UNAVAILABLE"
        return f"Electron iMessage send HTTP {e.code}"
    except (urllib.error.URLError, OSError):
        return "UNAVAILABLE"
    except Exception as e:  # noqa: BLE001 — bridge must never crash the channel
        logger.warning(f"Electron iMessage delegation failed: {e}")
        return "UNAVAILABLE"


# ── osascript path ─────────────────────────────────────────────────────


def _run_osascript_sync(script: str, args: list[str]) -> str | None:
    """Run an AppleScript with argv. Returns None on success, error text on failure."""
    try:
        proc = subprocess.run(
            ["osascript", "-", *args],
            input=script,
            capture_output=True,
            text=True,
            timeout=OSASCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return (
            "osascript timed out — likely a pending Automation permission "
            "prompt. Grant Flowly access to Messages in System Settings → "
            "Privacy & Security → Automation."
        )
    except FileNotFoundError:
        return "osascript not found (iMessage sending requires macOS)"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "-1743" in err or "not allowed" in err.lower():
            return (
                "Automation permission denied for Messages. Grant it in "
                "System Settings → Privacy & Security → Automation."
            )
        if "-10004" in err:
            # Messages accepted the Apple Event but refused the verb —
            # seen when the calling app's Automation→Messages toggle is
            # off/stale, or the process runs under a daemon (tmux,
            # launchd) with no promptable UI context.
            return (
                f"{err} — Messages refused the send (privilege "
                "violation). Check System Settings → Privacy & Security "
                "→ Automation → your terminal → Messages is ON; if the "
                "gateway runs inside tmux or a service, run it from a "
                "plain terminal once to capture the consent prompt."
            )
        return err or f"osascript exited {proc.returncode}"
    return None


# ── signed-helper path ─────────────────────────────────────────────────


def _helper_available() -> bool:
    return _HELPER_PATH.exists() and os.access(_HELPER_PATH, os.X_OK)


def _automation_denied(err: str) -> bool:
    return "-1743" in err or "not authorized" in err.lower()


_AUTOMATION_HINT = (
    "Automation permission not granted. Approve 'Flowly iMessage Helper' "
    "under System Settings → Privacy & Security → Automation. The first "
    "send surfaces the consent prompt — approve it once at the Mac."
)


def _run_helper_sync(text: str, target: str) -> str | None:
    """Send via the signed helper .app. None on success, error on failure.

    Launched through LaunchServices (``open -W -n``) so the app is its
    own TCC responsible process and can surface the Automation prompt;
    the outcome is read back from a result file. Returns the sentinel
    ``"UNAVAILABLE"`` when the helper isn't built so the caller falls
    through to osascript.
    """
    if not _helper_available():
        return "UNAVAILABLE"

    fd, result_path = tempfile.mkstemp(prefix="flowly-imsg-", suffix=".txt")
    os.close(fd)
    try:
        try:
            subprocess.run(
                ["open", "-W", "-n", str(_HELPER_APP),
                 "--args", text, target, result_path],
                capture_output=True,
                text=True,
                timeout=HELPER_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return (
                "iMessage helper timed out — likely a pending Automation "
                "permission prompt. Approve 'Flowly iMessage Helper' under "
                "System Settings → Privacy & Security → Automation at the Mac."
            )
        except OSError as e:
            logger.debug(f"iMessage helper launch failed: {e}")
            return "UNAVAILABLE"

        try:
            result = Path(result_path).read_text(encoding="utf-8").strip()
        except OSError:
            result = ""
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass

    if result == "OK":
        return None
    if not result:
        # The app never wrote a verdict — launch likely failed before the
        # AppleScript ran. Fall through to osascript rather than claim a
        # bogus error.
        return "UNAVAILABLE"
    err = result[4:] if result.startswith("ERR:") else result
    if _automation_denied(err):
        return _AUTOMATION_HINT
    return err


def resolve_chat_guid(db_path: Path, chat_identifier: str) -> str | None:
    """Look up the Messages chat GUID for a chat_identifier.

    Prefers the iMessage-service chat when the same identifier also has
    an SMS twin (both render as one conversation in Messages.app).
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT guid, COALESCE(service_name, '') FROM chat "
                "WHERE chat_identifier = ?",
                (chat_identifier,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    rows.sort(key=lambda r: 0 if r[1] == "iMessage" else 1)
    return rows[0][0]


def send_candidates(target: str, guid: str | None) -> list[str]:
    """Ordered send-target candidates, tried until one delivers.

    A candidate WITHOUT a ``;`` is a bare handle → the "buddy of
    account" AppleScript form, which is the only form verified to
    actually deliver a DM on macOS 26/27 (``send … to chat id
    "any;-;<handle>"`` against an existing chat is refused with
    -10004). A candidate WITH a ``;`` is a ``chat id`` form, used for
    groups (no buddy) and as a DM fallback for older macOS.

    For a DM handle we therefore try: bare handle (buddy) → ``any;-;``
    → ``iMessage;-;``. For a group we try its chat-id forms. The
    gateway often can't read chat.db (no Full Disk Access) so the
    constructed forms matter — ``resolve_chat_guid`` returns ``None``.
    """
    identifier = target
    if ";" in target:
        # Caller passed a raw GUID — recover the bare identifier.
        identifier = target.rsplit(";", 1)[-1]

    is_group = bool(guid and ";+;" in guid) or ";+;" in target
    if not is_group and ";" not in target:
        # DM handle: buddy form first (delivers), then chat-id fallbacks.
        candidates = [identifier, f"any;-;{identifier}", f"iMessage;-;{identifier}"]
    else:
        sep = ";+;" if is_group else ";-;"
        candidates = [f"any{sep}{identifier}"]
        if is_group:
            candidates.append(f"any;-;{identifier}")
    for raw in (guid, target if ";" in target else None):
        if raw and raw not in candidates:
            candidates.append(raw)
    return candidates


# Remembers which chat-id form actually delivered, per target, so steady
# state is one osascript call instead of a failing probe each message.
_working_chat_id: dict[str, str] = {}
_WORKING_CACHE_MAX = 512


def _cache_working(target: str, chat_id: str) -> None:
    if len(_working_chat_id) >= _WORKING_CACHE_MAX:
        _working_chat_id.clear()
    _working_chat_id[target] = chat_id


# ── public API ─────────────────────────────────────────────────────────


def _looks_like_handle(target: str) -> bool:
    """A bare phone/email (DM) vs a group/GUID-style identifier."""
    return "@" in target or re.fullmatch(r"\+?[0-9 ()\-]{5,}", target) is not None


def _scripts_for(candidate: str, *, is_file: bool) -> str:
    """Pick the AppleScript form for a candidate (buddy vs chat id)."""
    if ";" in candidate:  # chat-id form (group, or DM fallback)
        return _SEND_FILE_TO_CHAT if is_file else _SEND_TO_CHAT
    return _SEND_FILE_TO_BUDDY if is_file else _SEND_TO_BUDDY  # bare handle


async def _dispatch_send(
    payload_arg: str,
    target: str,
    guid: str | None,
    *,
    is_file: bool,
    use_helper: bool,
) -> str | None:
    """Try every send candidate until one delivers. None on success.

    Each candidate selects its own AppleScript form (buddy vs chat id).
    For text the signed helper is preferred per candidate, but ANY
    helper failure falls through to bare osascript — osascript runs
    under the terminal's (often already-granted) TCC identity and can
    succeed where the unregistered helper is refused (``-1743``: a bare
    Mach-O can't surface a consent prompt).
    """
    candidates = send_candidates(target, guid)
    cached = _working_chat_id.get(target)
    if cached in candidates:
        candidates.remove(cached)
        candidates.insert(0, cached)

    last_err: str | None = None
    for candidate in candidates:
        script = _scripts_for(candidate, is_file=is_file)
        if use_helper and not is_file:
            err = await asyncio.to_thread(_run_helper_sync, payload_arg, candidate)
            if err is not None:
                logger.debug(f"iMessage helper {candidate!r} failed: {err}")
                err = await asyncio.to_thread(
                    _run_osascript_sync, script, [payload_arg, candidate]
                )
        else:
            err = await asyncio.to_thread(
                _run_osascript_sync, script, [payload_arg, candidate]
            )
        if err is None:
            _cache_working(target, candidate)
            return None
        last_err = err
        logger.debug(f"iMessage send form {candidate!r} failed: {err}")
    return last_err


async def send_text(
    target: str,
    text: str,
    *,
    db_path: Path | None = None,
) -> None:
    """Send one text bubble to ``target`` (chat_identifier, GUID or handle).

    Raises :class:`IMessageSendError` when every path fails.
    """
    payload: dict = {"text": text}
    guid = None
    if ";" in target:
        guid = target  # caller already passed a raw chat GUID
    elif db_path is not None:
        guid = await asyncio.to_thread(resolve_chat_guid, db_path, target)
    if guid:
        payload["chatGuid"] = guid
    else:
        payload["handle"] = target

    electron_err = await asyncio.to_thread(_send_via_electron_sync, payload)
    if electron_err is None:
        return
    if electron_err != "UNAVAILABLE":
        # The bridge was reachable and definitively failed — surface it.
        raise IMessageSendError(electron_err)

    send_err = await _dispatch_send(
        text, target, guid, is_file=False, use_helper=True
    )
    if send_err is not None:
        raise IMessageSendError(send_err)


async def send_file(
    target: str,
    file_path: Path,
    *,
    db_path: Path | None = None,
) -> None:
    """Best-effort attachment send. Raises :class:`IMessageSendError` on failure."""
    guid = target if ";" in target else None
    if guid is None and db_path is not None:
        guid = await asyncio.to_thread(resolve_chat_guid, db_path, target)

    payload: dict = {"filePath": str(file_path)}
    if guid:
        payload["chatGuid"] = guid
    else:
        payload["handle"] = target

    electron_err = await asyncio.to_thread(_send_via_electron_sync, payload)
    if electron_err is None:
        return
    if electron_err != "UNAVAILABLE":
        raise IMessageSendError(electron_err)

    os_err = await _dispatch_send(
        str(file_path), target, guid, is_file=True, use_helper=False
    )
    if os_err is not None:
        raise IMessageSendError(os_err)
