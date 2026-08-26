"""Caller-argument validation against a workflow's declared input schema.

JSON Schema draft 2020-12 with ``additionalProperties: false`` required on every
registry ``input_schema`` (rule R4, enforced at load time in ``registry/loader.py`` —
this module assumes the schema it is given already satisfies R4). Errors carry a
JSON-Pointer path so a model can repair its own call without guessing (AC-04).

``REQUIRED`` and ``ADDITIONAL_PROPERTY`` errors are computed directly rather than taken
from ``jsonschema``'s own error objects: the library reports both against the *root* of
the instance (``absolute_path == []``) with the offending field name only inside the
English message text, which is not something this codebase wants to depend on parsing.
Computing them directly also produces exactly the message wording MCP_TOOLS.md section
2.4 documents. Every other validator keyword (``type``, ``enum``, ``format``,
``minLength``, ...) already reports a proper per-field path, so those are taken from
``jsonschema`` directly.

Phase 2 (BUILD_PLAN section 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

# MCP_TOOLS.md section 2.4's documented codes, plus a reasonable generic mapping for
# every other JSON Schema keyword a workflow author might use.
_VALIDATOR_CODES: dict[str, str] = {
    "type": "TYPE",
    "enum": "ENUM",
    "const": "ENUM",
    "minLength": "MIN_LENGTH",
    "maxLength": "MAX_LENGTH",
    "pattern": "PATTERN",
    "format": "FORMAT",
    "minimum": "MINIMUM",
    "maximum": "MAXIMUM",
    "exclusiveMinimum": "MINIMUM",
    "exclusiveMaximum": "MAXIMUM",
    "multipleOf": "MULTIPLE_OF",
    "minItems": "MIN_ITEMS",
    "maxItems": "MAX_ITEMS",
    "uniqueItems": "UNIQUE_ITEMS",
    "minProperties": "MIN_PROPERTIES",
    "maxProperties": "MAX_PROPERTIES",
}


@dataclass(frozen=True)
class ArgumentError:
    """One argument-validation failure — the shape MCP_TOOLS.md section 2.4 documents."""

    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


def _json_pointer(segments: list[Any]) -> str:
    """RFC 6901 JSON Pointer from a sequence of path segments, root-escaped."""
    if not segments:
        return ""
    parts = []
    for segment in segments:
        text = str(segment).replace("~", "~0").replace("/", "~1")
        parts.append(text)
    return "/" + "/".join(parts)


def validate_arguments(
    input_schema: dict[str, Any], arguments: dict[str, Any]
) -> list[ArgumentError]:
    """Validate ``arguments`` against ``input_schema``. An empty list means valid.

    ``input_schema`` is assumed to already be a valid draft 2020-12 object schema with
    ``additionalProperties: false`` (rule R4) — a workflow's registered schema is
    checked for that once, at registry-load time, not on every call.
    """
    errors: list[ArgumentError] = []

    required = input_schema.get("required", [])
    if isinstance(required, list):
        for field_name in required:
            if isinstance(field_name, str) and field_name not in arguments:
                errors.append(
                    ArgumentError(
                        path=_json_pointer([field_name]),
                        code="REQUIRED",
                        message=f"Field {field_name!r} is required.",
                    )
                )

    properties = input_schema.get("properties", {})
    known_fields = set(properties) if isinstance(properties, dict) else set()
    if input_schema.get("additionalProperties") is False and isinstance(arguments, dict):
        for field_name in arguments:
            if field_name not in known_fields:
                errors.append(
                    ArgumentError(
                        path=_json_pointer([field_name]),
                        code="ADDITIONAL_PROPERTY",
                        message=(
                            f"Unknown field {field_name!r}; this workflow accepts no extra fields."
                        ),
                    )
                )

    validator = Draft202012Validator(input_schema)
    for error in validator.iter_errors(arguments):
        if error.validator in ("required", "additionalProperties"):
            continue  # handled above, with MCP_TOOLS.md's exact documented phrasing
        code = _VALIDATOR_CODES.get(str(error.validator), str(error.validator).upper())
        errors.append(
            ArgumentError(
                path=_json_pointer(list(error.absolute_path)),
                code=code,
                message=error.message,
            )
        )

    return errors


__all__ = ["ArgumentError", "validate_arguments"]
