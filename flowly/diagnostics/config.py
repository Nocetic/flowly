"""Read and validate Flowly configuration without recovery or writes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from flowly.config.loader import camel_to_snake, convert_keys
from flowly.config.schema import Config


class _ObjectPairs(list[tuple[str, Any]]):
    """Marker returned by ``json.loads`` so exact duplicate keys survive."""


@dataclass(frozen=True)
class ConfigSnapshot:
    raw: dict[str, Any] | None
    config: Config | None
    error: str = ""
    duplicates: tuple[str, ...] = ()


def read_config_snapshot(path: Path) -> ConfigSnapshot:
    """Parse and validate *path* without calling the self-healing loader."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ConfigSnapshot(None, None, "file not found")
    except (OSError, UnicodeError) as exc:
        return ConfigSnapshot(None, None, f"{type(exc).__name__}: unable to read config")

    try:
        paired = json.loads(text, object_pairs_hook=_ObjectPairs)
    except (json.JSONDecodeError, ValueError) as exc:
        return ConfigSnapshot(None, None, f"invalid JSON at line {getattr(exc, 'lineno', '?')}")

    duplicates: list[str] = []
    raw = _materialize_object(paired, model_type=Config, duplicates=duplicates)
    if not isinstance(raw, dict):
        return ConfigSnapshot(
            None,
            None,
            "config root must be a JSON object",
            tuple(duplicates),
        )

    try:
        config = Config.model_validate(convert_keys(raw))
    except ValidationError as exc:
        errors = [
            {
                "loc": ".".join(str(part) for part in item.get("loc", ())),
                "type": item.get("type", "validation_error"),
            }
            for item in exc.errors(include_url=False, include_input=False)
        ]
        return ConfigSnapshot(
            raw,
            None,
            "schema validation failed: " + json.dumps(errors, ensure_ascii=False),
            tuple(duplicates),
        )
    except Exception as exc:  # Validators may raise custom exception types.
        return ConfigSnapshot(
            raw,
            None,
            f"schema validation failed ({type(exc).__name__})",
            tuple(duplicates),
        )
    return ConfigSnapshot(raw, config, duplicates=tuple(duplicates))


def _materialize_object(
    value: Any,
    *,
    model_type: type[BaseModel] | None = None,
    mapping_item_model: type[BaseModel] | None = None,
    duplicates: list[str],
    path: str = "",
) -> Any:
    """Materialize retained JSON pairs using schema-aware collision rules.

    Exact duplicate JSON keys always collide. camelCase/snake_case aliases
    collide only when both names address a declared Pydantic field. Dynamic
    map keys (MCP server names, agent ids) and opaque maps (env, headers) are
    deliberately case-sensitive and never normalized.
    """
    if isinstance(value, _ObjectPairs):
        result: dict[str, Any] = {}
        fields = _model_fields(model_type) if model_type is not None else {}
        seen: dict[tuple[str, str], str] = {}
        for key, child in value:
            key_path = f"{path}.{key}" if path else key
            field_info = fields.get(camel_to_snake(key))
            if field_info is not None:
                identity = ("field", field_info[0])
            else:
                identity = ("exact", key)
            previous = seen.get(identity)
            if previous is not None:
                duplicates.append(f"{previous} + {key_path}")
            seen[identity] = key_path

            child_model: type[BaseModel] | None = None
            child_mapping_model: type[BaseModel] | None = None
            if mapping_item_model is not None:
                child_model = mapping_item_model
            elif field_info is not None:
                kind, nested = _nested_model_type(field_info[1].annotation)
                if kind == "model":
                    child_model = nested
                elif kind == "mapping":
                    child_mapping_model = nested
                elif kind == "sequence":
                    child_model = nested

            result[key] = _materialize_object(
                child,
                model_type=child_model,
                mapping_item_model=child_mapping_model,
                duplicates=duplicates,
                path=key_path,
            )
        return result
    if isinstance(value, list):
        return [
            _materialize_object(
                item,
                model_type=model_type,
                mapping_item_model=mapping_item_model,
                duplicates=duplicates,
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    return value


def _model_fields(model_type: type[BaseModel]) -> dict[str, tuple[str, Any]]:
    fields: dict[str, tuple[str, Any]] = {}
    for name, info in model_type.model_fields.items():
        fields[camel_to_snake(name)] = (name, info)
        alias = getattr(info, "alias", None)
        if isinstance(alias, str):
            fields[camel_to_snake(alias)] = (name, info)
    return fields


def _nested_model_type(annotation: Any) -> tuple[str, type[BaseModel] | None]:
    try:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return "model", annotation
    except TypeError:
        pass

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is dict and len(args) == 2:
        _, nested = _nested_model_type(args[1])
        return "mapping", nested
    if origin in (list, tuple, set, frozenset) and args:
        _, nested = _nested_model_type(args[0])
        return "sequence", nested
    if origin in (Union, UnionType):
        for candidate in args:
            kind, nested = _nested_model_type(candidate)
            if nested is not None:
                return kind, nested
    return "", None


def find_unknown_keys(raw: dict[str, Any], model: type[BaseModel] = Config) -> list[str]:
    """Return unknown schema keys while respecting dynamic/opaque maps."""
    unknown: list[str] = []

    def walk(value: Any, model_type: type[BaseModel], path: str) -> None:
        if not isinstance(value, dict):
            return
        fields = _model_fields(model_type)
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            field_info = fields.get(camel_to_snake(key))
            if field_info is None:
                unknown.append(child_path)
                continue
            kind, nested = _nested_model_type(field_info[1].annotation)
            if nested is None:
                continue
            if kind == "model":
                walk(child, nested, child_path)
            elif kind == "mapping" and isinstance(child, dict):
                for item_key, item in child.items():
                    walk(item, nested, f"{child_path}.{item_key}")
            elif kind == "sequence" and isinstance(child, list):
                for index, item in enumerate(child):
                    walk(item, nested, f"{child_path}[{index}]")

    walk(raw, model, "")
    return unknown


def deduplicate_preserving_runtime_values(
    raw: dict[str, Any],
    model: type[BaseModel] = Config,
) -> dict[str, Any]:
    """Remove schema alias collisions while preserving opaque map keys.

    JSON and :func:`flowly.config.loader.convert_keys` both implement effective
    last-value-wins semantics. For a known Pydantic field with multiple aliases,
    retain that effective last value and emit the schema's canonical JSON alias.
    Dynamic names and free-form maps are never normalized.
    """
    from flowly.config.loader import snake_to_camel

    def walk(
        value: Any,
        *,
        model_type: type[BaseModel] | None = None,
        mapping_item_model: type[BaseModel] | None = None,
    ) -> Any:
        if isinstance(value, list):
            return [
                walk(
                    item,
                    model_type=model_type,
                    mapping_item_model=mapping_item_model,
                )
                for item in value
            ]
        if not isinstance(value, dict):
            return value

        fields = _model_fields(model_type) if model_type is not None else {}
        groups: dict[tuple[str, str], list[str]] = {}
        field_by_identity: dict[tuple[str, str], tuple[str, Any]] = {}
        for key in value:
            field_info = fields.get(camel_to_snake(key))
            if field_info is None:
                identity = ("exact", key)
            else:
                identity = ("field", field_info[0])
                field_by_identity[identity] = field_info
            groups.setdefault(identity, []).append(key)

        emitted: set[tuple[str, str]] = set()
        result: dict[str, Any] = {}
        for key in value:
            field_info = fields.get(camel_to_snake(key))
            identity = ("field", field_info[0]) if field_info is not None else ("exact", key)
            if identity in emitted:
                continue
            emitted.add(identity)
            aliases = groups[identity]
            effective_key = aliases[-1]
            declared = field_by_identity.get(identity)

            child_model: type[BaseModel] | None = None
            child_mapping_model: type[BaseModel] | None = None
            if mapping_item_model is not None:
                child_model = mapping_item_model
            elif declared is not None:
                kind, nested = _nested_model_type(declared[1].annotation)
                if kind in {"model", "sequence"}:
                    child_model = nested
                elif kind == "mapping":
                    child_mapping_model = nested

            if len(aliases) > 1 and declared is not None:
                alias = getattr(declared[1], "alias", None)
                output_key = alias if isinstance(alias, str) else snake_to_camel(declared[0])
            else:
                output_key = effective_key
            result[output_key] = walk(
                value[effective_key],
                model_type=child_model,
                mapping_item_model=child_mapping_model,
            )
        return result

    return walk(raw, model_type=model)
