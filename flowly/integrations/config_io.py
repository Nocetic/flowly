"""Read & write integration config sections to ``~/.flowly/config.json``.

We deliberately bypass the Pydantic write path for two reasons:

1. **Partial saves.** A user editing only Telegram shouldn't accidentally
   re-emit every other config section (and overwrite manual edits to
   plugin sections we don't model). The Pydantic ``save_config`` does
   round-trip merge, but it always serialises the *full* validated tree;
   we want surgical updates.

2. **Type tolerance.** A half-filled form (e.g. token without enabling
   the channel yet) shouldn't fail Pydantic validation — we let the user
   save in-progress state and rely on the runtime channel/tool code to
   gate on its own ``enabled`` checks.

The atomic-write strategy (temp file + ``os.replace``) and 0600 perms
mirror :mod:`flowly.config.loader` so a partial integration save can
never corrupt the config — the backup at ``config.json.bak`` plus the
self-heal in ``load_config()`` continues to apply.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from flowly.config.loader import (
    convert_keys,
    convert_to_camel,
    get_config_path,
)
from flowly.integrations.cards import Field, FieldType, IntegrationCard

logger = logging.getLogger(__name__)


# ── reading ────────────────────────────────────────────────────────


def read_card_values(card: IntegrationCard) -> dict[str, Any]:
    """Pull current values for every field on ``card`` from config.json.

    Returns a snake_case dict keyed by field.key. Missing keys fall back to
    the field's ``default`` (or a type-appropriate empty value). On-disk
    keys are camelCase (matching ``save_config``), so each field.key is
    converted via :func:`snake_to_camel` before lookup — and we also fall
    back to the raw snake_case key for round-trip resilience with older
    configs that may have been hand-edited.
    """
    from flowly.config.loader import snake_to_camel
    raw = _load_raw()
    section = _descend(raw, card.config_path) or {}
    out: dict[str, Any] = {}
    for f in card.fields:
        camel = snake_to_camel(f.key)
        if camel in section:
            v = section[camel]
        elif f.key in section:
            v = section[f.key]
        else:
            v = _empty_for(f)
        out[f.key] = _coerce_in(f, v)
    return out


# ── writing ────────────────────────────────────────────────────────


class CardValidationError(ValueError):
    """A card's values would make config.json fail schema validation.

    Raised before persisting so a bad value (e.g. an out-of-range SELECT that
    maps to a ``Literal`` schema field) is rejected with a clear message instead
    of being written verbatim — which would make the WHOLE config fail to load
    at next boot, silently reverting to ``.bak`` and losing the user's save."""


def _assert_config_valid(raw: dict[str, Any]) -> None:
    """Validate ``raw`` through the SAME path :func:`load_config` uses at boot.

    Because the check is identical to boot validation, it accepts exactly what
    boot accepts (a free ``str`` SELECT like ``fal_image.model`` passes; only a
    constrained ``Literal`` field rejects an off-list value) — no over-rejection.
    """
    from flowly.config.loader import convert_keys
    from flowly.config.schema import Config

    try:
        Config.model_validate(convert_keys(raw))
    except Exception as exc:  # pydantic ValidationError (a ValueError) or convert error
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                first = errors()[0]
                loc = ".".join(str(p) for p in first.get("loc", ())) or "config"
                raise CardValidationError(f"{loc}: {first.get('msg', 'invalid value')}") from exc
            except (IndexError, TypeError, KeyError):
                pass
        raise CardValidationError(str(exc)) from exc


def apply_card_values(card: IntegrationCard, values: dict[str, Any]) -> None:
    """Persist ``values`` (snake_case) into config.json at ``card.config_path``.

    - Existing fields outside the card are preserved (deep merge).
    - Snake_case → camelCase conversion happens at write time so the file
      stays consistent with the rest of the config.
    - The merged result is validated against the real schema BEFORE writing;
      a value that wouldn't load is rejected (``CardValidationError``) rather
      than corrupting config.json.
    - The previous parseable config.json is copied to ``config.json.bak``
      before write so :func:`flowly.config.loader.load_config` can recover
      from any post-write corruption.
    - File is written atomically (temp + rename) with mode 0600.
    """
    path = get_config_path()
    raw = _load_raw_or_empty(path)

    section_camel = convert_to_camel({f.key: _coerce_out(f, values.get(f.key)) for f in card.fields})
    _set_path(raw, card.config_path, section_camel, merge=True)

    _assert_config_valid(raw)  # reject a value that would brick config.json at boot
    _atomic_write_json(path, raw)


def clear_card(card: IntegrationCard) -> None:
    """Reset every field on ``card`` to its empty default.

    Used by the modal's "Disconnect" action. Keeps the section dict but
    nukes its contents — channels/tools then see ``enabled=False`` (or
    empty credentials) and skip themselves on next gateway boot.
    """
    cleared = {f.key: _empty_for(f) for f in card.fields}
    apply_card_values(card, cleared)


# ── helpers ────────────────────────────────────────────────────────


def _load_raw() -> dict[str, Any]:
    """Best-effort read of config.json. Empty dict on any failure."""
    return _load_raw_or_empty(get_config_path())


def _load_raw_or_empty(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _descend(raw: dict[str, Any], dotted_snake: str) -> dict[str, Any] | None:
    """Walk into ``raw`` along a dotted snake_case path.

    On-disk keys are camelCase, so we convert each segment to its camelCase
    equivalent before descending. Returns the leaf dict, or ``None`` if any
    segment is missing or not a dict.
    """
    from flowly.config.loader import snake_to_camel
    node: Any = raw
    for seg in dotted_snake.split("."):
        camel = snake_to_camel(seg)
        if not isinstance(node, dict):
            return None
        node = node.get(camel)
        if node is None:
            return None
    return node if isinstance(node, dict) else None


def _set_path(
    raw: dict[str, Any],
    dotted_snake: str,
    values_camel: dict[str, Any],
    *,
    merge: bool,
) -> None:
    """Place ``values_camel`` (a dict) at the dotted snake path inside ``raw``.

    Each segment of ``dotted_snake`` is converted to camelCase before
    write. Intermediate dicts are created on demand. When ``merge=True``,
    existing keys at the leaf are preserved unless ``values_camel``
    overrides them.
    """
    from flowly.config.loader import snake_to_camel
    segs = dotted_snake.split(".")
    node: dict[str, Any] = raw
    for seg in segs[:-1]:
        camel = snake_to_camel(seg)
        sub = node.get(camel)
        if not isinstance(sub, dict):
            sub = {}
            node[camel] = sub
        node = sub
    leaf_key = snake_to_camel(segs[-1])
    if merge and isinstance(node.get(leaf_key), dict):
        merged = dict(node[leaf_key])
        merged.update(values_camel)
        node[leaf_key] = merged
    else:
        node[leaf_key] = values_camel


def _atomic_write_json(path: Path, raw: dict[str, Any]) -> None:
    """Write ``raw`` to ``path`` atomically with backup + 0600 perms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Backup current parseable file.
    if path.exists():
        try:
            shutil.copy2(str(path), str(path.with_suffix(path.suffix + ".bak")))
        except OSError as exc:
            logger.warning("integrations: backup write failed: %s", exc)
    tmp = path.with_suffix(f".tmp.{secrets.token_hex(4)}")
    try:
        tmp.write_text(json.dumps(raw, indent=4), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    try:
        from flowly.utils.file_security import secure_file
        secure_file(path)  # POSIX chmod; real owner-only ACL on Windows
    except OSError:
        pass


def _empty_for(f: Field) -> Any:
    if f.default is not None:
        return f.default
    return {
        FieldType.TEXT: "",
        FieldType.PASSWORD: "",
        FieldType.INT: 0,
        FieldType.BOOL: False,
        FieldType.SELECT: (f.choices[0][0] if f.choices else ""),
        FieldType.MULTI: [],
    }[f.type]


def _coerce_in(f: Field, value: Any) -> Any:
    """Normalize a raw on-disk value into the editor-friendly shape."""
    if value is None:
        return _empty_for(f)
    if f.type == FieldType.BOOL:
        return bool(value)
    if f.type == FieldType.INT:
        try:
            return int(value)
        except (TypeError, ValueError):
            return _empty_for(f)
    if f.type == FieldType.MULTI:
        if isinstance(value, list):
            return [str(x) for x in value]
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return []
    return str(value)


def _coerce_out(f: Field, value: Any) -> Any:
    """Normalize an editor value back to what the schema expects on disk."""
    if value is None:
        return _empty_for(f)
    if f.type == FieldType.BOOL:
        return bool(value)
    if f.type == FieldType.INT:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if f.type == FieldType.MULTI:
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return []
    return str(value)
