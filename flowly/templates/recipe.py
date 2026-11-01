"""An agent template's recipe (``v: 1``) and the one check it passes everywhere.

A recipe is what a template carries: the agent's profile, its own skills, its
routines (as suggestions), the apps it uses (by name only) and the memories
its owner chose. The server checks it with the same rules
(``server/src/templates/recipe.ts``); the shared fixtures in
``tests/fixtures/template_recipes`` keep the two in step.

Unknown fields are refused, not dropped: a newer recipe says so with ``v``.
"""
from __future__ import annotations

import json
import re
from typing import Any

from flowly.profile import PROFILE_MARK_SHAPES

RECIPE_VERSION = 1
MAX_RECIPE_BYTES = 512 * 1024
MAX_SOUL_BYTES = 64 * 1024
MAX_SKILLS = 20
MAX_SKILL_BYTES = 64 * 1024
MAX_ROUTINES = 10
MAX_ROUTINE_PROMPT = 4000
MAX_APPS = 20
MAX_MEMORIES = 50
MAX_MEMORY_CHARS = 2000
MIN_EVERY_SECONDS = 60
MAX_EVERY_SECONDS = 366 * 24 * 3600

SKILL_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
APP_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
CRON_EXPR = re.compile(r"^\S+( \S+){4}$")
MARK_TONES = frozenset({"aqua", "violet", "rose", "amber", "lime", "sky", "slate"})
MARK_COLOR = re.compile(r"^#[0-9a-f]{6}$")

_TOP = ("v", "profile", "skills", "routines", "apps", "memories")
_PROFILE = ("displayName", "description", "soul", "markShape", "markTone")
_SKILL = ("slug", "name", "description", "content")
_ROUTINE = ("name", "description", "prompt", "schedule")
_APP = ("id", "name")
_MEMORY = ("content",)


class RecipeError(ValueError):
    """A recipe refused: ``code`` is ``BAD_RECIPE`` or ``TOO_LARGE``; ``field`` names where."""

    def __init__(self, code: str, field: str, message: str):
        super().__init__(message)
        self.code = code
        self.field = field

    def to_wire(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": str(self)}


def _bad(field: str, message: str) -> RecipeError:
    return RecipeError("BAD_RECIPE", field, message)


def recipe_bytes(recipe: Any) -> bytes:
    """The recipe as stored and measured: compact, key-sorted UTF-8 JSON."""
    return json.dumps(recipe, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _object(value: Any, field: str, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        raise _bad(field, f"{field or 'recipe'} must be an object.")
    for key in value:
        if key not in keys:
            path = f"{field}.{key}" if field else str(key)
            raise _bad(path, f"Unknown field: {path}.")
    return value


def _list(value: Any, field: str, limit: int) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _bad(field, f"{field} must be a list.")
    if len(value) > limit:
        raise _bad(field, f"At most {limit} {field}.")
    return value


def _text(value: Any, field: str, *, required: bool, max_chars: int | None = None,
          max_bytes: int | None = None) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise _bad(field, f"{field} must be text.")
    text = value.strip()
    if required and not text:
        raise _bad(field, f"{field} is required.")
    if "\x00" in text:
        raise _bad(field, f"{field} cannot contain null bytes.")
    if max_chars is not None and len(text) > max_chars:
        raise _bad(field, f"{field} must be at most {max_chars} characters.")
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise _bad(field, f"{field} must be at most {max_bytes // 1024} KiB.")
    return text


def _profile(value: Any) -> dict:
    raw = _object(value, "profile", _PROFILE)
    shape = _text(raw.get("markShape"), "profile.markShape", required=False).lower()
    if shape and shape not in PROFILE_MARK_SHAPES:
        raise _bad("profile.markShape", f"Unknown avatar shape: {shape}.")
    tone = _text(raw.get("markTone"), "profile.markTone", required=False).lower()
    if tone and tone not in MARK_TONES and not MARK_COLOR.fullmatch(tone):
        raise _bad("profile.markTone", f"Unknown avatar color: {tone}.")
    return {
        "displayName": _text(raw.get("displayName"), "profile.displayName", required=True, max_chars=64),
        "description": _text(raw.get("description"), "profile.description", required=False, max_chars=280),
        "soul": _text(raw.get("soul"), "profile.soul", required=False, max_bytes=MAX_SOUL_BYTES),
        "markShape": shape,
        "markTone": tone,
    }


def _skills(value: Any) -> list[dict]:
    skills, seen = [], set()
    for i, item in enumerate(_list(value, "skills", MAX_SKILLS)):
        field = f"skills[{i}]"
        raw = _object(item, field, _SKILL)
        slug = raw.get("slug")
        if not isinstance(slug, str) or not SKILL_SLUG.fullmatch(slug):
            raise _bad(f"{field}.slug", "A skill's slug is lowercase letters, digits and dashes.")
        if slug in seen:
            raise _bad(f"{field}.slug", f"Two skills are named {slug}.")
        seen.add(slug)
        skills.append({
            "slug": slug,
            "name": _text(raw.get("name"), f"{field}.name", required=True, max_chars=64),
            "description": _text(raw.get("description"), f"{field}.description", required=True, max_chars=1024),
            "content": _text(raw.get("content"), f"{field}.content", required=True, max_bytes=MAX_SKILL_BYTES),
        })
    return skills


def _schedule(value: Any, field: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _bad(field, "A schedule must be an object.")
    kind = value.get("kind")
    if kind == "cron":
        _object(value, field, ("kind", "expr", "tz"))
        expr = _text(value.get("expr"), f"{field}.expr", required=True, max_chars=120)
        if not CRON_EXPR.fullmatch(expr):
            raise _bad(f"{field}.expr", "A cron schedule has five fields.")
        tz = _text(value.get("tz"), f"{field}.tz", required=False, max_chars=64)
        return {"kind": "cron", "expr": expr, **({"tz": tz} if tz else {})}
    if kind == "every":
        _object(value, field, ("kind", "seconds"))
        seconds = value.get("seconds")
        if (not isinstance(seconds, int) or isinstance(seconds, bool)
                or not MIN_EVERY_SECONDS <= seconds <= MAX_EVERY_SECONDS):
            raise _bad(f"{field}.seconds", f"Every {MIN_EVERY_SECONDS} seconds at most, once a year at least.")
        return {"kind": "every", "seconds": seconds}
    raise _bad(f"{field}.kind", "A schedule is cron or every.")


def _routines(value: Any) -> list[dict]:
    routines, seen = [], set()
    for i, item in enumerate(_list(value, "routines", MAX_ROUTINES)):
        field = f"routines[{i}]"
        raw = _object(item, field, _ROUTINE)
        name = _text(raw.get("name"), f"{field}.name", required=True, max_chars=80)
        if name.casefold() in seen:
            raise _bad(f"{field}.name", f"Two routines are named {name}.")
        seen.add(name.casefold())
        routine = {
            "name": name,
            "description": _text(raw.get("description"), f"{field}.description", required=False, max_chars=280),
            "prompt": _text(raw.get("prompt"), f"{field}.prompt", required=True, max_chars=MAX_ROUTINE_PROMPT),
        }
        schedule = _schedule(raw.get("schedule"), f"{field}.schedule")
        if schedule:
            routine["schedule"] = schedule
        routines.append(routine)
    return routines


def _apps(value: Any) -> list[dict]:
    apps, seen = [], set()
    for i, item in enumerate(_list(value, "apps", MAX_APPS)):
        field = f"apps[{i}]"
        raw = _object(item, field, _APP)
        app_id = raw.get("id")
        if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id):
            raise _bad(f"{field}.id", "An app is named by its id: letters, digits, dots, dashes, underscores.")
        if app_id in seen:
            raise _bad(f"{field}.id", f"{app_id} is listed twice.")
        seen.add(app_id)
        apps.append({"id": app_id, "name": _text(raw.get("name"), f"{field}.name", required=True, max_chars=64)})
    return apps


def _memories(value: Any) -> list[dict]:
    return [
        {"content": _text(_object(item, f"memories[{i}]", _MEMORY).get("content"), f"memories[{i}].content",
                          required=True, max_chars=MAX_MEMORY_CHARS)}
        for i, item in enumerate(_list(value, "memories", MAX_MEMORIES))
    ]


def validate_recipe(value: Any) -> dict:
    """The recipe, trimmed and with its lists filled, or ``RecipeError``."""
    raw = _object(value, "", _TOP)
    if raw.get("v") != RECIPE_VERSION or isinstance(raw.get("v"), bool):
        raise _bad("v", f"This Flowly reads template recipes of version {RECIPE_VERSION}.")
    recipe = {
        "v": RECIPE_VERSION,
        "profile": _profile(raw.get("profile")),
        "skills": _skills(raw.get("skills")),
        "routines": _routines(raw.get("routines")),
        "apps": _apps(raw.get("apps")),
        "memories": _memories(raw.get("memories")),
    }
    if len(recipe_bytes(recipe)) > MAX_RECIPE_BYTES:
        raise RecipeError("TOO_LARGE", "", f"A template is at most {MAX_RECIPE_BYTES // 1024} KiB.")
    return recipe


def recipe_counts(recipe: dict) -> dict[str, int]:
    return {key: len(recipe.get(key) or []) for key in ("skills", "routines", "apps", "memories")}
