"""Skill bundles — alias a list of skills under a single ``/slug`` command.

A bundle is a YAML file in ``~/.flowly/skill-bundles/`` that names
several skills the user wants to load together. Typing ``/research
solar panels`` in chat rewrites the message into the combined skill
content of the bundle followed by the user's instruction, so the
agent has every relevant skill in context for one turn.

Design notes
------------
- **Stateless.** A bundle is *not* an activation toggle: invoking it
  injects the skill content into a single turn, just like the user
  had pasted every SKILL.md body manually. The next turn, fresh
  invocation needed. This matches the way slash commands compose
  with the rest of the agent loop and avoids hidden state.
- **Forgiving.** A bundle that references a missing or unavailable
  skill loads the rest and logs the skips; no hard failure. Bundles
  are user-authored, skill availability changes (uninstall, missing
  ``bins``), and a half-broken bundle is more useful than no bundle.
- **mtime-based cache.** Bundle files change rarely but get scanned
  on every user turn for slash command resolution. A directory +
  per-file mtime fingerprint avoids re-parsing YAML on every keystroke
  without giving up "edit the file and try again" iteration.

Adapted to Flowly's ``SkillsLoader`` API + ``~/.flowly`` directory
layout from an earlier reference implementation.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from flowly.agent.slash_commands import gateway_commands as _gateway_commands

# Max skills surfaced in the slash menu before overflow is hidden behind
# `/skills`. Keeps a big skills dir from burying the real commands.
_MAX_SKILL_COMMANDS = 30

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------- #


def get_bundles_dir() -> Path:
    """Resolve the active profile's skill-bundles directory.

    Lazy import of ``get_flowly_home`` keeps this module importable
    from CLI subcommands that run before the profile is fully wired.
    """
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "skill-bundles"


def _default_workspace() -> Path:
    """Default workspace used when no live AgentLoop workspace is available."""
    from flowly.profile import get_flowly_home
    return get_flowly_home() / "workspace"


def scan_bundles() -> dict[str, dict[str, Any]]:
    """Return the bundle registry, refreshing the cache when files change.

    Keyed by canonical slug (``/-prefixed``) so the slash command
    dispatcher can do an O(1) ``key in registry`` lookup.
    """
    return _CACHE.get_or_refresh()


def get_bundle(slug: str) -> dict[str, Any] | None:
    """Look up a single bundle by slug. Accepts ``/research`` or ``research``."""
    key = _canonical_key(slug)
    return scan_bundles().get(key)


def is_bundle_command(text: str) -> bool:
    """Quick check before we bother with full resolution."""
    if not text or not text.lstrip().startswith("/"):
        return False
    first, _ = _split_first_word(text)
    return first in scan_bundles()


def maybe_expand(message: str, *, workspace: Path | None = None) -> str:
    """Expand a leading ``/bundle`` or ``/skill`` into the loaded prompt.

    Bundles take precedence over individual skills. Returns the message
    unchanged when the leading token isn't a known bundle or skill.
    Caller can route the return value straight into the agent without
    an existence check — the no-op path is the common case.
    """
    if not message or not message.lstrip().startswith("/"):
        return message
    first, rest = _split_first_word(message)
    bundle = scan_bundles().get(first)
    if not bundle:
        return maybe_expand_skill(message, workspace=workspace)
    return _build_invocation_message(bundle, user_instruction=rest)


def scan_skill_commands(*, workspace: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return individual skill slash commands keyed by canonical ``/slug``.

    Skills are profile/workspace markdown files discovered through
    ``SkillsLoader``. The command key is the slugified skill directory
    name so ``3-statement-model`` becomes ``/3-statement-model``.
    """
    from flowly.agent.skills import SkillsLoader

    loader = SkillsLoader(workspace=workspace or _default_workspace())
    commands: dict[str, dict[str, Any]] = {}
    for skill in loader.list_skills(filter_unavailable=False):
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        key = _canonical_key(name)
        if not key or key in commands:
            continue
        commands[key] = {
            "name": name,
            "description": _skill_description(loader, name),
            "path": str(skill.get("path") or ""),
            "source": str(skill.get("source") or ""),
        }
    return commands


def maybe_expand_skill(message: str, *, workspace: Path | None = None) -> str:
    """Expand a leading ``/skill`` into a one-turn skill invocation payload."""
    if not message or not message.lstrip().startswith("/"):
        return message
    first, rest = _split_first_word(message)
    if not first:
        return message

    name = first.lstrip("/")
    if _is_reserved_command(name):
        return message

    commands = scan_skill_commands(workspace=workspace)
    skill = commands.get(first)
    if not skill:
        return message
    expanded = build_skill_invocation_message(skill, user_instruction=rest, workspace=workspace)
    return expanded or message


def build_skill_invocation_message(
    skill: dict[str, Any],
    *,
    user_instruction: str = "",
    workspace: Path | None = None,
) -> str | None:
    """Build the user-message payload for a single skill invocation."""
    from flowly.agent.skills import SkillsLoader

    name = str(skill.get("name") or "").strip()
    path = Path(str(skill.get("path") or ""))
    if not name or not path.exists():
        return None

    loader = SkillsLoader(workspace=workspace or _default_workspace())
    try:
        raw_content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[skills] cannot read skill %s: %s", name, exc)
        return None

    content = loader._strip_frontmatter(raw_content).strip()
    skill_dir = path.parent
    parts = [
        (
            f'[IMPORTANT: The user has invoked the "{name}" skill, indicating they want '
            "you to follow its instructions. The full skill content is loaded below.]"
        ),
        "",
        content,
        "",
        f"[Skill directory: {skill_dir}]",
        (
            "Resolve any relative paths in this skill (for example, scripts, templates, "
            "references, or assets) against that directory."
        ),
    ]

    supporting = _discover_supporting_files(skill_dir)
    if supporting:
        parts.extend(["", "[This skill has supporting files:]"])
        for rel in supporting:
            parts.append(f"- {rel} -> {skill_dir / rel}")
        parts.append(
            f'\nLoad these with skill_view(name="{name}", file_path="<path>") '
            "when you need more detail."
        )

    if user_instruction:
        parts.extend(["", f"[Task] {user_instruction}"])
    return "\n".join(part for part in parts if part is not None)


def reload() -> None:
    """Drop the in-memory cache. Next ``scan_bundles()`` re-reads disk."""
    _CACHE.invalidate()


# --------------------------------------------------------------------- #
# Slug + cache plumbing
# --------------------------------------------------------------------- #


_SLUG_INVALID = re.compile(r"[^a-z0-9-]+")
_SLUG_COLLAPSE_DASHES = re.compile(r"-{2,}")


def _slugify(name: str) -> str:
    """Normalise a bundle file stem / config name into a stable slug.

    ``Research Tools`` → ``research-tools``. Standard slug form so
    bundle filenames and config names round-trip.
    """
    lowered = (name or "").strip().lower()
    lowered = lowered.replace(" ", "-").replace("_", "-")
    lowered = _SLUG_INVALID.sub("-", lowered)
    lowered = _SLUG_COLLAPSE_DASHES.sub("-", lowered)
    return lowered.strip("-")


def _canonical_key(slug_or_command: str) -> str:
    """Map a user-typed identifier to the dictionary key.

    Accepts ``/research``, ``research``, ``/Research``, etc.
    """
    raw = (slug_or_command or "").strip()
    if raw.startswith("/"):
        raw = raw[1:]
    return "/" + _slugify(raw) if raw else ""


def _split_first_word(text: str) -> tuple[str, str]:
    """Return ``("/slug", "rest of message")``.

    The slug is *canonicalised* (lowercased, hyphenated) so the lookup
    is case-insensitive without forcing the bundle dict to store every
    spelling variant.
    """
    stripped = text.lstrip()
    leading_ws_len = len(text) - len(stripped)
    if not stripped.startswith("/"):
        return "", text
    parts = stripped.split(None, 1)
    head = parts[0]
    tail = parts[1] if len(parts) > 1 else ""
    return _canonical_key(head), tail.strip() if tail else ""


def _skill_description(loader: Any, name: str) -> str:
    """Best-effort skill description lookup."""
    try:
        desc = loader._get_skill_description(name)
    except Exception:
        desc = ""
    return str(desc or name)


def _discover_supporting_files(skill_dir: Path) -> list[str]:
    """Return skill-local supporting files worth surfacing in the prompt."""
    supporting: list[str] = []
    for subdir in ("references", "templates", "scripts", "assets"):
        root = skill_dir / subdir
        if not root.exists():
            continue
        try:
            files = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
        except OSError:
            continue
        for path in files:
            try:
                supporting.append(str(path.relative_to(skill_dir)))
            except ValueError:
                continue
    return supporting


def _is_reserved_command(name: str) -> bool:
    """Return True when a slash token belongs to built-ins/plugins/bundles."""
    slug = _slugify(name)
    if not slug:
        return True
    builtin_names = {_slugify(cmd["name"]) for cmd in BUILTIN_SLASH_COMMANDS}
    if slug in builtin_names:
        return True
    if f"/{slug}" in scan_bundles():
        return True
    try:
        from flowly.plugins import get_plugin_manager
        mgr = get_plugin_manager()
        if mgr.get_slash_handler(slug) is not None:
            return True
    except Exception:
        pass
    return False


class _BundleCache:
    """mtime-keyed cache with thread-safe refresh.

    A bundle directory change (any file mtime, plus the directory's
    own mtime to catch creates/deletes) flips the fingerprint and
    triggers a full re-scan. Reads after that point share the new
    dict; older readers either see the stale dict (acceptable —
    bundles are user-controlled, not security-sensitive) or block on
    the lock momentarily.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._fingerprint: tuple[float, ...] | None = None
        self._cache: dict[str, dict[str, Any]] = {}

    def get_or_refresh(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            current = self._compute_fingerprint()
            if current != self._fingerprint:
                self._cache = self._scan()
                self._fingerprint = current
            return self._cache

    def invalidate(self) -> None:
        with self._lock:
            self._fingerprint = None
            self._cache = {}

    @staticmethod
    def _compute_fingerprint() -> tuple[float, ...]:
        bundles_dir = get_bundles_dir()
        if not bundles_dir.exists():
            return (0.0,)
        try:
            entries: list[float] = [bundles_dir.stat().st_mtime]
            for p in sorted(bundles_dir.glob("*.yaml")):
                try:
                    entries.append(p.stat().st_mtime)
                except OSError:
                    continue
            return tuple(entries)
        except OSError:
            return (0.0,)

    @staticmethod
    def _scan() -> dict[str, dict[str, Any]]:
        bundles_dir = get_bundles_dir()
        if not bundles_dir.exists():
            return {}
        out: dict[str, dict[str, Any]] = {}
        for path in sorted(bundles_dir.glob("*.yaml")):
            bundle = _load_bundle_file(path)
            if bundle is None:
                continue
            key = _canonical_key(bundle["slug"])
            if not key:
                logger.warning("[bundles] dropping bundle with empty slug: %s", path.name)
                continue
            if key in out:
                logger.warning(
                    "[bundles] duplicate slug %s from %s; keeping %s",
                    key, path.name, out[key]["path"],
                )
                continue
            out[key] = bundle
        return out


_CACHE = _BundleCache()


# --------------------------------------------------------------------- #
# File parsing
# --------------------------------------------------------------------- #


def _load_bundle_file(path: Path) -> dict[str, Any] | None:
    """Parse one ``*.yaml`` file into a bundle dict, or ``None`` on error.

    Failures (missing skills key, malformed YAML, empty file) get logged
    as WARN and the file is skipped — bundles are user-edited and we'd
    rather drop one bad file than crash the agent loop.
    """
    try:
        import yaml
    except ImportError:
        logger.error("[bundles] PyYAML missing — skill bundles disabled")
        return None

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[bundles] cannot read %s: %s", path.name, exc)
        return None

    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        logger.warning("[bundles] %s has invalid YAML: %s", path.name, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("[bundles] %s top-level must be a mapping", path.name)
        return None

    skills = data.get("skills") or []
    if not isinstance(skills, list) or not skills:
        logger.warning("[bundles] %s missing or empty `skills` list", path.name)
        return None

    # Normalise: every skill entry must be a non-empty string. Drop the
    # rest with a warning rather than failing the whole file.
    clean_skills: list[str] = []
    for entry in skills:
        if isinstance(entry, str) and entry.strip():
            clean_skills.append(entry.strip())
        else:
            logger.warning("[bundles] %s has non-string skill entry: %r", path.name, entry)
    if not clean_skills:
        return None

    name_field = data.get("name")
    name = name_field.strip() if isinstance(name_field, str) and name_field.strip() else path.stem
    slug = _slugify(name)
    description = data.get("description") or ""
    instruction = data.get("instruction") or ""
    if not isinstance(description, str):
        description = ""
    if not isinstance(instruction, str):
        instruction = ""

    return {
        "name": name,
        "slug": slug,
        "description": description.strip(),
        "instruction": instruction.strip(),
        "skills": clean_skills,
        "path": str(path),
    }


# --------------------------------------------------------------------- #
# Invocation message builder
# --------------------------------------------------------------------- #


def _build_invocation_message(
    bundle: dict[str, Any],
    *,
    user_instruction: str,
) -> str:
    """Produce the message the agent will actually receive.

    Layout:
        [BUNDLE] <name> — <description>
        Loaded skills: <skill names>

        <skill 1 body>
        ---
        <skill 2 body>
        ...

        [Instruction] <bundle.instruction>
        [Task] <user_instruction>

    Missing or unavailable skills are listed in a footer note so the
    agent (and the user, looking at the trajectory) can see which
    pieces of the bundle didn't make it in.
    """
    from flowly.agent.skills import SkillsLoader

    # Bundles run outside any specific workspace context (they're a
    # profile-level construct, not project-scoped). Point the loader
    # at the default workspace so it sees default workspace skills,
    # managed profile skills, and built-ins in normal priority order.
    workspace_root = _default_workspace()
    loader = SkillsLoader(workspace=workspace_root)
    available_skills = {s["name"]: s for s in loader.list_skills(filter_unavailable=False)}

    loaded: list[tuple[str, str]] = []
    missing: list[str] = []
    seen: set[str] = set()

    for skill_name in bundle["skills"]:
        if skill_name in seen:
            continue
        seen.add(skill_name)
        info = available_skills.get(skill_name)
        if not info:
            missing.append(skill_name)
            continue
        try:
            content = Path(info["path"]).read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("[bundles] cannot read skill %s: %s", skill_name, exc)
            missing.append(skill_name)
            continue
        loaded.append((skill_name, content.strip()))

    header_lines = [f"[BUNDLE] {bundle['name']}"]
    if bundle["description"]:
        header_lines[0] += f" — {bundle['description']}"
    header_lines.append("Loaded skills: " + ", ".join(name for name, _ in loaded))

    sections = ["\n\n---\n\n".join(content for _, content in loaded)] if loaded else []

    footer_lines: list[str] = []
    if bundle["instruction"]:
        footer_lines.append(f"[Bundle instruction] {bundle['instruction']}")
    if missing:
        footer_lines.append(
            "[Note] Skills not loaded (missing or unavailable): "
            + ", ".join(missing)
        )
    if user_instruction:
        footer_lines.append(f"[Task] {user_instruction}")

    parts = ["\n".join(header_lines)]
    parts.extend(sections)
    if footer_lines:
        parts.append("\n".join(footer_lines))
    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------- #
# CRUD helpers — used by the CLI subcommand
# --------------------------------------------------------------------- #


def save_bundle(
    *,
    name: str,
    skills: list[str],
    description: str = "",
    instruction: str = "",
    overwrite: bool = False,
) -> Path:
    """Write a bundle YAML to disk and invalidate the cache.

    Returns the resulting path. Raises ``ValueError`` on empty input
    or ``FileExistsError`` when ``overwrite=False`` and the file
    already exists.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to save bundles") from exc

    slug = _slugify(name)
    if not slug:
        raise ValueError("Bundle name yields an empty slug")
    clean_skills = [s.strip() for s in skills if isinstance(s, str) and s.strip()]
    if not clean_skills:
        raise ValueError("Bundle must reference at least one skill")

    bundles_dir = get_bundles_dir()
    bundles_dir.mkdir(parents=True, exist_ok=True)
    target = bundles_dir / f"{slug}.yaml"
    if target.exists() and not overwrite:
        raise FileExistsError(target)

    payload: dict[str, Any] = {
        "name": name.strip(),
        "skills": clean_skills,
    }
    if description:
        payload["description"] = description.strip()
    if instruction:
        payload["instruction"] = instruction.strip()

    target.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _CACHE.invalidate()
    return target


def delete_bundle(slug: str) -> Path | None:
    """Remove a bundle file. Returns the deleted path or ``None``."""
    key = _canonical_key(slug)
    bundle = scan_bundles().get(key)
    if not bundle:
        return None
    path = Path(bundle["path"])
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("[bundles] cannot delete %s: %s", path, exc)
        return None
    _CACHE.invalidate()
    return path


# --------------------------------------------------------------------- #
# Slash command catalogue — shared between gateway WS handler and
# web channel relay RPC handler. Both endpoints need to return the
# exact same shape ({builtin, plugin, bundle, skill}) so the desktop /
# iOS / web composer's autocomplete dropdown speaks one protocol.
# --------------------------------------------------------------------- #


# Built-in slash commands handled directly in ``agent/loop.py``. Each
# entry mirrors the name + description we show in ``/help`` so the
# composer dropdown stays in lockstep with the agent's own
# introspection — adding a new built-in here is a two-line change
# (this list + the handler).
# Gateway-facing builtins, derived from the single command registry (the
# commands NOT flagged cli_only). This is the discovery list remote clients
# (desktop, iOS) get via commands.list — see flowly/agent/slash_commands.py.
BUILTIN_SLASH_COMMANDS: list[dict[str, str]] = [
    {"name": _c.name, "description": _c.description}
    for _c in _gateway_commands()
]


def build_commands_catalogue() -> dict[str, list[dict[str, Any]]]:
    """Return the slash command catalogue grouped by command source.

    Built-ins are always present. Plugins, bundles, and skills are best-effort
    — if the plugin manager isn't wired yet or the bundles directory
    doesn't exist, those categories come back empty rather than the whole call
    failing. The caller wraps this in the appropriate RPC envelope.
    """
    plugin_cmds: list[dict[str, Any]] = []
    try:
        from flowly.plugins import get_plugin_manager
        mgr = get_plugin_manager()
        for name, entry in sorted((mgr._slash_commands or {}).items()):
            plugin_cmds.append({
                "name": str(name),
                "description": str(entry.get("description") or ""),
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug("commands.list plugin enumeration skipped: %s", exc)

    bundle_cmds: list[dict[str, Any]] = []
    try:
        for key, bundle in sorted(scan_bundles().items()):
            slug = key.lstrip("/")
            bundle_cmds.append({
                "name": slug,
                "description": str(bundle.get("description") or ""),
                "skill_count": len(bundle.get("skills") or []),
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug("commands.list bundle enumeration skipped: %s", exc)

    skill_cmds: list[dict[str, Any]] = []
    reserved = {
        _slugify(entry["name"])
        for entry in BUILTIN_SLASH_COMMANDS
    }
    reserved.update(_slugify(entry["name"]) for entry in plugin_cmds)
    reserved.update(_slugify(entry["name"]) for entry in bundle_cmds)
    try:
        for key, skill in sorted(scan_skill_commands().items()):
            slug = key.lstrip("/")
            if _slugify(slug) in reserved:
                continue
            skill_cmds.append({
                "name": slug,
                "display_name": str(skill.get("name") or slug),
                "description": str(skill.get("description") or ""),
                "source": str(skill.get("source") or ""),
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug("commands.list skill enumeration skipped: %s", exc)

    # Cap how many skills flood the slash menu: plugins/bundles
    # are never trimmed, but a large skills dir would otherwise bury the real
    # commands. The overflow stays reachable via `/skills`; clients can show
    # ``skill_hidden`` as a "+N more" hint.
    skill_hidden = max(0, len(skill_cmds) - _MAX_SKILL_COMMANDS)
    skill_cmds = skill_cmds[:_MAX_SKILL_COMMANDS]

    return {
        "builtin": list(BUILTIN_SLASH_COMMANDS),
        "plugin": plugin_cmds,
        "bundle": bundle_cmds,
        "skill": skill_cmds,
        "skill_hidden": skill_hidden,
    }
