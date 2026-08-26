"""``registry/loader.py``: YAML parsing safety, canonical JSON, and orchestration
(BUILD_PLAN section 12, phase 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from n8n_operator.registry.loader import (
    MAX_REGISTRY_FILE_BYTES,
    RegistryParseError,
    RegistryValidationError,
    canonical_json_bytes,
    load_registry,
    parse_registry_yaml,
    read_registry_source,
    sha256_hex,
)

VALID_DOCUMENT = """apiVersion: n8n-operator/v1
metadata:
  name: test
workflows:
  - id: wf.a
    n8n_workflow_id: n8n-1
    title: A
    description: B
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      additionalProperties: false
""".format(hash="a" * 64)


# --------------------------------------------------------------------------------------
# Strict, safe YAML parsing
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_duplicate_top_level_key_is_rejected() -> None:
    with pytest.raises(RegistryParseError, match="duplicate key"):
        parse_registry_yaml("a: 1\na: 2\n")


@pytest.mark.unit
def test_duplicate_nested_key_is_rejected() -> None:
    with pytest.raises(RegistryParseError, match="duplicate key"):
        parse_registry_yaml("a:\n  b: 1\n  b: 2\n")


@pytest.mark.unit
def test_ordinary_document_parses_fine() -> None:
    result = parse_registry_yaml("a: 1\nb: [1, 2, 3]\nc:\n  d: true\n")
    assert result == {"a": 1, "b": [1, 2, 3], "c": {"d": True}}


@pytest.mark.unit
def test_invalid_yaml_syntax_is_rejected() -> None:
    with pytest.raises(RegistryParseError):
        parse_registry_yaml("a: [unterminated\n")


@pytest.mark.unit
def test_arbitrary_python_object_construction_is_rejected() -> None:
    """The classic PyYAML RCE vector: !!python/object tags must never construct
    anything, whether or not the payload happens to also be a duplicate key."""
    malicious = "!!python/object/apply:builtins.exec ['import os']"
    with pytest.raises(RegistryParseError):
        parse_registry_yaml(malicious)


@pytest.mark.unit
def test_file_size_limit_is_enforced(tmp_path: Path) -> None:
    huge = tmp_path / "huge.yaml"
    huge.write_text("a" * (MAX_REGISTRY_FILE_BYTES + 1))
    with pytest.raises(RegistryParseError, match="exceeds"):
        read_registry_source(huge)


@pytest.mark.unit
def test_file_at_exactly_the_limit_is_accepted(tmp_path: Path) -> None:
    at_limit = tmp_path / "at_limit.yaml"
    at_limit.write_text("a" * MAX_REGISTRY_FILE_BYTES)
    text = read_registry_source(at_limit)
    assert len(text.encode("utf-8")) == MAX_REGISTRY_FILE_BYTES


@pytest.mark.unit
def test_missing_file_raises_registry_parse_error(tmp_path: Path) -> None:
    with pytest.raises(RegistryParseError):
        read_registry_source(tmp_path / "does-not-exist.yaml")


@pytest.mark.unit
def test_non_utf8_file_raises_registry_parse_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad_encoding.yaml"
    bad.write_bytes(b"\xff\xfe\x00\x01invalid utf-8")
    with pytest.raises(RegistryParseError):
        read_registry_source(bad)


# --------------------------------------------------------------------------------------
# canonical_json_bytes
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_canonical_json_sorts_keys() -> None:
    a = canonical_json_bytes({"b": 1, "a": 2})
    b = canonical_json_bytes({"a": 2, "b": 1})
    assert a == b


@pytest.mark.unit
def test_canonical_json_is_compact() -> None:
    result = canonical_json_bytes({"a": 1})
    assert b" " not in result
    assert result == b'{"a":1}'


@pytest.mark.unit
def test_canonical_json_preserves_array_order() -> None:
    a = canonical_json_bytes({"x": [1, 2, 3]})
    b = canonical_json_bytes({"x": [3, 2, 1]})
    assert a != b


@pytest.mark.unit
def test_canonical_json_nfc_normalizes_strings() -> None:
    # "é" as a single code point (NFC) vs "e" + combining acute accent (NFD).
    nfc = canonical_json_bytes({"x": "\u00e9"})
    nfd = canonical_json_bytes({"x": "e\u0301"})
    assert nfc == nfd


@pytest.mark.unit
def test_canonical_json_is_idempotent() -> None:
    import json

    once = canonical_json_bytes({"b": [1, {"z": 1, "a": 2}], "a": "x"})
    twice = canonical_json_bytes(json.loads(once.decode()))
    assert once == twice


@pytest.mark.unit
def test_canonical_json_rejects_nan_and_infinity() -> None:
    with pytest.raises(ValueError):
        canonical_json_bytes({"x": float("nan")})


@pytest.mark.unit
def test_sha256_hex_is_64_lowercase_hex_chars() -> None:
    digest = sha256_hex(b"hello world")
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)  # raises ValueError if not valid hex


# --------------------------------------------------------------------------------------
# load_registry orchestration
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_load_registry_succeeds_on_a_valid_document(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(VALID_DOCUMENT)
    loaded = load_registry(path, server_max_argument_bytes=262_144)
    assert loaded.content_hash.startswith("sha256:")
    assert len(loaded.entries) == 1
    assert loaded.entries[0].id == "wf.a"
    assert loaded.source_path == str(path)


@pytest.mark.unit
def test_load_registry_raises_on_a_rule_violation(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(VALID_DOCUMENT.replace("n8n-operator/v1", "n8n-operator/v99"))
    with pytest.raises(RegistryValidationError) as excinfo:
        load_registry(path, server_max_argument_bytes=262_144)
    assert {v.rule for v in excinfo.value.violations} == {"R1"}


@pytest.mark.unit
def test_load_registry_reports_every_violation_in_one_pass(tmp_path: Path) -> None:
    """Two independent, unrelated violations in the same document are *both* reported —
    not just the first one found."""
    broken = VALID_DOCUMENT.replace("n8n-operator/v1", "n8n-operator/v99").replace(
        "definition_hash: sha256:" + "a" * 64, "definition_hash: not-a-valid-hash"
    )
    path = tmp_path / "workflows.yaml"
    path.write_text(broken)
    with pytest.raises(RegistryValidationError) as excinfo:
        load_registry(path, server_max_argument_bytes=262_144)
    assert {v.rule for v in excinfo.value.violations} == {"R1", "R7"}


@pytest.mark.unit
def test_load_registry_raises_on_missing_required_field(tmp_path: Path) -> None:
    broken = VALID_DOCUMENT.replace("    owner: carolyn\n", "")
    path = tmp_path / "workflows.yaml"
    path.write_text(broken)
    with pytest.raises(RegistryValidationError) as excinfo:
        load_registry(path, server_max_argument_bytes=262_144)
    assert {v.rule for v in excinfo.value.violations} == {"SCHEMA"}


@pytest.mark.unit
def test_empty_workflows_list_loads_successfully(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("apiVersion: n8n-operator/v1\nmetadata:\n  name: empty\nworkflows: []\n")
    loaded = load_registry(path, server_max_argument_bytes=262_144)
    assert loaded.entries == []


@pytest.mark.unit
def test_rule_violation_format_names_the_rule_and_workflow() -> None:
    from n8n_operator.registry.loader import RuleViolation

    v = RuleViolation("R7", "bad hash", "wf.a")
    assert v.format() == "R7 (wf.a): bad hash"
    v2 = RuleViolation("R1", "bad apiVersion")
    assert v2.format() == "R1: bad apiVersion"
