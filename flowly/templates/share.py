"""What an agent can share from its card, and its recipe (``agent.template.*``).

The share button in an app's agent panel asks the agent itself, never the
model: ``contents`` lists what a template of it would carry so the owner can
choose, ``export`` builds the recipe of what was chosen. The server asks for
the recipe and keeps it (flowly-server docs/cloud/design/agent-templates.md,
"Paylaşım linki"); an app never supplies one.

Memories never go from here: they go in only from the chat path, in the
owner's own words (``template_draft``).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from flowly.templates.recipe import MAX_ROUTINES, MAX_SKILLS, RecipeError

EXPORT_PARAMS = frozenset({"skills", "routines", "apps"})


def _names(params: dict, key: str, limit: int) -> list[str] | None:
    value = params.get(key)
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) > limit
            or not all(isinstance(item, str) and 0 < len(item.strip()) <= 128 for item in value)):
        raise RecipeError("BAD_RECIPE", key, f"Name {key} as a list of at most {limit} names.")
    return [item.strip() for item in value]


def _build(cron_jobs: Iterable[Any], *, skills: list[str] | None, routines: list[str] | None,
           apps: bool) -> tuple[dict, list[dict]]:
    from flowly.config.loader import load_config
    from flowly.profile import get_flowly_home
    from flowly.secrets.redact import current_redactor
    from flowly.templates.export import build_recipe

    config = load_config()
    return build_recipe(
        home=get_flowly_home(),
        workspace=config.workspace_path,
        cron_jobs=list(cron_jobs),
        mcp_servers=getattr(config, "mcp_servers", None) or {},
        name=None,
        description=None,
        skills=skills,
        routines=routines,
        include_apps=apps,
        memories=[],
        redactor=current_redactor(),
    )


def share_contents(cron_jobs: Iterable[Any]) -> dict:
    """Everything a template of this agent could carry, to choose from.

    A recipe that cannot be shared as it is (a key-like text in its
    instructions, too large) is not an error here: ``refused`` says what and
    where, so the app can tell the owner what to fix.
    """
    try:
        recipe, skipped = _build(cron_jobs, skills=None, routines=None, apps=True)
    except RecipeError as exc:
        return {"refused": exc.to_wire(), "skills": [], "routines": [], "apps": [], "skipped": []}
    return {
        "refused": None,
        "name": recipe["profile"]["displayName"],
        "description": recipe["profile"]["description"],
        "skills": [{"slug": s["slug"], "name": s["name"], "description": s["description"]} for s in recipe["skills"]],
        "routines": [{"name": r["name"], "description": r["description"], **({"schedule": r["schedule"]} if r.get("schedule") else {})}
                     for r in recipe["routines"]],
        "apps": [{"id": a["id"], "name": a["name"]} for a in recipe["apps"]],
        "skipped": skipped,
    }


def share_export(params: dict, cron_jobs: Iterable[Any]) -> dict:
    """The recipe of what the owner chose: ``{recipe, skipped}``, or ``RecipeError``.

    ``skills`` and ``routines`` name what to keep (all when omitted, ``[]`` for
    none); ``apps`` says whether the names of its apps go (default yes).
    """
    if not isinstance(params, dict) or set(params) - EXPORT_PARAMS:
        raise RecipeError("BAD_RECIPE", "", "Only skills, routines and apps can be chosen.")
    apps = params.get("apps", True)
    if not isinstance(apps, bool):
        raise RecipeError("BAD_RECIPE", "apps", "apps is true or false.")
    recipe, skipped = _build(
        cron_jobs,
        skills=_names(params, "skills", MAX_SKILLS * 4),
        routines=_names(params, "routines", MAX_ROUTINES * 4),
        apps=apps,
    )
    return {"recipe": recipe, "skipped": skipped}
