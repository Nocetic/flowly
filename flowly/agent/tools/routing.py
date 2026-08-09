"""Toolset and platform routing policy for the model-facing tool surface.

The registry keeps every constructed tool available for direct runtime lookup,
while this module decides which subsets may be advertised to a model turn.
Unknown/plugin tools deliberately fall into ``extensions`` so an explicit
platform allowlist cannot accidentally gain new capabilities after an update.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

TOOLSET_MEMBERS: dict[str, frozenset[str]] = {
    "filesystem": frozenset({
        "read_file", "write_file", "edit_file", "list_dir", "memory_append",
    }),
    "skills": frozenset({"skill_manage", "skill_view", "skill_improve"}),
    "execution": frozenset({"exec", "process", "docker", "system"}),
    "web": frozenset({"web_search", "web_fetch", "web_extract", "x_search"}),
    "delivery": frozenset({"message"}),
    "interactive": frozenset({"clarify", "plan"}),
    "media": frozenset({
        "screenshot", "video_analyze", "image_generate", "video_generate",
        "voice_generate", "voice_call", "computer", "browser_tab",
        "browser_plan",
    }),
    "delegation": frozenset({
        "spawn", "builtin_agent", "sessions_list", "delegate_to",
        "codex_session",
    }),
    "sessions": frozenset({"session_search"}),
    "scheduling": frozenset({"cron"}),
    "productivity": frozenset({
        "trello", "x", "linear", "github", "sentry", "email",
        "google_calendar", "google_drive", "google_contacts", "google_tasks",
        "ha_list_entities", "ha_get_state", "ha_list_services",
        "ha_call_service",
    }),
    "memory": frozenset({
        "memory_search", "memory_get", "knowledge_graph",
        "memory_consolidate", "memory_import", "memory_recall",
        "memory_feedback",
    }),
    "workspace": frozenset({
        "artifact", "flowlet", "board_add", "board_list", "board_get",
        "board_update", "board_run",
    }),
}

ALL_BUILTIN_TOOLSETS: frozenset[str] = frozenset(TOOLSET_MEMBERS)

# Scheduled runs have no human present to answer clarification/plan prompts and
# must not recursively schedule or directly deliver messages. Existing cron
# callers already supplied name-level blocks for cron/message; expressing the
# same boundary as toolsets makes it apply consistently to every cron entrypoint.
DEFAULT_DISABLED_TOOLSETS_BY_PLATFORM: dict[str, frozenset[str]] = {
    "cron": frozenset({"delivery", "interactive", "scheduling"}),
}

_TOOLSET_BY_NAME = {
    name: toolset
    for toolset, names in TOOLSET_MEMBERS.items()
    for name in names
}


def infer_toolset(tool_name: str) -> str:
    """Return the stable toolset for a registered tool name."""
    name = str(tool_name or "").strip()
    direct = _TOOLSET_BY_NAME.get(name)
    if direct:
        return direct
    if name.startswith("mcp_"):
        return "mcp"
    if name.startswith("obsidian_"):
        return "productivity"
    if name.startswith("board_"):
        return "workspace"
    return "extensions"


def resolve_toolset_filters(
    platform: str | None,
    *,
    enabled: bool = True,
    platform_toolsets: Mapping[str, Sequence[str]] | None = None,
    disabled_toolsets: Sequence[str] | None = None,
) -> tuple[frozenset[str] | None, frozenset[str]]:
    """Resolve an optional allowlist plus the effective denylist.

    A missing platform entry means "all toolsets" for backward compatibility.
    An explicitly configured empty list means "no tools" for that platform.
    """
    if not enabled:
        return None, frozenset()

    normalized_platform = str(platform or "").strip().lower()
    configured = {
        str(key).strip().lower(): values
        for key, values in (platform_toolsets or {}).items()
    }
    selected: frozenset[str] | None = None
    selected_values = configured.get(normalized_platform)
    if selected_values is None and "*" in configured:
        # A wildcard applies one policy to every current and future surface;
        # a platform-specific entry remains authoritative when both exist.
        selected_values = configured["*"]
    if selected_values is not None:
        selected = frozenset(
            str(value).strip().lower()
            for value in selected_values
            if str(value).strip()
        )

    denied = {
        str(value).strip().lower()
        for value in (disabled_toolsets or ())
        if str(value).strip()
    }
    denied.update(DEFAULT_DISABLED_TOOLSETS_BY_PLATFORM.get(normalized_platform, ()))
    return selected, frozenset(denied)
