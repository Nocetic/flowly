"""What one tool call was, in terms an owner can read and nothing more.

A step keeps the tool's name, a coarse ``kind`` that clients turn into a title
in the owner's language ("Searched the web for …", "Wrote report.md"), and a
short ``target``: the search query, the file's name, the site, the program,
the other bot. Never the full arguments: a command line, a file's contents, a
request body or a token must not reach an activity list.
"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any
from urllib.parse import urlparse

KINDS: tuple[str, ...] = (
    "search", "web", "read", "write", "exec", "mcp", "bot", "agent", "media", "memory", "other",
)
TARGET_MAX_CHARS = 80

_SEARCH = {"web_search": ("query", "q"), "x_search": ("query", "q"), "session_search": ("query", "q"),
           "memory_search": ("query", "q"), "memory_recall": ("query", "q")}
_WEB = {"web_fetch": ("url", "urls")}
_READ = {"read_file": ("path", "file_path"), "list_dir": ("path", "directory", "dir")}
_WRITE = {"write_file": ("path", "file_path"), "edit_file": ("path", "file_path")}
_EXEC = {"exec": ("command", "cmd"), "process": ("command", "cmd"), "docker": ("command", "cmd"),
         "codex_session": ()}
_BOT = {"message_profile": ("profile", "target", "name"), "delegate_to": ("profile", "target", "name")}
_AGENT = {"spawn": ("label", "name"), "builtin_agent": ("agent", "name")}
_MEDIA = {"image_generate": (), "video_generate": (), "voice_generate": (), "video_analyze": ()}
_MEMORY_PREFIX = "memory_"

# A value that looks like a credential never becomes a target, whatever key it
# came from.
_SECRET = re.compile(
    r"(?i)\b(bearer\s+\S+|sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9]{8,}|xox[abpr]-\S+|AKIA[0-9A-Z]{12,})"
)


def _first(args: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, list):
            value = next((item for item in value if isinstance(item, str) and item.strip()), None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _line(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if _SECRET.search(text):
        return ""
    return text if len(text) <= TARGET_MAX_CHARS else text[: TARGET_MAX_CHARS - 1].rstrip() + "…"


def _file_name(path: str) -> str:
    name = PurePath(path.strip()).name
    return _line(name or path)


def _site(url: str) -> str:
    try:
        host = urlparse(url.strip()).hostname or ""
    except ValueError:
        return ""
    return _line(host[4:] if host.startswith("www.") else host)


def _program(command: str) -> str:
    # The program only: arguments carry paths, hosts and, too often, secrets.
    first = command.strip().split(None, 1)[0] if command.strip() else ""
    return _line(PurePath(first).name)


def describe_step(tool_name: str, args: Any) -> dict[str, str]:
    """``{tool, kind, target}`` for one call; ``target`` may be empty."""
    tool = tool_name if isinstance(tool_name, str) else ""
    values = args if isinstance(args, dict) else {}
    if tool in _SEARCH:
        return {"tool": tool, "kind": "search", "target": _line(_first(values, _SEARCH[tool]))}
    if tool in _WEB or tool.startswith("browser"):
        return {"tool": tool, "kind": "web", "target": _site(_first(values, _WEB.get(tool, ("url",))))}
    if tool in _READ:
        return {"tool": tool, "kind": "read", "target": _file_name(_first(values, _READ[tool]))}
    if tool in _WRITE:
        return {"tool": tool, "kind": "write", "target": _file_name(_first(values, _WRITE[tool]))}
    if tool in _EXEC:
        return {"tool": tool, "kind": "exec", "target": _program(_first(values, _EXEC[tool]))}
    if tool.startswith("mcp_") or tool == "mcp":
        return {"tool": tool, "kind": "mcp", "target": _line(tool[4:] if tool.startswith("mcp_") else "")}
    if tool in _BOT:
        return {"tool": tool, "kind": "bot", "target": _line(_first(values, _BOT[tool]))}
    if tool in _AGENT:
        return {"tool": tool, "kind": "agent", "target": _line(_first(values, _AGENT[tool]))}
    if tool in _MEDIA:
        return {"tool": tool, "kind": "media", "target": ""}
    if tool.startswith(_MEMORY_PREFIX):
        return {"tool": tool, "kind": "memory", "target": ""}
    return {"tool": tool, "kind": "other", "target": ""}
