"""Favourite models, one list per computer.

Starred models live in Core, so every app (Desktop, iOS, Android, through any of the computer's
agents) reads the same list rather than each device keeping its own. Like the
computer's sign-ins (``flowly/auth/shared_signin.py``) the list sits in the
main agent's home, so every named agent on the computer shares it; a cloud
computer has its own.

An entry is a provider and a model (the same model on two providers is two
favourites), in the order they were starred. An entry stays when its provider
is signed out or its catalogue no longer lists the model: it comes back when
they do (the apps show it as unavailable meanwhile).

Writes take a computer-wide lock and replace the file atomically, so two
agents (or two devices) starring at once both land. Setting is idempotent
(``on`` true or false, never a toggle), so a retried or repeated request
cannot undo itself.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

#: The most favourites kept; a star past it is refused.
MAX_FAVORITES = 200
_MAX_MODEL_CHARS = 300


class FavoritesError(ValueError):
    """A favourite that cannot be set: ``code`` is the RPC error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _path() -> Path:
    from flowly.auth.shared_signin import signin_home

    return signin_home() / "state" / "model-favorites.json"


@contextmanager
def _locked() -> Iterator[None]:
    path = _path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # no advisory locks (Windows): one writer is the norm
        yield
        return
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _valid(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    provider = entry.get("provider")
    model = entry.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    provider, model = provider.strip(), model.strip()
    if not provider or not model or len(model) > _MAX_MODEL_CHARS:
        return None
    added = entry.get("addedAt")
    return {"provider": provider, "model": model, "addedAt": added if isinstance(added, (int, float)) else 0}


def _read() -> list[dict[str, Any]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("favorites") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items if isinstance(items, list) else []:
        entry = _valid(item)
        if entry is None or (entry["provider"], entry["model"]) in seen:
            continue
        seen.add((entry["provider"], entry["model"]))
        out.append(entry)
    return out


def _write(entries: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
    try:
        tmp.write_text(json.dumps({"version": 1, "favorites": entries}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def favorites() -> list[dict[str, Any]]:
    """The favourites, in the order they were starred."""
    return _read()


def is_favorite(provider: str, model: str, entries: list[dict[str, Any]] | None = None) -> bool:
    return any(e["provider"] == provider and e["model"] == model for e in (entries if entries is not None else _read()))


def set_favorite(provider: str, model: str, on: bool) -> list[dict[str, Any]]:
    """Star (``on``) or unstar ``model`` on ``provider``; returns the list."""
    from flowly.integrations.active_provider import resolve_provider_alias, _PROVIDER_KEYS

    provider = resolve_provider_alias((provider or "").strip())
    model = (model or "").strip()
    if provider != "flowly" and provider not in _PROVIDER_KEYS:
        raise FavoritesError("INVALID", f"unknown provider: {provider or '(empty)'}")
    if not model or len(model) > _MAX_MODEL_CHARS:
        raise FavoritesError("INVALID", "model must be a non-empty string")
    with _locked():
        entries = _read()
        present = is_favorite(provider, model, entries)
        if on and not present:
            if len(entries) >= MAX_FAVORITES:
                raise FavoritesError("FAVORITES_FULL", f"At most {MAX_FAVORITES} favourite models.")
            entries.append({"provider": provider, "model": model, "addedAt": int(time.time())})
            _write(entries)
        elif not on and present:
            entries = [e for e in entries if not (e["provider"] == provider and e["model"] == model)]
            _write(entries)
        return entries
