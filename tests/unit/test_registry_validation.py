"""``registry/validation.py``: JSON Schema argument validation with JSON-Pointer paths
(BUILD_PLAN section 12, phase 2; AC-04)."""

from __future__ import annotations

import pytest

from n8n_operator.registry.validation import ArgumentError, validate_arguments

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["email", "tier"],
    "properties": {
        "email": {"type": "string", "format": "email"},
        "tier": {"type": "string", "enum": ["free", "pro", "enterprise"]},
        "notes": {"type": "string", "maxLength": 10},
        "count": {"type": "integer", "minimum": 0, "maximum": 100},
    },
}


@pytest.mark.unit
def test_valid_arguments_produce_no_errors() -> None:
    assert validate_arguments(SCHEMA, {"email": "a@b.com", "tier": "pro"}) == []


@pytest.mark.unit
def test_missing_required_field_reports_required_code_and_path() -> None:
    errors = validate_arguments(SCHEMA, {"tier": "pro"})
    assert errors == [
        ArgumentError(path="/email", code="REQUIRED", message="Field 'email' is required.")
    ]


@pytest.mark.unit
def test_multiple_missing_required_fields_each_get_their_own_error() -> None:
    errors = validate_arguments(SCHEMA, {})
    codes_by_path = {e.path: e.code for e in errors}
    assert codes_by_path["/email"] == "REQUIRED"
    assert codes_by_path["/tier"] == "REQUIRED"


@pytest.mark.unit
def test_unknown_field_reports_additional_property_code_and_path() -> None:
    errors = validate_arguments(SCHEMA, {"email": "a@b.com", "tier": "pro", "nickname": "x"})
    assert errors == [
        ArgumentError(
            path="/nickname",
            code="ADDITIONAL_PROPERTY",
            message="Unknown field 'nickname'; this workflow accepts no extra fields.",
        )
    ]


@pytest.mark.unit
def test_enum_violation_reports_enum_code_and_path() -> None:
    errors = validate_arguments(SCHEMA, {"email": "a@b.com", "tier": "platinum"})
    assert len(errors) == 1
    assert errors[0].path == "/tier"
    assert errors[0].code == "ENUM"


@pytest.mark.unit
def test_type_violation_reports_type_code() -> None:
    errors = validate_arguments(
        SCHEMA, {"email": "a@b.com", "tier": "pro", "count": "not a number"}
    )
    assert any(e.path == "/count" and e.code == "TYPE" for e in errors)


@pytest.mark.unit
def test_max_length_violation_reports_max_length_code() -> None:
    errors = validate_arguments(
        SCHEMA, {"email": "a@b.com", "tier": "pro", "notes": "way too long"}
    )
    assert any(e.path == "/notes" and e.code == "MAX_LENGTH" for e in errors)


@pytest.mark.unit
def test_minimum_violation_reports_minimum_code() -> None:
    errors = validate_arguments(SCHEMA, {"email": "a@b.com", "tier": "pro", "count": -1})
    assert any(e.path == "/count" and e.code == "MINIMUM" for e in errors)


@pytest.mark.unit
def test_maximum_violation_reports_maximum_code() -> None:
    errors = validate_arguments(SCHEMA, {"email": "a@b.com", "tier": "pro", "count": 1000})
    assert any(e.path == "/count" and e.code == "MAXIMUM" for e in errors)


@pytest.mark.unit
def test_multiple_errors_all_reported_together() -> None:
    errors = validate_arguments(SCHEMA, {"tier": "platinum", "nickname": "x"})
    codes = {(e.path, e.code) for e in errors}
    assert ("/email", "REQUIRED") in codes
    assert ("/tier", "ENUM") in codes
    assert ("/nickname", "ADDITIONAL_PROPERTY") in codes


# --------------------------------------------------------------------------------------
# JSON-Pointer path construction (RFC 6901), including escaping edge cases
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_nested_object_error_produces_a_multi_segment_pointer() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "address": {
                "type": "object",
                "properties": {"zip": {"type": "string", "pattern": r"^\d{5}$"}},
            }
        },
    }
    errors = validate_arguments(schema, {"address": {"zip": "not-a-zip"}})
    assert errors == [ArgumentError(path="/address/zip", code="PATTERN", message=errors[0].message)]


@pytest.mark.unit
def test_array_index_error_produces_a_numeric_pointer_segment() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    }
    errors = validate_arguments(schema, {"items": ["ok", 123, "also ok"]})
    assert errors == [ArgumentError(path="/items/1", code="TYPE", message=errors[0].message)]


@pytest.mark.unit
def test_field_name_containing_tilde_and_slash_is_escaped_per_rfc6901() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["a/b~c"],
        "properties": {"a/b~c": {"type": "string"}},
    }
    errors = validate_arguments(schema, {})
    assert errors == [
        ArgumentError(path="/a~1b~0c", code="REQUIRED", message="Field 'a/b~c' is required.")
    ]


@pytest.mark.unit
def test_argument_error_to_dict_matches_documented_shape() -> None:
    error = ArgumentError(path="/tier", code="ENUM", message="msg")
    assert error.to_dict() == {"path": "/tier", "code": "ENUM", "message": "msg"}
