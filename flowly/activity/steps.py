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

# Tools that belong to talking, not to work done for the owner: recalling
# from its own memory or past conversations, noting something down, asking
# the owner, drafting a plan for approval, reading its own recipes. A turn
# that used only these is a conversation. Everything else counts as work, so
# a tool added tomorrow (a new connection, say) counts without a change here.
CONVERSATION_TOOLS = frozenset({
    "knowledge_graph", "session_search", "sessions_list",
    "clarify", "agent_setup_ask", "plan",
    "skills_list", "skill_view",
})

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


def is_work(step: dict[str, Any]) -> bool:
    """Whether one recorded step was work done for the owner, not conversation."""
    tool = step.get("tool") if isinstance(step, dict) else None
    if not isinstance(tool, str) or not tool:
        return False
    # By the tool's name: a memory search is described as a search, but it is
    # still the agent recalling, not looking something up for the owner.
    return not tool.startswith(_MEMORY_PREFIX) and tool not in CONVERSATION_TOOLS


# What a task was, for its icon, most consequential first: what it sent or
# put on a calendar outranks what it made, which outranks what it read on
# the way. A task takes the first of these any of its working steps belongs
# to. Apps show an unknown kind with their general icon, so a kind added
# here never breaks an older app.
TASK_KINDS: tuple[str, ...] = (
    "message", "calendar", "image", "video", "voice", "writing", "code",
    "connection", "team", "research", "browse", "files",
)
_MESSAGE_TOOLS = frozenset({"email", "message", "voice_call"})
_CALENDAR_TOOLS = frozenset({"google_calendar", "google_tasks", "cron"})
_WRITING_TOOLS = frozenset({"artifact", "flowlet", "obsidian_write", "obsidian_append"})
_APP_TOOLS = frozenset({"github", "linear", "trello", "sentry", "google_drive", "google_contacts",
                        "obsidian_read", "obsidian_search", "obsidian_list", "obsidian_ingest"})
_BY_STEP_KIND = {"write": "writing", "exec": "code", "mcp": "connection", "bot": "team", "agent": "team",
                 "search": "research", "web": "browse", "read": "files"}


def task_kind_of(step: dict[str, Any]) -> str | None:
    """Which of ``TASK_KINDS`` one working step belongs to, if any."""
    if not is_work(step):
        return None
    tool = str(step.get("tool") or "")
    if tool in _MESSAGE_TOOLS:
        return "message"
    if tool in _CALENDAR_TOOLS:
        return "calendar"
    if tool == "image_generate":
        return "image"
    if tool.startswith("video_"):
        return "video"
    if tool == "voice_generate":
        return "voice"
    if tool in _WRITING_TOOLS:
        return "writing"
    if tool in _APP_TOOLS or tool.startswith("ha_"):
        return "connection"
    return _BY_STEP_KIND.get(str(step.get("kind") or ""))


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
