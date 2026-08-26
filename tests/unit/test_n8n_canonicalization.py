"""``n8n/canonicalization.py`` against the sanitized, empirically-gathered fixtures in
``tests/fixtures/canonicalization/`` (BUILD_PLAN section 12, phase 4; AC-26, AC-27).

Every fixture pair here came from a real n8n 2.35.7 instance — see
``docs/N8N_COMPATIBILITY.md`` for the exact request/response evidence each assertion
below is backed by.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from n8n_operator.n8n.canonicalization import (
    CANONICALIZATION_VERSION,
    EXCLUSION_ALLOWLIST,
    canonical_bytes,
    canonical_form,
    compute_definition_hash,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "canonicalization"


def _load(name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return result


# --------------------------------------------------------------------------------------
# AC-26: allowlisted fields preserve the hash; everything else changes it.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_node_position_only_change_preserves_the_hash() -> None:
    before, after = _load("position_before"), _load("position_after")
    assert compute_definition_hash(before) == compute_definition_hash(after)


@pytest.mark.unit
def test_pin_data_change_preserves_the_hash() -> None:
    before, after = _load("pindata_before"), _load("pindata_after")
    assert compute_definition_hash(before) == compute_definition_hash(after)


@pytest.mark.unit
def test_workflow_name_change_preserves_the_hash() -> None:
    """Structurally excluded, not a CAN-02 allowlist entry — ``name`` is row metadata,
    never part of ``nodes``/``connections``/``settings`` to begin with."""
    before, after = _load("name_before"), _load("name_after")
    assert compute_definition_hash(before) == compute_definition_hash(after)


@pytest.mark.unit
def test_active_state_change_preserves_the_hash_and_does_not_crash() -> None:
    """The critical structural finding (docs/N8N_COMPATIBILITY.md section 5):
    ``activeVersion`` is entirely ``null`` when inactive. This must not raise, and the
    hash must agree with the active reading, since active state is not part of the
    definition."""
    before, after = _load("active_state_before"), _load("active_state_after")
    assert after["activeVersion"] is None  # sanity: the fixture actually captures this
    assert compute_definition_hash(before) == compute_definition_hash(after)


@pytest.mark.unit
def test_node_parameter_change_changes_the_hash() -> None:
    before, after = _load("parameter_before"), _load("parameter_after")
    assert compute_definition_hash(before) != compute_definition_hash(after)


@pytest.mark.unit
def test_connection_topology_change_changes_the_hash() -> None:
    before, after = _load("connection_before"), _load("connection_after")
    assert compute_definition_hash(before) != compute_definition_hash(after)


@pytest.mark.unit
def test_webhook_path_change_changes_the_hash() -> None:
    before, after = _load("webhook_path_before"), _load("webhook_path_after")
    assert compute_definition_hash(before) != compute_definition_hash(after)


@pytest.mark.unit
def test_credential_binding_change_changes_the_hash() -> None:
    before, after = _load("credential_binding_before"), _load("credential_binding_after")
    assert compute_definition_hash(before) != compute_definition_hash(after)


@pytest.mark.unit
def test_unchanged_reads_produce_the_identical_hash() -> None:
    a, b = _load("unchanged_read_a"), _load("unchanged_read_b")
    assert compute_definition_hash(a) == compute_definition_hash(b)


# --------------------------------------------------------------------------------------
# AC-27: an unrecognized field still contributes to the hash.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_an_unrecognized_field_on_a_node_changes_the_hash() -> None:
    base = _load("position_before")
    mutated = json.loads(json.dumps(base))
    mutated["nodes"][0]["aBrandNewFieldNoOneHasSeenBefore"] = "some future n8n field"
    assert compute_definition_hash(base) != compute_definition_hash(mutated)


@pytest.mark.unit
def test_an_unrecognized_top_level_field_within_settings_changes_the_hash() -> None:
    base = _load("position_before")
    mutated = json.loads(json.dumps(base))
    mutated.setdefault("settings", {})["futureSetting"] = True
    assert compute_definition_hash(base) != compute_definition_hash(mutated)


@pytest.mark.unit
def test_an_unrecognized_field_survives_into_the_canonical_form_verbatim() -> None:
    base = _load("position_before")
    mutated = json.loads(json.dumps(base))
    mutated["nodes"][0]["novelField"] = {"nested": ["structure", 1, True, None]}
    form = canonical_form(mutated)
    assert form["nodes"][0]["novelField"] == {"nested": ["structure", 1, True, None]}


# --------------------------------------------------------------------------------------
# CAN-04: deterministic, idempotent serialization.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_canonical_form_is_idempotent() -> None:
    base = _load("position_before")
    once = canonical_form(base)
    twice = canonical_form(json.loads(json.dumps(once)))
    assert once == twice


@pytest.mark.unit
def test_canonical_bytes_is_compact_and_deterministic() -> None:
    base = _load("position_before")
    a = canonical_bytes(base)
    b = canonical_bytes(json.loads(json.dumps(base)))
    assert a == b
    # CAN-04: no insignificant (structural) whitespace — re-serializing the parsed
    # payload with the same compact separators must reproduce the identical bytes.
    # String *content* legitimately contains spaces (e.g. a node's own JS code), so
    # this checks structural compactness, not "no space byte anywhere".
    reparsed = json.loads(a.decode())
    recompacted = json.dumps(
        reparsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert a == recompacted


@pytest.mark.unit
def test_compute_definition_hash_has_the_sha256_prefix() -> None:
    digest = compute_definition_hash(_load("position_before"))
    assert digest.startswith("sha256:")
    hex_part = digest.removeprefix("sha256:")
    assert len(hex_part) == 64
    int(hex_part, 16)


# --------------------------------------------------------------------------------------
# CAN-07: the algorithm version is part of the hash preimage.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_canonicalization_version_is_part_of_the_preimage() -> None:
    base = _load("position_before")
    real = canonical_bytes(base)
    assert json.dumps(CANONICALIZATION_VERSION).encode() in real
    payload = json.loads(real.decode())
    assert payload["canonicalization_version"] == CANONICALIZATION_VERSION


# --------------------------------------------------------------------------------------
# CAN-03: the allowlist itself is well-formed.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_exclusion_allowlist_has_no_wildcard_or_regex_entries() -> None:
    """CAN-03 forbids a *wildcard* or *regex family* entry — one that could match paths
    nobody has actually justified. ``[]`` in this module's own field-path syntax
    (``nodes[].position``) means "this exact field, applied per array element", which
    is enumerable and unambiguous, not a wildcard; it is deliberately not in the
    forbidden set below. What CAN-03 actually rules out — glob/regex metacharacters
    that would match an open-ended set of paths — is."""
    forbidden_chars = set("*?(){}^$|\\")
    for entry in EXCLUSION_ALLOWLIST:
        assert not (forbidden_chars & set(entry.field_path)), entry.field_path


@pytest.mark.unit
def test_every_allowlist_entry_names_a_field_path_evidence_and_version_range() -> None:
    for entry in EXCLUSION_ALLOWLIST:
        assert entry.field_path
        assert entry.evidence
        assert entry.n8n_versions
