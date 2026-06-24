"""Pet feature service — ties config + store + manifest + sprites together.

Backs the ``pet.*`` feature RPCs. Functions raise :class:`PetServiceError`
(``code`` + ``message``) on user-facing failures; the RPC layer maps that onto
its own error envelope. A failed download/select never disturbs the currently
active pet — config is only mutated after a successful install.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from flowly.config.loader import load_config, save_config
from flowly.pet import constants, manifest, sprites, store

_MIME_BY_EXT = {".webp": "image/webp", ".png": "image/png", ".gif": "image/gif"}


class PetServiceError(Exception):
    """User-facing pet failure with a structured ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _safe(slug: Any) -> str:
    try:
        return store.safe_slug(slug or "")
    except store.PetStoreError:
        return ""


def _spritesheet_path(slug: str) -> Path | None:
    d = store.pet_dir(slug)
    if not d.is_dir():
        return None
    for ext in _MIME_BY_EXT:
        p = d / f"spritesheet{ext}"
        if p.is_file():
            return p
    return None


def _ext_from_url(url: str, default: str = ".webp") -> str:
    head = url.split("?", 1)[0]
    if "." in head:
        ext = "." + head.rsplit(".", 1)[1].lower()
        if ext in _MIME_BY_EXT:
            return ext
    return default


# ── reads ────────────────────────────────────────────────────────────────────

def get_info() -> dict:
    """Active-pet payload for the renderer, or ``{enabled: False}``."""
    cfg = load_config()
    pet = cfg.display.pet
    if not pet.enabled or not pet.slug:
        return {"enabled": False}
    slug = _safe(pet.slug)
    meta = store.read_meta(slug) if slug else None
    sheet = _spritesheet_path(slug) if slug else None
    if not slug or meta is None or sheet is None:
        # Active pet isn't actually installed — report not-enabled, flagged.
        return {"enabled": False, "slug": pet.slug, "missing": True}
    raw = sheet.read_bytes()
    return {
        "enabled": True,
        "slug": slug,
        "scale": pet.scale,
        "name": meta.get("name") or slug,
        "loopMs": int(meta.get("loopMs") or constants.DEFAULT_LOOP_MS),
        "frameWidth": constants.FRAME_WIDTH,
        "frameHeight": constants.FRAME_HEIGHT,
        "rowByState": meta.get("rowByState") or {},
        "framesByState": meta.get("framesByState") or {},
        "spritesheet": base64.b64encode(raw).decode("ascii"),
        "spritesheetMime": _MIME_BY_EXT.get(sheet.suffix.lower(), "image/webp"),
    }


async def get_gallery(*, client: Any = None) -> dict:
    """Manifest pets ∪ installed pets, annotated with installed/active flags.
    Falls back to installed-only when the manifest is unreachable."""
    cfg = load_config()
    pet = cfg.display.pet
    active = _safe(pet.slug) if (pet.enabled and pet.slug) else ""
    installed = set(store.list_installed())

    offline = False
    try:
        data = await manifest.fetch_manifest(client=client)
        entries = manifest.pets_from_manifest(data)
    except manifest.PetManifestError:
        offline = True
        entries = [{"slug": s, **(store.read_meta(s) or {})} for s in sorted(installed)]

    pets: list[dict] = []
    seen: set[str] = set()
    for e in entries:
        slug = _safe(e.get("slug"))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        pets.append({
            "slug": slug,
            "name": e.get("name") or slug,
            "installed": slug in installed,
            "active": slug == active,
        })
    return {"pets": pets, "active": active, "enabled": bool(pet.enabled), "offline": offline}


# ── writes ───────────────────────────────────────────────────────────────────

async def _install(slug: str, *, client: Any = None) -> None:
    try:
        data = await manifest.fetch_manifest(client=client)
    except manifest.PetManifestError as exc:
        raise PetServiceError("MANIFEST_UNAVAILABLE", str(exc)) from exc

    entry = next((e for e in manifest.pets_from_manifest(data) if _safe(e.get("slug")) == slug), None)
    if entry is None:
        raise PetServiceError("NOT_FOUND", f"pet not in manifest: {slug}")
    url = entry.get("spritesheet") or entry.get("spritesheetUrl")
    if not url:
        raise PetServiceError("INVALID", f"manifest entry has no spritesheet: {slug}")

    dest = store.pet_dir(slug) / f"spritesheet{_ext_from_url(url)}"
    try:
        await store.download_asset(url, dest, client=client)
    except store.PetStoreError as exc:
        raise PetServiceError("DOWNLOAD_FAILED", str(exc)) from exc

    states = entry.get("states") if isinstance(entry.get("states"), list) else list(constants.PET_STATES)
    row_by, frames_by = sprites.analyze(sprites.load_image(dest), states)
    store.write_meta(slug, {
        "slug": slug,
        "name": entry.get("name") or slug,
        "loopMs": int(entry.get("loopMs") or constants.DEFAULT_LOOP_MS),
        "spritesheet": dest.name,
        "rowByState": row_by,
        "framesByState": frames_by,
        "thumbUrl": entry.get("thumb") or entry.get("thumbnail") or "",
    })


async def select(slug: str, *, client: Any = None) -> dict:
    """Adopt a pet: install it if needed, then make it the active enabled pet.
    On any install failure the current active pet is left untouched."""
    safe = _safe(slug)
    if not safe:
        raise PetServiceError("INVALID", f"invalid pet slug: {slug!r}")
    if store.read_meta(safe) is None or _spritesheet_path(safe) is None:
        await _install(safe, client=client)  # raises on failure — config untouched
    cfg = load_config()
    cfg.display.pet.slug = safe
    cfg.display.pet.enabled = True
    save_config(cfg)
    return get_info()


def disable() -> dict:
    cfg = load_config()
    cfg.display.pet.enabled = False
    save_config(cfg)
    return {"enabled": False}


def set_scale(scale: Any) -> dict:
    try:
        clamped = constants.clamp_scale(scale)
    except (TypeError, ValueError) as exc:
        raise PetServiceError("INVALID", "scale must be a number") from exc
    cfg = load_config()
    cfg.display.pet.scale = clamped
    save_config(cfg)
    return {"ok": True, "scale": clamped}
