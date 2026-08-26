"""``core/redaction.py``: JSONPath redaction, secret scrubbing, and output capping
(BUILD_PLAN section 12, phase 3)."""

from __future__ import annotations

import json

import pytest

from n8n_operator.core.redaction import REDACTED_MARKER, cap_output, redact, scrub_secrets


@pytest.mark.unit
def test_redact_replaces_a_simple_field() -> None:
    result = redact({"email": "a@b.com", "tier": "pro"}, ["$.email"])
    assert result == {"email": REDACTED_MARKER, "tier": "pro"}


@pytest.mark.unit
def test_redact_reaches_a_nested_field() -> None:
    result = redact({"user": {"contact": {"email": "a@b.com"}}}, ["$.user.contact.email"])
    assert result["user"]["contact"]["email"] == REDACTED_MARKER


@pytest.mark.unit
def test_redact_reaches_every_element_of_an_array() -> None:
    result = redact({"records": [{"ssn": "1"}, {"ssn": "2"}, {"ssn": "3"}]}, ["$.records[*].ssn"])
    assert all(r["ssn"] == REDACTED_MARKER for r in result["records"])


@pytest.mark.unit
def test_redact_descendant_operator_reaches_every_matching_key_at_any_depth() -> None:
    data = {"a": {"api_key": "x"}, "b": [{"api_key": "y"}, {"c": {"api_key": "z"}}]}
    result = redact(data, ["$..api_key"])
    assert result["a"]["api_key"] == REDACTED_MARKER
    assert result["b"][0]["api_key"] == REDACTED_MARKER
    assert result["b"][1]["c"]["api_key"] == REDACTED_MARKER


@pytest.mark.unit
def test_redact_does_not_mutate_the_original_value() -> None:
    original = {"email": "a@b.com"}
    redact(original, ["$.email"])
    assert original == {"email": "a@b.com"}


@pytest.mark.unit
def test_redact_with_no_paths_is_a_no_op() -> None:
    original = {"email": "a@b.com"}
    assert redact(original, []) == original


@pytest.mark.unit
def test_redact_path_matching_nothing_is_not_an_error() -> None:
    result = redact({"a": 1}, ["$.does.not.exist"])
    assert result == {"a": 1}


@pytest.mark.unit
def test_redact_applies_every_path_in_a_list() -> None:
    result = redact({"a": 1, "b": 2, "c": 3}, ["$.a", "$.b"])
    assert result == {"a": REDACTED_MARKER, "b": REDACTED_MARKER, "c": 3}


# --------------------------------------------------------------------------------------
# scrub_secrets
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_scrub_secrets_replaces_a_literal_occurrence() -> None:
    result = scrub_secrets({"msg": "token=sk-abc123 failed"}, ["sk-abc123"])
    assert "sk-abc123" not in result["msg"]
    assert REDACTED_MARKER in result["msg"]


@pytest.mark.unit
def test_scrub_secrets_reaches_nested_and_array_positions() -> None:
    data = {"a": {"b": ["contains sk-abc123 here", "clean"]}}
    result = scrub_secrets(data, ["sk-abc123"])
    assert "sk-abc123" not in result["a"]["b"][0]
    assert result["a"]["b"][1] == "clean"


@pytest.mark.unit
def test_scrub_secrets_replaces_every_occurrence_not_just_the_first() -> None:
    result = scrub_secrets({"msg": "secret secret secret"}, ["secret"])
    assert "secret" not in result["msg"]
    assert result["msg"].count(REDACTED_MARKER) == 3


@pytest.mark.unit
def test_scrub_secrets_ignores_empty_secret_values() -> None:
    original = {"msg": "hello"}
    assert scrub_secrets(original, ["", ""]) == original


@pytest.mark.unit
def test_scrub_secrets_with_no_secrets_returns_the_value_unchanged() -> None:
    original = {"msg": "hello"}
    result = scrub_secrets(original, [])
    assert result == original


@pytest.mark.unit
def test_scrub_secrets_leaves_non_string_leaves_alone() -> None:
    result = scrub_secrets({"count": 5, "ok": True, "x": None}, ["5"])
    assert result == {"count": 5, "ok": True, "x": None}


# --------------------------------------------------------------------------------------
# cap_output
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_cap_output_passes_through_small_values_unchanged() -> None:
    value, truncated = cap_output({"a": 1}, max_bytes=1000)
    assert value == {"a": 1}
    assert truncated is False


@pytest.mark.unit
def test_cap_output_truncates_and_marks_an_oversized_value() -> None:
    value, truncated = cap_output({"x": "y" * 10_000}, max_bytes=200)
    assert truncated is True
    assert value["truncated"] is True


@pytest.mark.unit
def test_cap_output_result_is_always_valid_json() -> None:
    value, _truncated = cap_output({"x": "y" * 10_000}, max_bytes=200)
    json.loads(json.dumps(value))  # raises if not round-trippable


@pytest.mark.unit
def test_cap_output_result_fits_within_max_bytes() -> None:
    value, _truncated = cap_output({"x": "y" * 10_000}, max_bytes=200)
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 200


@pytest.mark.unit
def test_cap_output_at_a_tiny_budget_still_produces_valid_json_within_budget() -> None:
    value, truncated = cap_output({"x": "y" * 10_000}, max_bytes=25)
    assert truncated is True
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 25
    json.loads(json.dumps(value))


@pytest.mark.unit
def test_cap_output_at_an_impossibly_tiny_budget_still_fits_and_is_valid_json() -> None:
    """No structured marker fits in 10 bytes — the ultimate fallback is the smallest
    possible valid JSON value, ``0``, still paired with ``truncated=True``."""
    value, truncated = cap_output({"x": "y" * 10_000}, max_bytes=10)
    assert truncated is True
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 10
    json.loads(json.dumps(value))


@pytest.mark.unit
def test_cap_output_boundary_exactly_at_the_limit_is_not_truncated() -> None:
    value = {"a": "x"}
    encoded_len = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    result, truncated = cap_output(value, max_bytes=encoded_len)
    assert truncated is False
    assert result == value


@pytest.mark.unit
def test_cap_output_handles_unicode_without_crashing() -> None:
    value, truncated = cap_output({"x": "héllo wörld 你好 " * 2000}, max_bytes=100)
    assert truncated is True
    json.loads(json.dumps(value))
