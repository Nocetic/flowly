"""An agent's recipe, built from what is its own (``template_draft``).

What goes in: the profile (name, description, avatar), ``SOUL.md``, the skills
in the agent's own workspace (never the bundled or hub ones), its routines
that run a turn (as suggestions: prompt and schedule), the names of its MCP
servers (never their addresses, headers, env or keys) and the memories its
owner named.

Nothing that looks like a secret leaves: a saved value of the agent's vault,
in any form the redactor knows, or a key-shaped string refuses the draft
(``CONTAINS_SECRET``) instead of being cut out silently, so the owner sees
what to remove.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from flowly.secrets.redact import Redactor
from flowly.templates.recipe import (
    APP_ID,
    MAX_SKILL_BYTES,
    RECIPE_VERSION,
    RecipeError,
    validate_recipe,
)

_KEY_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def split_skill(text: str) -> tuple[dict[str, str], str]:
    """A ``SKILL.md``'s ``key: value`` frontmatter and its body (as the skill loader reads it)."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, text[match.end():].strip()


def slugify(name: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")[:64]


def _profile(home: Path, workspace: Path, name: str | None, description: str | None) -> dict:
    from flowly.profile import _profile_metadata

    meta = _profile_metadata(home)
    soul_path = workspace / "SOUL.md"
    soul = soul_path.read_text(encoding="utf-8") if soul_path.is_file() else ""
    return {
        "displayName": (name or "").strip() or str(meta.get("displayName") or ""),
        "description": (description or "").strip() or str(meta.get("description") or ""),
        "soul": soul,
        "markShape": str(meta.get("markShape") or ""),
        "markTone": str(meta.get("markTone") or ""),
    }


def _skills(workspace: Path, chosen: list[str] | None, skipped: list[dict]) -> list[dict]:
    root = workspace / "skills"
    found: dict[str, dict] = {}
    for path in sorted(root.glob("*/SKILL.md")) if root.is_dir() else []:
        folder = path.parent.name
        slug = slugify(folder)
        if not slug:
            skipped.append({"kind": "skill", "name": folder, "reason": "BAD_NAME"})
            continue
        base, n = slug, 2
        while slug in found:
            slug, n = f"{base}-{n}", n + 1
        meta, body = split_skill(path.read_text(encoding="utf-8", errors="replace"))
        if not body:
            continue
        if len(body.encode("utf-8")) > MAX_SKILL_BYTES:
            skipped.append({"kind": "skill", "name": folder, "reason": "TOO_LARGE"})
            continue
        first_line = body.splitlines()[0].lstrip("# ").strip()
        found[slug] = {
            "slug": slug,
            "name": (meta.get("name") or folder)[:64],
            "description": (meta.get("description") or first_line or folder)[:1024],
            "content": body,
        }
    if chosen is None:
        return [found[slug] for slug in sorted(found)]
    missing = [slug for slug in chosen if slug not in found]
    if missing:
        raise RecipeError("BAD_RECIPE", "skills", f"No skill of this agent is named {', '.join(missing)}.")
    return [found[slug] for slug in dict.fromkeys(chosen)]


def _schedule(job: Any) -> dict | None:
    schedule = job.schedule
    if schedule.kind == "cron" and schedule.expr:
        return {"kind": "cron", "expr": schedule.expr, **({"tz": schedule.tz} if schedule.tz else {})}
    if schedule.kind == "every" and schedule.every_ms:
        return {"kind": "every", "seconds": max(60, int(schedule.every_ms // 1000))}
    return None


def _routines(jobs: Iterable[Any], chosen: list[str] | None) -> list[dict]:
    routines: dict[str, dict] = {}
    for job in jobs:
        if job.lifecycle not in ("scheduled", "paused") or job.payload.kind != "agent_turn":
            continue
        schedule = _schedule(job)
        prompt = (job.payload.message or "").strip()
        if schedule is None or not prompt or job.name.casefold() in routines:
            continue
        routines[job.name.casefold()] = {"name": job.name, "description": "", "prompt": prompt, "schedule": schedule}
    if chosen is None:
        return list(routines.values())
    missing = [name for name in chosen if name.casefold() not in routines]
    if missing:
        raise RecipeError("BAD_RECIPE", "routines", f"No routine of this agent is named {', '.join(missing)}.")
    return [routines[name.casefold()] for name in dict.fromkeys(chosen)]


def _apps(mcp_servers: Mapping[str, Any], include: bool, skipped: list[dict]) -> list[dict]:
    if not include:
        return []
    apps = []
    for key in sorted(mcp_servers):
        if APP_ID.fullmatch(key):
            apps.append({"id": key, "name": key[:64]})
        else:
            skipped.append({"kind": "app", "name": key[:64], "reason": "BAD_NAME"})
    return apps


def _strings(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, f"{path}[{i}]")


def check_secrets(recipe: dict, redactor: Redactor) -> None:
    """``RecipeError("CONTAINS_SECRET", field)`` for the first field holding a secret; never says the value."""
    for field, text in _strings(recipe):
        if redactor.redact(text) != text or any(shape.search(text) for shape in _KEY_SHAPES):
            raise RecipeError("CONTAINS_SECRET", field,
                              f"{field} holds what looks like a key or a saved secret; take it out first.")


def build_recipe(*, home: Path, workspace: Path, cron_jobs: Iterable[Any], mcp_servers: Mapping[str, Any],
                 name: str | None, description: str | None, skills: list[str] | None, routines: list[str] | None,
                 include_apps: bool, memories: list[str], redactor: Redactor) -> tuple[dict, list[dict]]:
    """The checked recipe and what was left out (``[{kind, name, reason}]``), or ``RecipeError``."""
    skipped: list[dict] = []
    recipe = validate_recipe({
        "v": RECIPE_VERSION,
        "profile": _profile(home, workspace, name, description),
        "skills": _skills(workspace, skills, skipped),
        "routines": _routines(cron_jobs, routines),
        "apps": _apps(mcp_servers, include_apps, skipped),
        "memories": [{"content": text} for text in memories],
    })
    check_secrets(recipe, redactor)
    return recipe, skipped
