"""Validate the server's wire schema without retrieving remote references."""

from jsonschema.validators import Draft202012Validator, validator_for
from referencing import Registry


def input_validator(schema):
    try:
        schema = schema if schema is not None else {"type": "object"}
        cls = (
            validator_for(schema, default=None)
            if isinstance(schema, dict) and "$schema" in schema
            else Draft202012Validator
        )
        if cls is None:
            return None
        cls.check_schema(schema)
        # Registry's default retriever rejects non-local references. Validation
        # must never turn an untrusted schema into network or filesystem access.
        return cls(schema, registry=Registry())
    except Exception:
        return None


def argument_issue(validator, arguments: dict) -> tuple[str, str] | None:
    if validator is None:
        return (
            "INVALID_SCHEMA",
            "Cannot validate this MCP tool's input schema. Refresh its tool definition before calling it.",
        )
    try:
        error = next(validator.iter_errors(arguments), None)
        if error is None:
            return None
        # ValidationError.message can contain entire argument values (including
        # credentials). Report only schema-owned locations and missing names.
        location = "/" + "/".join(str(part) for part in error.absolute_schema_path)
        detail = f"schema rule '{error.validator}' at {location}"
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = [key for key in error.validator_value if key not in error.instance]
            detail += "; missing: " + ", ".join(missing[:8])
        return "INVALID_ARGUMENTS", (
            f"Invalid MCP arguments ({detail[:400]}). Follow the tool's schema, "
            "including top-level versus nested fields. Resolve required IDs with "
            "an available, permitted lookup tool. Do not retry unchanged arguments."
        )
    except Exception:
        return (
            "INVALID_SCHEMA",
            "Cannot resolve this MCP tool's input schema locally. Refresh its tool definition before calling it.",
        )
